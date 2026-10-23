import asyncio
import random
import time
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

group_chat = on_message(priority=10, block=False)


def _context(group_id: int, bot_id: int, current: dict) -> dict:
    # 排队期间可能又收到消息，只取当前提问之前的本群记录。
    # assistant 的 sequence 对应它回答的问题，而非回答发送的时间。
    history = [
        dict(item) for item in sessions[group_id]
        if item["sequence"] < current["sequence"]
    ]
    history.sort(key=lambda item: (item["sequence"], item["role"] == "assistant"))
    return {
        "group_id": group_id,
        "bot_user_id": bot_id,
        "conversation": history,
        "current_request": dict(current),
    }


@group_chat.handle()
async def handle_group_message(bot: Bot, event: GroupMessageEvent):
    if event.user_id == event.self_id:
        return

    text = event.get_plaintext().strip()
    images = [seg for seg in event.message if seg.type == "image"]
    if not text and not images:
        return

    group_id = event.group_id
    group_sequences[group_id] += 1
    mentioned_users = [
        str(segment.data.get("qq"))
        for segment in event.message if segment.type == "at"
    ]
    is_at_bot = event.is_tome() or str(event.self_id) in mentioned_users
    quoted = getattr(event, "reply", None)
    current = {
        "role": "user",
        "group_id": group_id,
        "sequence": group_sequences[group_id],
        "message_id": event.message_id,
        "user_id": event.user_id,
        "nickname": event.sender.card or event.sender.nickname or "群友",
        "content": text,
        "mentions_bot": is_at_bot,
        "mentioned_user_ids": mentioned_users,
        "reply_to_message_id": getattr(quoted, "message_id", None),
    }
    sessions[group_id].append(current)

    at_trigger = is_at_bot and reply_config.get("reply_on_at", True)
    lock = group_locks[group_id]
    # 普通消息仍记作背景；只有明确 @ 的请求在忙碌时排队。
    if not at_trigger and lock.locked():
        logger.debug(f"[chat] 群 {group_id} 消息 {event.message_id}：忙碌中，仅记录背景")
        return

    keyword_trigger = any(
        keyword and keyword in text for keyword in reply_config.get("keywords", [])
    )
    if not (at_trigger or keyword_trigger
            or random.random() < float(reply_config["probability"])):
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

        context = _context(group_id, event.self_id, current)
        messages = [{"role": "system", "content": config["persona"]}]
        try:
            logger.info(f"[chat] 群 {group_id} 消息 {event.message_id} 用户 {event.user_id} 开始调用 DeepSeek")
            reply_text = await deepseek_client.chat(messages, context=context)
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
