import asyncio
import random
import time
import json
from collections import defaultdict, deque

from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment

from src.deepseek_client import deepseek_client
from src.utils import load_config


config = load_config()
reply_config = config["reply"]

sessions = defaultdict(lambda: deque(maxlen=int(reply_config["max_history"])))
last_reply_time: dict[int, float] = {}
group_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
group_sequences: dict[int, int] = defaultdict(int)
pending_requests: dict[int, int] = defaultdict(int)
user_request_times: dict[tuple[int, int], float] = {}
model_slots = asyncio.Semaphore(int(reply_config.get("max_concurrent_requests", 3)))

group_chat = on_message(priority=10, block=False)


def _mention_names(event: GroupMessageEvent) -> dict[str, str]:
    # 只使用本群既有发言人的名字，不跨群借用名片。
    names = {
        str(item["user_id"]): item["nickname"]
        for item in sessions[event.group_id]
        if item["role"] == "user" and item.get("nickname")
    }
    names[str(event.user_id)] = event.sender.card or event.sender.nickname or "群友"
    for segment in event.message:
        if segment.type == "at" and isinstance(segment.data.get("name"), str):
            names[str(segment.data.get("qq"))] = segment.data["name"]
    return names


def _message_content(event: GroupMessageEvent, names: dict[str, str]) -> str:
    parts = []
    for segment in event.message:
        if segment.type == "text":
            parts.append(segment.data["text"])
        elif segment.type == "at":
            user_id = str(segment.data.get("qq"))
            if user_id == str(event.self_id):
                continue  # @机器人用于触发，不混入要评价的群友名单。
            if user_id == "all":
                parts.append("@全体成员")
            elif user_id.isdecimal():
                name = " ".join(str(names.get(user_id, "")).split())[:80]
                parts.append(f"@{name}（QQ:{user_id}）" if name else f"@QQ:{user_id}")
    return "".join(parts).strip()


def _mentioned_people(event: GroupMessageEvent, names: dict[str, str]) -> list[dict]:
    ids = dict.fromkeys(
        str(segment.data.get("qq")) for segment in event.message
        if segment.type == "at" and str(segment.data.get("qq")) != str(event.self_id)
    )
    return [
        {"user_id": user_id, "nickname": names.get(user_id)}
        for user_id in ids if user_id.isdecimal()
    ]


async def _resolve_mention_names(bot: Bot, event: GroupMessageEvent, names: dict[str, str]):
    # 在同群锁内补查，第一条消息查名片时后续提问仍排在它后面。
    missing = [person["user_id"] for person in _mentioned_people(event, names)
               if not person["nickname"]]
    for user_id in missing[:8]:
        try:
            member = await asyncio.wait_for(bot.get_group_member_info(
                group_id=event.group_id, user_id=int(user_id), no_cache=False,
            ), timeout=3)
            if (not isinstance(member, dict)
                    or str(member.get("user_id", user_id)) != user_id
                    or member.get("group_id", event.group_id) != event.group_id):
                raise ValueError("群成员信息不匹配")
            name = member.get("card") or member.get("nickname")
            if isinstance(name, str) and name.strip():
                names[user_id] = " ".join(name.split())[:80]
        except Exception as error:
            logger.debug(f"[chat] 群 {event.group_id} @对象名称查询失败：{type(error).__name__}，保留QQ号")


