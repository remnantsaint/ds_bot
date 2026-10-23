"""新闻/天气插件离线测试：不请求真实网站，不发送 QQ 消息。"""

import asyncio
import json
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import nonebot

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi", log_level="WARNING", deepseek_api_key="test-only", _env_file=None)

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from src.plugins import briefing as mod


TODAY = date(2026, 10, 23)
NOW = datetime(2026, 10, 23, 21, tzinfo=mod.BEIJING)
PNG = b"\x89PNG\r\n\x1a\nfake-image"
NEWS_IMAGE = "https://cdn.jsdmirror.com/gh/vikiboss/60s-static-host@main/static/images/2026-10-23.png"


def weather_url(day="20261023", *, hours="024"):
    return (f"https://image.nmc.cn/product/{day[:4]}/{day[4:6]}/{day[6:]}/RFFC/medium/"
            f"SEVP_NMC_RFFC_SNWFD_ETM_ACHN_L88_P9_{day}2000{hours}12.jpg?v=1")


def weather_html(day="20261023", **kwargs):
    return f'<img id=imgpath data-time="10/23 20:00" src="{weather_url(day, **kwargs)}">'


def event(text, *, at=False, user_id=2, extra=None):
    message = Message(([MessageSegment.at(1)] if at else []) + [MessageSegment.text(text)] + (extra or []))
    return GroupMessageEvent(
        time=1, self_id=1, post_type="message", sub_type="normal", user_id=user_id,
        message_type="group", group_id=100, message_id=10, message=message,
        original_message=message.copy(), raw_message=str(message), font=0,
        sender={"user_id": user_id, "nickname": "测试", "card": "", "role": "member"},
        anonymous=None, to_me=at,
    )


class ForecastTests(unittest.TestCase):
    def test_actual_official_filename_format(self):
        url = ("https://image.nmc.cn/product/2026/10/23/RFFC/medium/"
               "SEVP_NMC_RFFC_SNWFD_ETM_ACHN_L88_P9_20261023200002412.jpg?v=1792760359655")
        self.assertEqual(mod.forecast_image(f'<img id=imgpath src="{url}">', TODAY)[0], url)

    def test_today_forecast_and_caption(self):
        url, caption = mod.forecast_image(weather_html(), TODAY)
        self.assertEqual(url, weather_url())
        self.assertIn("10月23日 20:00", caption)
        self.assertIn("图内标题", caption)

    def test_old_or_future_image_is_rejected_even_if_page_time_is_today(self):
        for day in ("20261022", "20261024"):
            with self.subTest(day=day), self.assertRaises(mod.BriefingError):
                mod.forecast_image(weather_html(day), TODAY)

    def test_wrong_product_horizon_missing_image_and_duplicate_are_rejected(self):
        cases = [weather_html(hours="048"), weather_html().replace("RFFC", "WEAP"),
                 "<html>no image</html>", weather_html() * 2,
                 weather_html().replace("2026/10/23", "2026/10/22")]
        for html in cases:
            with self.subTest(html=html), self.assertRaises(mod.BriefingError):
                mod.forecast_image(html, TODAY)

    def test_image_url_rejects_private_unknown_http_or_credentials(self):
        for url in ("http://image.nmc.cn/a.jpg", "https://127.0.0.1/a.jpg",
                    "https://image.nmc.cn.attacker.invalid/a.jpg",
                    "https://secret@image.nmc.cn/a.jpg", "https://image.nmc.cn:8080/a.jpg"):
            with self.subTest(url=url), self.assertRaises(mod.BriefingError):
                mod.validate_image_url(url)


class TriggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_exact_commands_with_and_without_at(self):
        with patch.dict(mod.briefing_config, {"enabled": True}):
            for text, kind in (("今日新闻", "news"), ("今日天气", "weather"), ("今日气温", "weather")):
                for at in (False, True):
                    evt = event(" " + text + " ", at=at)
                    self.assertEqual(mod.request_kind(evt), kind)
                    self.assertTrue(await mod.briefing.rule(None, evt, {}))
        self.assertEqual(mod.briefing.priority, 5)
        self.assertTrue(mod.briefing.block)

    async def test_non_commands_disabled_self_and_other_mentions_do_not_match(self):
        for evt in (event("今日新闻怎么样"), event("查询今日天气"), event("今日天气", user_id=1),
                    event("今日天气", extra=[MessageSegment.at(3)]),
                    event("今日天气", extra=[MessageSegment.image("https://example.com/a.png")])):
            self.assertIsNone(mod.request_kind(evt))
        self.assertIsNone(mod.request_kind(SimpleNamespace()))
        with patch.dict(mod.briefing_config, {"enabled": False}):
            self.assertIsNone(mod.request_kind(event("今日新闻")))

    async def test_send_image_and_failure_only_once_without_model(self):
        bot = SimpleNamespace(send=AsyncMock())
        with patch.object(mod.service, "get", AsyncMock(return_value=(PNG, "标题"))):
            await mod.handle_briefing(bot, event("今日气温"))
        message = bot.send.await_args.args[1]
        self.assertEqual([s.type for s in message], ["reply", "text", "image"])
        self.assertTrue(message[-1].data["file"].startswith("base64://"))
        bot.send.reset_mock()
        with patch.object(mod.service, "get", AsyncMock(side_effect=mod.BriefingError("今天尚未更新"))):
            await mod.handle_briefing(bot, event("今日新闻"))
        self.assertEqual(bot.send.await_count, 1)
        self.assertIn("今天尚未更新", str(bot.send.await_args.args[1]))


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = mod.BriefingService({"weather_cache_seconds": 3600, "failure_cache_seconds": 60})
        self.now = patch.object(mod, "beijing_now", return_value=NOW)
        self.now.start()

    async def asyncTearDown(self):
        self.now.stop()

    async def test_concurrent_requests_share_one_fetch_across_groups_and_aliases(self):
        with patch.object(self.service, "_load", AsyncMock(return_value=(PNG, "标题"))) as load:
            results = await asyncio.gather(*(self.service.get("weather") for _ in range(5)))
        self.assertEqual(load.await_count, 1)
        self.assertEqual(results, [(PNG, "标题")] * 5)

    async def test_midnight_invalidates_news_and_weather_cache(self):
        for kind in ("news", "weather"):
            with patch.object(self.service, "_load", AsyncMock(return_value=(PNG, "标题"))) as load:
                await self.service.get(kind)
                tomorrow = NOW.replace(day=24)
                with patch.object(mod, "beijing_now", return_value=tomorrow):
                    await self.service.get(kind)
            self.assertEqual(load.await_count, 2)

    async def test_expired_weather_cache_does_not_fall_back_when_source_fails(self):
        with patch.object(self.service, "_load", AsyncMock(return_value=(PNG, "标题"))):
            await self.service.get("weather")
        self.service.cache["weather"].expires = 0
        with patch.object(self.service, "_load", AsyncMock(side_effect=RuntimeError("private details"))) as load:
            for _ in range(2):
                with self.assertRaises(mod.BriefingError) as result:
                    await self.service.get("weather")
                self.assertNotIn("private details", str(result.exception))
        self.assertEqual(load.await_count, 1)

    async def test_midnight_during_download_rejects_old_image(self):
        with patch.object(mod, "beijing_now", side_effect=[NOW, NOW.replace(day=24)]), \
                patch.object(self.service, "_load", AsyncMock(return_value=(PNG, "标题"))):
            with self.assertRaisesRegex(mod.BriefingError, "日期已切换"):
                await self.service.get("news")


class SourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.news_date = TODAY.isoformat()
        self.news_image = NEWS_IMAGE
        self.html = weather_html()
        self.image_type = "image/png"
        self.image = PNG
        self.redirect = None
        self.transport = httpx.MockTransport(self.respond)
        real_client = httpx.AsyncClient
        self.patch = patch.object(mod.httpx, "AsyncClient", side_effect=lambda **kw: real_client(transport=self.transport, **kw))
        self.patch.start()
        self.service = mod.BriefingService({})

    async def asyncTearDown(self):
        self.patch.stop()

    def respond(self, request):
        self.calls.append(str(request.url))
        if str(request.url) == mod.NEWS_API:
            return httpx.Response(200, json={"code": 200, "data": {"date": self.news_date, "image": self.news_image}})
        if str(request.url) == mod.WEATHER_PAGE:
            return httpx.Response(200, text=self.html)
        if self.redirect:
            return httpx.Response(302, headers={"location": self.redirect})
        return httpx.Response(200, content=self.image, headers={"content-type": self.image_type})

    async def test_both_sources_fetch_verified_image_bytes(self):
        for kind in ("news", "weather"):
            image, _ = await self.service._load(kind, TODAY)
            self.assertEqual(image, PNG)
        self.assertEqual(len(self.calls), 4)

    async def test_outdated_news_and_weather_do_not_download_images(self):
        self.news_date = "2026-10-22"
        self.html = weather_html("20261022")
        for kind in ("news", "weather"):
            with self.assertRaisesRegex(mod.BriefingError, "尚未更新"):
                await self.service._load(kind, TODAY)
        self.assertEqual(self.calls, [mod.NEWS_API, mod.WEATHER_PAGE])

    async def test_bad_image_content_type_signature_and_size_fail(self):
        self.image_type = "text/html"
        with self.assertRaises(mod.BriefingError):
            await self.service._load("news", TODAY)

        self.image_type, self.image = "image/png", b"not an image"
        with self.assertRaises(mod.BriefingError):
            await self.service._load("news", TODAY)
        self.image = PNG
        with patch.object(mod, "MAX_IMAGE_BYTES", 4), self.assertRaises(mod.BriefingError):
            await self.service._load("news", TODAY)

    async def test_today_metadata_with_yesterday_image_url_fails(self):
        self.news_image = NEWS_IMAGE.replace("2026-10-23", "2026-10-22")
        with self.assertRaisesRegex(mod.BriefingError, "尚未更新"):
            await self.service._load("news", TODAY)
        self.assertEqual(self.calls, [mod.NEWS_API])

    async def test_redirect_to_private_host_is_never_requested(self):
        self.redirect = "https://127.0.0.1/private"
        with self.assertRaises(mod.BriefingError):
            await self.service._load("news", TODAY)
        self.assertNotIn(self.redirect, self.calls)


if __name__ == "__main__":
    unittest.main()
