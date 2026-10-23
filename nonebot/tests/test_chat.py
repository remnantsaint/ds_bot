"""离线测试排队、回复对象和群聊隔离，不请求 API 或连接 QQ。"""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi", log_level="WARNING", deepseek_api_key="test-only", _env_file=None)

from nonebot.adapters.onebot.v11 import MessageSegment
from src.deepseek_client import DeepSeekClient
from src.plugins import chat


def event(message_id, text="问题", *, group_id=1001, user_id=2, at=True,
          quoted_id=None, mentions=None, to_me=None):
    return SimpleNamespace(
        message_id=message_id, group_id=group_id, user_id=user_id, self_id=1,
        sender=SimpleNamespace(card="", nickname="同名群友"),
        message=([MessageSegment.at(1)] if at else []) + (mentions or []) + [MessageSegment.text(text)],
        reply=SimpleNamespace(message_id=quoted_id) if quoted_id is not None else None,
        get_plaintext=lambda: text, is_tome=lambda: at if to_me is None else to_me,
    )


class GroupQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_answer_uses_latest_persona_and_read_failure_skips_model(self):
        with patch.object(chat, "load_persona", side_effect=["旧人设", "新人设", ValueError("人设为空")]), \
             patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock, return_value="答案") as model:
            await chat.handle_group_message(self.bot, event(901))
            await chat.handle_group_message(self.bot, event(902))
            await chat.handle_group_message(self.bot, event(903))
            self.assertEqual(model.await_count, 2)
            self.assertEqual(model.await_args_list[0].args[0][0]["content"], "旧人设")
            self.assertEqual(model.await_args_list[1].args[0][0]["content"], "新人设")

    async def test_full_queue_keeps_background_and_does_not_start_cooldown(self):
        chat.pending_requests[1001] = 5
        try:
            with patch.dict(chat.reply_config, {"user_cooldown_seconds": 3}):
                await chat.handle_group_message(self.bot, event(801, "背景", at=False))
                await chat.handle_group_message(self.bot, event(802))
            self.assertEqual(len(chat.sessions[1001]), 2)
            self.assertNotIn((1001, 2), chat.user_request_times)
        finally:
            chat.pending_requests.clear()

    def test_current_metadata_cannot_exceed_budget(self):
        with patch.dict(chat.reply_config, {"max_context_chars": 100}):
            with self.assertRaises(ValueError):
                chat._context(1001, 1, {"sequence": 1, "nickname": "x" * 200})

    async def test_cooldown_does_not_block_another_user(self):
        with patch.dict(chat.reply_config, {"user_cooldown_seconds": 3}), \
             patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock, return_value="答案") as model:
            await chat.handle_group_message(self.bot, event(803, user_id=2))
            await chat.handle_group_message(self.bot, event(804, user_id=3))
            self.assertEqual(model.await_count, 2)

    async def test_cross_group_slot_is_released_on_cancellation(self):
        entered = asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(chat, "model_slots", asyncio.Semaphore(1)), \
             patch.object(chat.deepseek_client, "chat", side_effect=slow) as model:
            first = self.start(event(805, group_id=1001))
            await asyncio.wait_for(entered.wait(), 1)
            second = self.start(event(806, group_id=1002))
            await asyncio.sleep(0.02)
            self.assertEqual(model.await_count, 1)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            for _ in range(20):
                if model.await_count == 2:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(model.await_count, 2)
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
            self.assertFalse(chat.pending_requests)

    async def test_user_cooldown_blocks_repeated_request(self):
        chat.user_request_times.clear()
        try:
            with patch.dict(chat.reply_config, {"user_cooldown_seconds": 3}), \
                 patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock, return_value="答案") as model:
                await chat.handle_group_message(self.bot, event(710))
                await chat.handle_group_message(self.bot, event(711))
                self.assertEqual(model.await_count, 1)
        finally:
            chat.user_request_times.clear()

    def test_context_budget_removes_oversized_history(self):
        chat.sessions[1001].append({"role": "user", "sequence": 1, "content": "x" * 1000})
        with patch.dict(chat.reply_config, {"max_context_chars": 100}):
            context = chat._context(1001, 1, {"sequence": 2, "content": "当前问题"})
        self.assertEqual(context["conversation"], [])

    def test_invalid_limits_fail_validation(self):
        from src.utils import ReplyLimits
        for values in ({"max_concurrent_requests": 0}, {"max_pending_per_group": -1},
                       {"delay_min_seconds": 5, "delay_max_seconds": 1}):
            with self.assertRaises(ValueError):
                ReplyLimits.model_validate(values)

    async def test_full_queue_does_not_call_model(self):
        chat.pending_requests[1001] = 5
        try:
            with patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock) as model:
                await chat.handle_group_message(self.bot, event(700))
                model.assert_not_awaited()
                self.assertIn("当前提问较多", str(self.bot.send.call_args.args[1]))
        finally:
            chat.pending_requests.clear()

    async def test_total_timeout_releases_group_lock_and_counter(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(10)
        with patch.dict(chat.reply_config, {"request_timeout_seconds": 0.02}), \
             patch.object(chat.deepseek_client, "chat", side_effect=slow):
            await chat.handle_group_message(self.bot, event(701))
        self.assertFalse(chat.group_locks[1001].locked())
        self.assertNotIn(1001, chat.pending_requests)
        self.assertIn("超时", str(self.bot.send.call_args.args[1]))

    async def asyncSetUp(self):
        self.reset()
        self.policy = patch.dict(chat.reply_config, {
            "probability": 0, "keywords": ["大肥鱼"],
            "user_cooldown_seconds": 0,
            "reply_on_at": True, "cooldown_seconds": 300, "max_history": 20,
            "delay_min_seconds": 0, "delay_max_seconds": 0,
        })
        self.policy.start()
        self.bot = SimpleNamespace(send=AsyncMock(return_value={"message_id": 9000}))
        self.tasks = []

    @staticmethod
    def reset():
        chat.user_request_times.clear()
        chat.sessions.clear()
        chat.last_reply_time.clear()
        chat.group_locks.clear()
        chat.group_sequences.clear()

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.policy.stop()
        self.reset()

    def start(self, evt):
        task = asyncio.create_task(chat.handle_group_message(self.bot, evt))
        self.tasks.append(task)
        return task

    async def finish(self):
        await asyncio.wait_for(asyncio.gather(*self.tasks), timeout=3)

    async def test_consecutive_mentions_are_fifo_and_keep_reply_targets(self):
        entered, release = asyncio.Event(), asyncio.Event()
        contexts = []

        async def generate(messages, *, context):
            contexts.append(context)
            if len(contexts) == 1:
                entered.set()
                await release.wait()
            return "回答" + str(context["current_request"]["message_id"])

        with patch.object(chat.deepseek_client, "chat", side_effect=generate):
            self.start(event(10, "第一问"))
            await asyncio.wait_for(entered.wait(), 1)
            self.start(event(11, "第二问", user_id=3))
            await asyncio.sleep(0)
            self.start(event(12, "第三问"))
            await asyncio.sleep(0)
            self.assertEqual(len(contexts), 1)
            release.set()
            await self.finish()

        self.assertEqual([c["current_request"]["message_id"] for c in contexts], [10, 11, 12])
        self.assertNotIn("第二问", json.dumps(contexts[0], ensure_ascii=False))
        self.assertNotIn("第三问", json.dumps(contexts[1], ensure_ascii=False))
        for expected, call in zip([10, 11, 12], self.bot.send.await_args_list):
            self.assertEqual(int(call.args[1][0].data["id"]), expected)
            self.assertEqual(call.args[1][1].data["text"], f"回答{expected}")
        self.assertEqual(self.bot.send.await_count, 3)
        previous = [m for m in contexts[1]["conversation"] if m["role"] == "assistant"]
        self.assertEqual(previous[0]["reply_to_message_id"], 10)
        self.assertEqual(previous[0]["reply_to_user_id"], 2)
        self.assertEqual(contexts[1]["current_request"]["user_id"], 3)

    async def test_background_is_separate_and_does_not_replace_queued_target(self):
        entered, release = asyncio.Event(), asyncio.Event()
        contexts = []

        async def generate(messages, *, context):
            contexts.append(context)
            if len(contexts) == 1:
                entered.set()
                await release.wait()
            return "回答当前问题"

        with patch.object(chat.deepseek_client, "chat", side_effect=generate):
            self.start(event(20, "甲的编程问题"))
            await asyncio.wait_for(entered.wait(), 1)
            await chat.handle_group_message(self.bot, event(21, "乙在聊天气", user_id=3, at=False))
            self.start(event(22, "甲的第二问"))
            await asyncio.sleep(0)
            await chat.handle_group_message(self.bot, event(23, "乙的后续闲聊", user_id=3, at=False))
            release.set()
            await self.finish()

        self.assertEqual(len(contexts), 2)
        self.assertEqual(contexts[1]["current_request"]["message_id"], 22)
        self.assertEqual(contexts[1]["current_request"]["content"], "甲的第二问")
        background = json.dumps(contexts[1]["conversation"], ensure_ascii=False)
        self.assertIn("乙在聊天气", background)
        self.assertNotIn("乙的后续闲聊", background)
        answer = next(m for m in contexts[1]["conversation"] if m["role"] == "assistant")
        self.assertEqual(answer["reply_to_message_id"], 20)
        self.assertEqual(answer["reply_to_user_id"], 2)
        self.assertNotIn("乙在聊天气", json.dumps(contexts[0], ensure_ascii=False))

    async def test_different_groups_run_concurrently_without_shared_context(self):
        ready = {1001: asyncio.Event(), 1002: asyncio.Event()}
        release = asyncio.Event()
        contexts = []

        async def generate(messages, *, context):
            contexts.append(context)
            ready[context["group_id"]].set()
            await release.wait()
            return "本群回答"

        with patch.object(chat.deepseek_client, "chat", side_effect=generate):
            self.start(event(30, "A群专属内容", group_id=1001))
            self.start(event(31, "B群专属内容", group_id=1002))
            await asyncio.wait_for(asyncio.gather(*(x.wait() for x in ready.values())), 1)
            release.set()
            await self.finish()

        for context in contexts:
            foreign = "B群专属内容" if context["group_id"] == 1001 else "A群专属内容"
            self.assertNotIn(foreign, json.dumps(context, ensure_ascii=False))

    async def test_queued_target_survives_history_eviction(self):
        entered, release = asyncio.Event(), asyncio.Event()
        contexts = []

        async def generate(messages, *, context):
            contexts.append(context)
            if len(contexts) == 1:
                entered.set()
                await release.wait()
            return "回答"

        with patch.dict(chat.reply_config, {"max_history": 2}), patch.object(chat.deepseek_client, "chat", side_effect=generate):
            self.start(event(40))
            await asyncio.wait_for(entered.wait(), 1)
            self.start(event(41, "不能丢失的提问"))
            await asyncio.sleep(0)
            for ident in range(42, 48):
                await chat.handle_group_message(self.bot, event(ident, "闲聊", at=False))
            self.assertNotIn(41, [m["message_id"] for m in chat.sessions[1001]])
            release.set()
            await self.finish()
        self.assertEqual(contexts[1]["current_request"]["content"], "不能丢失的提问")
        self.assertEqual(self.bot.send.await_count, 2)

    async def test_model_failure_does_not_consume_cooldown_or_block_next_request(self):
        with patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock,
                          side_effect=[RuntimeError("private error"), "成功"]) as generate:
            await chat.handle_group_message(self.bot, event(50))
            self.assertNotIn(1001, chat.last_reply_time)
            self.assertFalse(chat.group_locks[1001].locked())
            # 随机回复应能紧接失败请求触发，不能被失败请求的冷却挡住。
            with patch.dict(chat.reply_config, {"probability": 1}):
                await chat.handle_group_message(self.bot, event(51, at=False))
            self.assertEqual(generate.await_count, 2)
        self.assertIn(1001, chat.last_reply_time)
        answers = [m for m in chat.sessions[1001] if m["role"] == "assistant"]
        self.assertEqual([m["reply_to_message_id"] for m in answers], [51])
        notice = str(self.bot.send.await_args_list[0].args[1])
        self.assertIn("暂时处理失败", notice)
        self.assertNotIn("private error", notice)

    async def test_send_failure_does_not_record_a_successful_answer(self):
        self.bot.send.side_effect = [RuntimeError("send failed"), None]
        with patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock, return_value="回答"):
            await chat.handle_group_message(self.bot, event(60))
        self.assertNotIn(1001, chat.last_reply_time)
        self.assertFalse(any(m["role"] == "assistant" for m in chat.sessions[1001]))
        self.assertFalse(chat.group_locks[1001].locked())

    async def test_cancellation_releases_the_group_queue(self):
        entered = asyncio.Event()

        async def generate(messages, *, context):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(chat.deepseek_client, "chat", side_effect=generate):
            task = self.start(event(70))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(chat.group_locks[1001].locked())
        with patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock, return_value="恢复"):
            await chat.handle_group_message(self.bot, event(71))
        self.assertEqual(self.bot.send.await_count, 1)

    async def test_random_and_keyword_replies_obey_cooldown_mentions_bypass_it(self):
        with patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock, return_value="回答") as generate:
            await chat.handle_group_message(self.bot, event(80, "大肥鱼", at=False))
            with patch.dict(chat.reply_config, {"probability": 1}):
                await chat.handle_group_message(self.bot, event(81, at=False))
            await chat.handle_group_message(self.bot, event(82, "大肥鱼", at=False))
            self.assertEqual(generate.await_count, 1)
            await chat.handle_group_message(self.bot, event(83))
            self.assertEqual(generate.await_count, 2)

    async def test_busy_automatic_reply_is_not_queued(self):
        await chat.group_locks[1001].acquire()
        try:
            with patch.dict(chat.reply_config, {"probability": 1}), patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock) as generate:
                await asyncio.wait_for(chat.handle_group_message(self.bot, event(90, at=False)), 1)
                generate.assert_not_awaited()
        finally:
            chat.group_locks[1001].release()
        self.assertEqual(chat.sessions[1001][0]["message_id"], 90)

    async def test_explicit_mention_is_detected_and_quoted_message_is_preserved(self):
        with patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock, return_value="回答") as generate:
            await chat.handle_group_message(self.bot, event(100, at=False, to_me=False,
                quoted_id=99, mentions=[MessageSegment.at(1)]))
        current = generate.await_args.kwargs["context"]["current_request"]
        self.assertTrue(current["mentions_bot"])
        self.assertEqual(current["reply_to_message_id"], 99)


class RequestContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = DeepSeekClient()
        self.context = {
            "group_id": 1001, "bot_user_id": 1,
            "current_request": {"role": "user", "group_id": 1001, "message_id": 20,
                                "user_id": 2, "content": "当前问题"},
            "conversation": [{"role": "user", "group_id": 1001, "message_id": 19,
                              "user_id": 3, "content": "其他成员的闲聊"}],
        }
        self.messages = [{"role": "system", "content": "测试人设"}]

    async def asyncTearDown(self):
        await self.client.close()

    async def test_context_and_persona_are_preserved_with_search_enabled_or_disabled(self):
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="仅回答当前问题", tool_calls=None))])
        for enabled in [False, True]:
            with self.subTest(search_enabled=enabled):
                search_client = self.client.search_client if enabled else None
                with patch.object(self.client, "search_client", search_client), patch.object(
                    self.client, "_complete", new_callable=AsyncMock, return_value=response,
                ) as complete:
                    self.assertEqual(await self.client.chat(self.messages, context=self.context), "仅回答当前问题")
                sent = complete.await_args.args[0]
                self.assertEqual(sent[0], self.messages[0])
                payload = json.loads(next(m["content"] for m in sent if m["role"] == "user"))
                self.assertEqual(payload, self.context)
                self.assertTrue(all(set(m) == {"role", "content"} for m in sent))

    async def test_cross_group_context_is_rejected_before_api_call(self):
        for position in ["current", "history"]:
            with self.subTest(position=position):
                context = json.loads(json.dumps(self.context))
                record = context["current_request"] if position == "current" else context["conversation"][0]
                record["group_id"] = 1002
                with patch.object(self.client, "_complete", new_callable=AsyncMock) as complete:
                    with self.assertRaisesRegex(ValueError, "其他群"):
                        await self.client.chat(self.messages, context=context)
                    complete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