def _context(group_id: int, bot_id: int, current: dict) -> dict:
    # 排队期间可能又收到消息，只取当前提问之前的本群记录。
    # assistant 的 sequence 对应它回答的问题，而非回答发送的时间。
    current = dict(current)
    budget = int(reply_config.get("max_context_chars", 16000))
    # 身份及提及信息同样计入预算；过大的请求明确拒绝，不静默改变对象。
    current_size = len(json.dumps(current, ensure_ascii=False))
    if current_size > budget:
        raise ValueError("当前请求超过上下文预算，请缩短消息或减少提及成员")
    history = [
        dict(item) for item in sessions[group_id]
        if item["sequence"] < current["sequence"]
    ]
    history.sort(key=lambda item: (item["sequence"], item["role"] == "assistant"))
    budget = int(reply_config.get("max_context_chars", 16000))
    retained = []
    used = len(json.dumps(current, ensure_ascii=False))
    for item in reversed(history):
        size = len(json.dumps(item, ensure_ascii=False))
        if used + size > budget:
            break
        retained.append(item)
        used += size
    history = list(reversed(retained))
    return {
        "group_id": group_id,
        "bot_user_id": bot_id,
        "conversation": history,
        "current_request": dict(current),
    }


@group_chat.handle()
async def handle_group_message(bot: Bot, event: GroupMessageEvent):
    group_id = event.group_id
    explicitly_at = event.is_tome() or any(
        seg.type == "at" and str(seg.data.get("qq")) == str(event.self_id)
        for seg in event.message)
    if event.user_id == event.self_id:
        return
    record = _record_group_message(event)
    if record is None:
        return
    if explicitly_at and reply_config.get("reply_on_at", True):
        now = time.monotonic()
        cooldown = float(reply_config.get("user_cooldown_seconds", 3))
        key = (group_id, event.user_id)
        if now - user_request_times.get(key, float("-inf")) < cooldown:
            return
        # 清理过期记录，避免按用户永久增长。
        for stale in [k for k, timestamp in user_request_times.items() if now - timestamp >= cooldown]:
            user_request_times.pop(stale, None)
    if pending_requests[group_id] >= int(reply_config.get("max_pending_per_group", 5)):
        if explicitly_at:
            try:
                await asyncio.wait_for(bot.send(event, MessageSegment.reply(event.message_id)
                    + MessageSegment.text("当前提问较多，请稍后再试。")), timeout=10)
            except Exception:
                logger.warning("[chat] 排队已满提示发送失败")
        return
    if explicitly_at and reply_config.get("reply_on_at", True):
        user_request_times[key] = now
    pending_requests[group_id] += 1
    try:
        await asyncio.wait_for(_handle_group_message(bot, event, record),
            timeout=float(reply_config.get("request_timeout_seconds", 180)))
    except asyncio.TimeoutError:
        logger.warning(f"[chat] 群 {group_id} 请求总超时，已释放队列")
        if event.is_tome():
            try:
                await asyncio.wait_for(bot.send(event, MessageSegment.reply(event.message_id)
                    + MessageSegment.text("这条提问等待超时了，请稍后重试。")), timeout=10)
            except Exception:
                logger.warning("[chat] 超时提示发送失败")
    finally:
        pending_requests[group_id] -= 1
        if pending_requests[group_id] == 0:
            pending_requests.pop(group_id, None)


def _record_group_message(event: GroupMessageEvent):
    if event.user_id == event.self_id:
        return

    text = event.get_plaintext().strip()
    images = [seg for seg in event.message if seg.type == "image"]
    mentioned_users = [
        str(segment.data.get("qq"))
        for segment in event.message if segment.type == "at"
    ]
    has_other_mentions = any(user_id != str(event.self_id) for user_id in mentioned_users)
    if not text and not images and not has_other_mentions:
        return

    group_id = event.group_id
    group_sequences[group_id] += 1
    names = _mention_names(event)
    is_at_bot = event.is_tome() or str(event.self_id) in mentioned_users
    quoted = getattr(event, "reply", None)
    current = {
        "role": "user",
        "group_id": group_id,
        "sequence": group_sequences[group_id],
        "message_id": event.message_id,
        "user_id": event.user_id,
        "nickname": event.sender.card or event.sender.nickname or "群友",
        "content": _message_content(event, names)[:int(reply_config.get("max_message_chars", 4000))],
        "mentions_bot": is_at_bot,
        "mentioned_user_ids": mentioned_users,
        "mentioned_users": _mentioned_people(event, names),
        "reply_to_message_id": getattr(quoted, "message_id", None),
    }
    sessions[group_id].append(current)
    return text, names, is_at_bot, current


