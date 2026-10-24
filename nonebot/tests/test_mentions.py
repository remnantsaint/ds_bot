"""验证 @ 目标、名片回退和查名片期间的排队；不请求真实 QQ/API。"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
from nonebot.adapters.onebot.v11 import Message, MessageSegment as Segment

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi", log_level="WARNING", deepseek_api_key="test-only", _env_file=None)

from src.plugins import chat


def event(ident, parts, *, group_id=1001, user_id=2, to_me=False):
    message = Message(parts)
    return SimpleNamespace(
        group_id=group_id, user_id=user_id, self_id=1, message_id=ident,
        sender=SimpleNamespace(card="", nickname="提问者"), message=message,
        reply=None, get_plaintext=message.extract_plain_text, is_tome=lambda: to_me,
    )


class MentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        chat.sessions.clear()
        chat.group_locks.clear()
        chat.group_sequences.clear()
        chat.last_reply_time.clear()
        policy = patch.dict(chat.reply_config, {
            "planner_enabled": False, "probability": 0, "keywords": ["大肥鱼"], "reply_on_at": True,
            "cooldown_seconds": 300, "delay_min_seconds": 0, "delay_max_seconds": 0,
        })
        policy.start()
        self.addCleanup(policy.stop)
        self.bot = SimpleNamespace(
            send=AsyncMock(return_value={"message_id": 9000}),
            get_group_member_info=AsyncMock(return_value={
                "group_id": 1001, "user_id": 3, "card": "笑笑", "nickname": "原昵称",
            }),
        )

    async def asyncTearDown(self):
        chat.sessions.clear()
        chat.group_locks.clear()
        chat.group_sequences.clear()
        chat.last_reply_time.clear()

    async def test_actual_at_target_is_retained_with_card_and_qq_id(self):
        msg = event(10, [Segment.at(1), Segment.text("锐评一下"), Segment.at(3)])
        with patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock, return_value="回答") as generate:
            await chat.handle_group_message(self.bot, msg)
        current = generate.await_args.kwargs["context"]["current_request"]
        self.assertEqual(current["content"], "锐评一下@笑笑（QQ:3）")
        self.assertEqual(current["mentioned_users"], [{"user_id": "3", "nickname": "笑笑"}])
        self.assertEqual(current["user_id"], 2)
        self.bot.get_group_member_info.assert_awaited_once_with(group_id=1001, user_id=3, no_cache=False)

    async def test_multiple_mentions_keep_positions_and_distinct_same_names(self):
        self.bot.get_group_member_info.side_effect = [
            {"group_id": 1001, "user_id": 3, "card": "同名"},
            {"group_id": 1001, "user_id": 4, "nickname": "同名"},
        ]
        msg = event(11, [Segment.text("比较"), Segment.at(3), Segment.text("和"), Segment.at(4)], to_me=True)
        with patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock, return_value="回答") as generate:
            await chat.handle_group_message(self.bot, msg)
        current = generate.await_args.kwargs["context"]["current_request"]
        self.assertEqual(current["content"], "比较@同名（QQ:3）和@同名（QQ:4）")
        self.assertEqual([p["user_id"] for p in current["mentioned_users"]], ["3", "4"])

    async def test_lookup_failure_keeps_target_id_instead_of_dropping_it(self):
        self.bot.get_group_member_info.side_effect = RuntimeError("private details")
        msg = event(12, [Segment.at(1), Segment.text("评价"), Segment.at(3)])
        with patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock, return_value="回答") as generate:
            await chat.handle_group_message(self.bot, msg)
        current = generate.await_args.kwargs["context"]["current_request"]
        self.assertEqual(current["content"], "评价@QQ:3")
        self.assertEqual(current["mentioned_user_ids"], ["1", "3"])

    async def test_only_at_target_is_not_discarded_as_empty(self):
        with patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock, return_value="回答") as generate:
            await chat.handle_group_message(self.bot, event(13, [Segment.at(1), Segment.at(3)]))
        self.assertEqual(generate.await_args.kwargs["context"]["current_request"]["content"], "@笑笑（QQ:3）")

    async def test_all_members_is_not_treated_as_one_person_or_queried(self):
        with patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock, return_value="回答") as generate:
            await chat.handle_group_message(self.bot, event(14, [Segment.at(1), Segment.text("通知"), Segment.at("all")]))
        current = generate.await_args.kwargs["context"]["current_request"]
        self.assertEqual(current["content"], "通知@全体成员")
        self.assertEqual(current["mentioned_users"], [])
        self.bot.get_group_member_info.assert_not_awaited()

    async def test_history_names_are_local_to_the_group(self):
        for group_id, name in [(1001, "本群名字"), (1002, "其他群名字")]:
            chat.sessions[group_id].append({"role": "user", "group_id": group_id, "user_id": 3,
                                           "nickname": name, "content": "历史发言", "sequence": 0})
        msg = event(15, [Segment.at(1), Segment.text("评价"), Segment.at(3)])
        with patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock, return_value="回答") as generate:
            await chat.handle_group_message(self.bot, msg)
        current = generate.await_args.kwargs["context"]["current_request"]
        self.assertEqual(current["content"], "评价@本群名字（QQ:3）")
        self.bot.get_group_member_info.assert_not_awaited()

    async def test_slow_name_lookup_does_not_let_next_question_overtake(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def member(**kwargs):
            entered.set()
            await release.wait()
            return {"group_id": 1001, "user_id": 3, "card": "笑笑"}
        tasks = []
        with patch.object(self.bot, "get_group_member_info", side_effect=member), \
             patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock, return_value="回答") as generate:
            try:
                tasks.append(asyncio.create_task(chat.handle_group_message(self.bot,
                    event(20, [Segment.at(1), Segment.text("评价"), Segment.at(3)]))))
                await asyncio.wait_for(entered.wait(), 1)
                tasks.append(asyncio.create_task(chat.handle_group_message(self.bot,
                    event(21, [Segment.at(1), Segment.text("下一问")]))))
                await asyncio.sleep(0)
                generate.assert_not_awaited()
                release.set()
                await asyncio.wait_for(asyncio.gather(*tasks), 2)
            finally:
                for task in tasks:
                    if not task.done(): task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        contexts = [call.kwargs["context"] for call in generate.await_args_list]
        self.assertEqual([c["current_request"]["message_id"] for c in contexts], [20, 21])
        self.assertEqual(contexts[0]["current_request"]["content"], "评价@笑笑（QQ:3）")
        self.assertNotIn("下一问", str(contexts[0]))

    async def test_disabled_probability_ignores_ordinary_chat_but_keeps_background(self):
        with patch.object(chat.random, "random", return_value=0) as random_draw, \
             patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock) as generate:
            await chat.handle_group_message(self.bot, event(30, [Segment.text("普通闲聊")]))
        generate.assert_not_awaited()
        random_draw.assert_not_called()
        self.assertEqual(chat.sessions[1001][-1]["content"], "普通闲聊")


if __name__ == "__main__":
    unittest.main()
