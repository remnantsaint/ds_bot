"""群内关键词取成品图片；不调用模型，缓存只保存在内存。"""

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from nonebot import logger, on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.rule import Rule

from src.utils import load_config


NEWS_API = "https://60s.viki.moe/v2/60s"
WEATHER_PAGE = "https://www.nmc.cn/publish/temperature/hight/24hour.html"
IMAGE_HOSTS = {
    "cdn.jsdmirror.com", "cdn.jsdelivr.net", "60s-static.viki.moe",
    "raw.githubusercontent.com", "mmbiz.qpic.cn", "image.nmc.cn",
}
BEIJING = ZoneInfo("Asia/Shanghai")
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class BriefingError(Exception):
    """可直接展示给群友的失败原因。"""


def beijing_now() -> datetime:
    return datetime.now(BEIJING)


class ForecastImageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "img" and values.get("id") == "imgpath":
            self.images.append(values.get("src", ""))


def forecast_image(html: str, today: date) -> tuple[str, str]:
    parser = ForecastImageParser()
    parser.feed(html)
    if len(parser.images) != 1:
        raise BriefingError("未找到今天的气温预报图，请稍后再试。")
    url = urljoin(WEATHER_PAGE, parser.images[0])
    parsed = urlsplit(url)
    # 校验产品类型、24 小时时效和真实文件时间，不把实况图或页面时间当预报日期。
    match = re.fullmatch(
        r"/product/(\d{4})/(\d{2})/(\d{2})/RFFC/(?:medium/)?"
        r"SEVP_NMC_RFFC_SNWFD_ETM_ACHN_L88_P9_(\d{12})02412\.jpg",
        parsed.path, flags=re.IGNORECASE,
    )
    if parsed.scheme != "https" or parsed.hostname != "image.nmc.cn" or not match:
        raise BriefingError("气温预报图片格式发生变化，暂时无法获取。")
    try:
        issued = datetime.strptime(match[4], "%Y%m%d%H%M")
        directory_date = date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError as error:
        raise BriefingError("气温预报图片日期无效，暂时无法获取。") from error
    if issued.date() != today or directory_date != today:
        raise BriefingError("今天的气温预报图尚未更新，请稍后再试。")
    caption = (
        f"全国最高气温24小时预报（{issued:%m月%d日 %H:%M}时次，北京时间）\n"
        "预报覆盖的日期与时段以图内标题为准。"
    )
    return url, caption


def validate_image_url(url: str):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in IMAGE_HOSTS
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)):
        raise BriefingError("图片来源地址异常，暂时无法获取。")


async def read_response(client, url: str, *, limit: int, image: bool = False) -> bytes:
    # 对图片每次跳转重新验证域名；不将密钥或 QQ 凭据发送给图片站点。
    for _ in range(4):
        if image:
            validate_image_url(url)
        async with client.stream("GET", url) as response:
            if response.is_redirect:
                if not image or not response.headers.get("location"):
                    raise BriefingError("来源服务返回异常跳转，请稍后再试。")
                url = urljoin(url, response.headers["location"])
                continue
            response.raise_for_status()
            if image and not response.headers.get("content-type", "").lower().startswith("image/"):
                raise BriefingError("来源没有返回有效图片，请稍后再试。")
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise BriefingError("来源返回的内容过大，暂时无法发送。")
                chunks.append(chunk)
            return b"".join(chunks)
    raise BriefingError("图片来源跳转过多，请稍后再试。")


@dataclass
class CacheEntry:
    day: date
    expires: float
    image: bytes = b""
    caption: str = ""
    error: str = ""