async def _handle_group_message(bot: Bot, event: GroupMessageEvent, record):
    text, names, is_at_bot, current = record
    group_id = event.group_id

    at_trigger = is_at_bot and reply_config.get("reply_on_at", True)
    lock = group_locks[group_id]
    # 普通消息仍记作背景；只有明确 @ 的请求在忙碌时排队。
    if not at_trigger and lock.locked():
        logger.debug(f"[chat] 群 {group_id} 消息 {event.message_id}：忙碌中，仅记录背景")
        return

    keyword_trigger = any(
        keyword and keyword in text for keyword in reply_config.get("keywords", [])
    )
    probability = float(reply_config.get("probability", 0))
    if not (at_trigger or keyword_trigger
            or (probability > 0 and random.random() < probability)):
        return

    if lock.locked():
        logger.info(f"[chat] 群 {group_id} 消息 {event.message_id} 用户 {event.user_id}：@ 提问已排队")

    # Lock 按等待顺序处理同群请求；不同群使用不同的锁。
    async with lock:
        if not at_trigger:
            elapsed = time.monotonic() - last_reply_time.get(group_id, float("-inf"))
            if elapsed < float(reply_config["cooldown_seconds"]):
                logger.debug(f"[chat] 群 {group_id} 消息 {event.message_id}：自动回复冷却中")
                return

        messages = [{"role": "system", "content": config["persona"]}]
        try:
            await _resolve_mention_names(bot, event, names)
            current["content"] = _message_content(event, names)[:int(reply_config.get("max_message_chars", 4000))]
            current["mentioned_users"] = _mentioned_people(event, names)
            context = _context(group_id, event.self_id, current)
            logger.info(f"[chat] 群 {group_id} 消息 {event.message_id} 用户 {event.user_id} 开始调用 DeepSeek")
            async with model_slots:
                reply_text = await deepseek_client.chat(messages, context=context)
            # 在同群锁内延迟发送，不收集或合并期间收到的其他提问。
            delay = random.uniform(
                float(reply_config.get("delay_min_seconds", 1)),
                float(reply_config.get("delay_max_seconds", 4)),
            )
            logger.debug(f"[chat] 群 {group_id} 消息 {event.message_id} 发送前等待 {delay:.2f} 秒")
            await asyncio.sleep(delay)
            message = MessageSegment.reply(event.message_id) + MessageSegment.text(reply_text)
            sent = await bot.send(event, message)
            sessions[group_id].append({
                "role": "assistant",
                "group_id": group_id,
                "sequence": current["sequence"],
                "user_id": event.self_id,
                "message_id": sent.get("message_id") if isinstance(sent, dict) else None,
                "reply_to_message_id": event.message_id,
                "reply_to_user_id": event.user_id,
                "content": reply_text,
            })
            # 失败的请求不占用自动回复冷却时间。
            last_reply_time[group_id] = time.monotonic()
            logger.info(f"[chat] 群 {group_id} 消息 {event.message_id} 回复发送成功")
        except Exception as error:
            status = getattr(error, "status_code", None)
            logger.error(
                f"[chat] 群 {group_id} 消息 {event.message_id} 回复失败："
                f"{type(error).__name__}，HTTP状态={status}"
            )
            if at_trigger:
                try:
                    await bot.send(
                        event, MessageSegment.reply(event.message_id)
                        + MessageSegment.text("这条提问暂时处理失败了，请稍后重试。"),
                    )
                except Exception as notify_error:
                    logger.warning(
                        f"[chat] 群 {group_id} 消息 {event.message_id} 失败提示发送失败："
                        f"{type(notify_error).__name__}"
                    )
