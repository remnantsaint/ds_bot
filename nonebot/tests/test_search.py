"""离线验证搜索接口、工具调用边界和群聊隔离；不会请求真实 API 或发送 QQ 消息。"""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import nonebot
from nonebot.adapters.onebot.v11 import MessageSegment
from openai.types.chat import ChatCompletion

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi", log_level="WARNING", deepseek_api_key="test-only", _env_file=None)

from src.deepseek_client import DeepSeekClient, deepseek_client
from src.plugins import chat
from src.search import SearchConfig, SearchResult, SearchUnavailable, WebSearchClient


def response(text=None, calls=None, reasoning=None):
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = calls
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return ChatCompletion(
        id="test", created=0, object="chat.completion", model="test",
        choices=[{"index": 0, "finish_reason": "tool_calls" if calls else "stop", "message": message}],
    )


def tool(query, ident="search-1"):
    return {"id": ident, "type": "function", "function": {"name": "web_search", "arguments": json.dumps({"query": query})}}


def result(label):
    return SearchResult(label, f"https://example.com/{label}", f"{label} 的公开资料", "2026-10-23")


class SearchHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = WebSearchClient("test-only", SearchConfig())

    async def asyncTearDown(self):
        await self.client.close()

    async def transport(self, handler):
        await self.client.http.aclose()
        self.client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_results_are_bounded_deduplicated_and_http_only(self):
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(payload, {"query": "NoneBot", "count": 3, "need_content": False})
            return httpx.Response(200, json={"results": [
                {"title": "A", "url": "https://a.example", "summary": "x" * 4000},
                {"title": "duplicate", "url": "https://a.example"},
                {"url": "javascript:alert(1)"},
                {"url": "https://user:password@b.example"},
                {"title": "B", "url": "https://b.example", "snippet": "摘要"},
                {"title": "C", "url": "https://c.example"},
                {"title": "D", "url": "https://d.example"},
            ]})
        await self.transport(handler)
        rows = await self.client.search(" NoneBot ")
        self.assertEqual([x.title for x in rows], ["A", "B", "C"])
        self.assertEqual(len(rows[0].summary), 1200)
        self.assertEqual(rows[1].summary, "摘要")

    async def test_empty_results_are_not_an_error(self):
        await self.transport(lambda request: httpx.Response(200, json={"results": []}))
        self.assertEqual(await self.client.search("no matches"), [])

    async def test_bad_queries_do_not_make_requests(self):
        with patch.object(self.client.http, "post", new_callable=AsyncMock) as post:
            for query in ["", " ", "x" * 129, None]:
                with self.assertRaises(ValueError):
                    await self.client.search(query)
            post.assert_not_awaited()

    async def test_http_errors_are_sanitized(self):
        await self.transport(lambda request: httpx.Response(402, json={"error": "private details"}))
        with self.assertRaises(SearchUnavailable) as caught:
            await self.client.search("test")
        self.assertEqual(caught.exception.status_code, 402)
        self.assertNotIn("private details", str(caught.exception))

    async def test_timeout_is_reported_without_retry_loop(self):
        def handler(request):
            raise httpx.ReadTimeout("private request", request=request)
        await self.transport(handler)
        with self.assertRaises(SearchUnavailable):
            await self.client.search("test")


class ToolFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = DeepSeekClient()
        self.client.search_config = SearchConfig()
        if self.client.fetch_client is not None:
            await self.client.fetch_client.close()
            self.client.fetch_client = None
        if self.client.search_client is None:
            self.client.search_client = WebSearchClient("test-only", self.client.search_config)
        self.messages = [{"role": "system", "content": "鲸鱼娘人设"}, {"role": "user", "content": "群友: 问题"}]

    async def asyncTearDown(self):
        await self.client.close()

    async def test_casual_reply_does_not_search(self):
        with patch.object(self.client, "_complete", new_callable=AsyncMock, return_value=response("你好")), patch.object(self.client.search_client, "search", new_callable=AsyncMock) as search:
            self.assertEqual(await self.client.chat(self.messages), "你好")
            search.assert_not_awaited()

    async def test_thinking_is_replayed_without_appending_source_footer(self):
        replies = [response(calls=[tool("NoneBot")], reasoning="test reasoning"), response("答案[1]")]
        with patch.object(self.client, "_complete", new_callable=AsyncMock, side_effect=replies) as complete, patch.object(self.client.search_client, "search", new_callable=AsyncMock, return_value=[result("A")]):
            answer = await self.client.chat(self.messages)
            self.assertEqual(answer, "答案[1]")
            self.assertNotIn("来源：", answer)
            self.assertNotIn("https://example.com/A", answer)
            self.assertNotIn("test reasoning", answer)
            later = complete.await_args_list[1]
            self.assertEqual(later.kwargs["tool_choice"], "none")
            assistant = next(m for m in later.args[0] if m["role"] == "assistant")
            self.assertEqual(assistant["reasoning_content"], "test reasoning")

    async def test_parallel_tool_calls_obey_search_budget(self):
        replies = [response(calls=[tool("A", "a"), tool("B", "b")]), response("答案")]
        with patch.object(self.client, "_complete", new_callable=AsyncMock, side_effect=replies) as complete, patch.object(self.client.search_client, "search", new_callable=AsyncMock, return_value=[result("A")]) as search:
            await self.client.chat(self.messages)
            self.assertEqual(search.await_count, 1)
            tools = [m for m in complete.await_args_list[1].args[0] if m["role"] == "tool"]
            self.assertEqual(json.loads(tools[1]["content"])["error"], "search_limit")

    async def test_search_failure_is_explicit_and_has_no_fake_sources(self):
        replies = [response(calls=[tool("A")]), response("暂时无法联网核实")]
        with patch.object(self.client, "_complete", new_callable=AsyncMock, side_effect=replies) as complete, patch.object(self.client.search_client, "search", new_callable=AsyncMock, side_effect=SearchUnavailable(429)):
            answer = await self.client.chat(self.messages)
            self.assertNotIn("来源：", answer)
            tools = [m for m in complete.await_args_list[1].args[0] if m["role"] == "tool"]
            self.assertEqual(json.loads(tools[0]["content"])["error"], "search_unavailable")

    async def test_concurrent_requests_do_not_share_sources(self):
        async def complete(messages, **kwargs):
            context = json.loads(next(m["content"] for m in messages if m["role"] == "user"))
            label = context["conversation"][-1]["content"]
            await asyncio.sleep(0)
            if not any(m["role"] == "tool" for m in messages):
                return response(calls=[tool(label)])
            evidence = next(m for m in messages if m["role"] == "tool")
            return response(json.dumps(json.loads(evidence["content"])["results"]))

        async def search(query):
            await asyncio.sleep(0)
            return [result(query)]

        with patch.object(self.client, "_complete", side_effect=complete), patch.object(self.client.search_client, "search", side_effect=search):
            a, b = await asyncio.gather(
                self.client.chat([{"role": "user", "content": "A"}]),
                self.client.chat([{"role": "user", "content": "B"}]),
            )
        self.assertIn("https://example.com/A", a)
        self.assertNotIn("https://example.com/B", a)
        self.assertIn("https://example.com/B", b)
        self.assertNotIn("https://example.com/A", b)


class GroupIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        delay_policy = patch.dict(chat.reply_config, {"planner_enabled":False,"delay_min_seconds": 0, "delay_max_seconds": 0})
        delay_policy.start()
        self.addCleanup(delay_policy.stop)
        chat.sessions.clear()
        chat.last_reply_time.clear()
        chat.group_locks.clear()
        chat.group_sequences.clear()

    async def asyncTearDown(self):
        chat.sessions.clear()
        chat.last_reply_time.clear()
        chat.group_locks.clear()
        chat.group_sequences.clear()

    @staticmethod
    def event(group_id, text):
        return SimpleNamespace(user_id=2, self_id=1, group_id=group_id, message_id=123, sender=SimpleNamespace(card="", nickname="群友"), message=[MessageSegment.text(text)], get_plaintext=lambda: text, is_tome=lambda: True)

    async def test_different_groups_never_receive_each_others_history(self):
        bot = SimpleNamespace(send=AsyncMock())
        with patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock, side_effect=["A回复", "B回复"]) as generate:
            await chat.handle_group_message(bot, self.event(1001, "A群私有内容"))
            await chat.handle_group_message(bot, self.event(1002, "B群私有内容"))
            a = json.dumps(generate.await_args_list[0].kwargs["context"], ensure_ascii=False)
            b = json.dumps(generate.await_args_list[1].kwargs["context"], ensure_ascii=False)
            self.assertNotIn("B群私有内容", a)
            self.assertNotIn("A群私有内容", b)
            self.assertNotIn("A回复", b)

    async def test_busy_group_does_not_block_another_group(self):
        await chat.group_locks[1001].acquire()
        self.addCleanup(chat.group_locks[1001].release)
        bot = SimpleNamespace(send=AsyncMock())
        with patch.object(chat.deepseek_client, "chat", new_callable=AsyncMock, return_value="B回复") as generate:
            await chat.handle_group_message(bot, self.event(1002, "B群问题"))
            generate.assert_awaited_once()


def tearDownModule():
    asyncio.run(deepseek_client.close())


if __name__ == "__main__":
    unittest.main()