class BriefingService:
    def __init__(self, config: dict):
        self.config = config
        self.cache: dict[str, CacheEntry] = {}
        self.locks = {kind: asyncio.Lock() for kind in ("news", "weather")}

    async def _load(self, kind: str, today: date) -> tuple[bytes, str]:
        async with httpx.AsyncClient(
            timeout=float(self.config.get("timeout_seconds", 15)),
            follow_redirects=False, trust_env=False,
            headers={"User-Agent": "ds-bot/0.1 (daily-briefing)"},
        ) as client:
            if kind == "news":
                raw = await read_response(client, NEWS_API, limit=1024 * 1024)
                data = json.loads(raw)
                if not isinstance(data, dict) or data.get("code") != 200:
                    raise BriefingError("新闻来源暂时不可用，请稍后再试。")
                payload = data.get("data")
                if not isinstance(payload, dict) or payload.get("date") != today.isoformat():
                    raise BriefingError("今天的新闻图片尚未更新，请稍后再试。")
                url = payload.get("image")
                if not isinstance(url, str) or not url:
                    raise BriefingError("今天的新闻图片暂时不可用，请稍后再试。")
                dated_image = re.search(r"/(\d{4}-\d{2}-\d{2})\.(?:png|jpe?g)$", urlsplit(url).path, re.I)
                if dated_image and dated_image[1] != today.isoformat():
                    raise BriefingError("今天的新闻图片尚未更新，请稍后再试。")
                caption = f"今日新闻 · {today:%Y年%m月%d日}"
            else:
                raw = await read_response(client, WEATHER_PAGE, limit=2 * 1024 * 1024)
                url, caption = forecast_image(raw.decode("utf-8"), today)
            image = await read_response(client, url, limit=MAX_IMAGE_BYTES, image=True)
            if not image.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")):
                raise BriefingError("来源返回的图片内容无效，请稍后再试。")
            return image, caption

    async def get(self, kind: str) -> tuple[bytes, str]:
        async with self.locks[kind]:
            today = beijing_now().date()
            cached = self.cache.get(kind)
            if cached and cached.day == today and cached.expires > time.monotonic():
                if cached.error:
                    raise BriefingError(cached.error)
                logger.debug(f"[briefing] {kind} 命中当日图片缓存")
                return cached.image, cached.caption
            try:
                image, caption = await self._load(kind, today)
                if beijing_now().date() != today:
                    raise BriefingError("日期已切换，请重新发送关键词获取今天的图片。")
            except Exception as error:
                message = str(error) if isinstance(error, BriefingError) else "图片获取失败，来源服务或网络暂时不可用，请稍后再试。"
                self.cache[kind] = CacheEntry(
                    today, time.monotonic() + float(self.config.get("failure_cache_seconds", 60)),
                    error=message,
                )
                logger.warning(f"[briefing] {kind} 获取失败：{type(error).__name__}")
                raise BriefingError(message) from error
            # 新闻当天复用；天气定时刷新；两者跨北京时间零点均立即失效。
            ttl = 86400 if kind == "news" else float(self.config.get("weather_cache_seconds", 3600))
            self.cache[kind] = CacheEntry(today, time.monotonic() + ttl, image, caption)
            logger.info(f"[briefing] {kind} 获取当日图片成功（{len(image)} 字节）")
            return image, caption


briefing_config = load_config().get("briefing", {})
service = BriefingService(briefing_config)


def request_kind(event: Event) -> str | None:
    if (not briefing_config.get("enabled", True) or not isinstance(event, GroupMessageEvent)
            or event.user_id == event.self_id):
        return None
    # @机器人、引用消息可用；带图或 @其他人的混合消息不当作命令。
    for segment in event.message:
        if segment.type == "at" and str(segment.data.get("qq")) == str(event.self_id):
            continue
        if segment.type not in ("text", "reply"):
            return None
    text = event.get_plaintext().strip()
    if text in briefing_config.get("news_commands", ["今日新闻"]):
        return "news"
    if text in briefing_config.get("weather_commands", ["今日天气", "今日气温"]):
        return "weather"
    return None


async def is_briefing_request(event: Event) -> bool:
    return request_kind(event) is not None


briefing = on_message(rule=Rule(is_briefing_request), priority=5, block=True)


@briefing.handle()
async def handle_briefing(bot: Bot, event: GroupMessageEvent):
    kind = request_kind(event)
    if kind is None:
        return
    try:
        image, caption = await service.get(kind)
        message = (MessageSegment.reply(event.message_id) + MessageSegment.text(caption + "\n")
                   + MessageSegment.image(image))
        await bot.send(event, message)
        logger.info(f"[briefing] 群 {event.group_id} {kind} 图片发送成功")
    except Exception as error:
        logger.warning(f"[briefing] 群 {event.group_id} {kind} 回复失败：{type(error).__name__}")
        text = str(error) if isinstance(error, BriefingError) else "图片发送失败，请稍后再试。"
        try:
            await bot.send(event, MessageSegment.reply(event.message_id) + MessageSegment.text(text))
        except Exception as notify_error:
            logger.warning(f"[briefing] 失败提示发送失败：{type(notify_error).__name__}")
