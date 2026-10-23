"""离线验证网页抓取、工具预算和 DSML 异常恢复，不发送 QQ 消息。"""

import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import nonebot
from openai.types.chat import ChatCompletion

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi", log_level="WARNING", deepseek_api_key="test-only", _env_file=None)

from src.deepseek_client import DeepSeekClient
from src.fetch import FetchConfig, FetchResult, FetchUnavailable, WebFetchClient, normalize_url


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


def call(name, arguments, ident="fetch-1"):
    return {"id": ident, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def page(url="https://www.fflogs.com/character/CN/server/name"):
    return FetchResult("角色页面", url, "已读取的公开角色记录", False)


class FetchHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = WebFetchClient("test-only", FetchConfig(enabled=True, content_chars=500))

    async def asyncTearDown(self):
        await self.client.close()

    async def transport(self, handler):
        await self.client.http.aclose()
        self.client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_official_endpoint_plain_url_and_truncation(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://api.deepseek.com/v1/tools/fetch")
            self.assertEqual(json.loads(request.content), {"url": "https://www.fflogs.com/character/CN/server/name"})
            return httpx.Response(200, json={"title": "角色\n 页面", "content": "x" * 501})
        await self.transport(handler)
        result = await self.client.fetch("image[角色](https://www.fflogs.com/character/CN/server/name)")
        self.assertEqual(result.title, "角色 页面")
        self.assertEqual(len(result.content), 500)
        self.assertTrue(result.truncated)

    async def test_invalid_private_and_credential_urls_never_make_requests(self):
        invalid = ["", None, "file:///etc/passwd", "http://localhost/", "http://127.0.0.1/",
                   "http://10.0.0.1/", "http://169.254.169.254/", "http://[::1]/",
                   "https://user:password@example.com/", "https://example.com:bad/",
                   "https://example.com/a\nb", "https://example.com/" + "x" * 8192,
                   "read https://example.com please"]
        with patch.object(self.client.http, "post", new_callable=AsyncMock) as post:
            for url in invalid:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    await self.client.fetch(url)
            post.assert_not_awaited()

    async def test_markdown_and_angle_links_are_normalized_without_rewriting_path(self):
        target = "https://www.fflogs.com/character/CN/%E6%A2%A6/name?zone=1#boss"
        for value in [target, f"<{target}>", f"[角色]({target})", f"image[{target}]({target})"]:
            self.assertEqual(normalize_url(value), target)

    async def test_http_failure_does_not_disclose_upstream_body(self):
        await self.transport(lambda request: httpx.Response(404, json={"error": "private request details"}))
        with self.assertRaises(FetchUnavailable) as caught:
            await self.client.fetch("https://www.fflogs.com/")
        self.assertEqual(caught.exception.status_code, 404)
        self.assertNotIn("private", str(caught.exception))

    async def test_empty_and_malformed_responses_are_unavailable(self):
        for payload in [{"content": " "}, {"content": None}, {"content": 123}, []]:
            await self.transport(lambda request: httpx.Response(200, json=payload))
            with self.subTest(payload=payload), self.assertRaises(FetchUnavailable):
                await self.client.fetch("https://example.com/")

    async def test_timeout_is_reported_without_hidden_retry(self):
        def handler(request):
            raise httpx.ReadTimeout("private url", request=request)
        await self.transport(handler)
        with self.assertRaises(FetchUnavailable):
            await self.client.fetch("https://example.com/")


class FetchToolFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = DeepSeekClient()
        if self.client.fetch_client is None:
            self.client.fetch_client = WebFetchClient("test-only", FetchConfig(enabled=True))
        self.client.fetch_config = FetchConfig(enabled=True)
        self.messages = [{"role": "system", "content": "原人设"}, {"role": "user", "content": "请查角色日志"}]

    async def asyncTearDown(self):
        await self.client.close()

    async def test_search_then_fetch_then_answer_preserves_reasoning_and_tool_schema(self):
        from src.search import SearchResult
        replies = [response(calls=[call("web_search", {"query": "fflogs 角色"}, "search-1")], reasoning="reason A"),
                   response(calls=[call("web_fetch", {"url": page().url})], reasoning="reason B"),
                   response("根据角色页面给出回答")]
        with patch.object(self.client, "_complete", new_callable=AsyncMock, side_effect=replies) as complete, \
             patch.object(self.client.search_client, "search", new_callable=AsyncMock, return_value=[SearchResult("角色", page().url, "摘要", "")]) as search, \
             patch.object(self.client.fetch_client, "fetch", new_callable=AsyncMock, return_value=page()) as fetch:
            answer = await self.client.chat(self.messages)
        self.assertEqual(answer, "根据角色页面给出回答")
        search.assert_awaited_once()
        fetch.assert_awaited_once_with(page().url)
        self.assertEqual(complete.await_args_list[1].kwargs["tool_choice"], "auto")
        for entry in complete.await_args_list:
            self.assertEqual({t["function"]["name"] for t in entry.kwargs["tools"]}, {"web_search", "web_fetch"})
        assistants = [m for m in complete.await_args_list[-1].args[0] if m["role"] == "assistant"]
        self.assertEqual([m["reasoning_content"] for m in assistants], ["reason A", "reason B"])

    async def test_direct_fetch_works_with_search_disabled(self):
        if self.client.search_client is not None:
            await self.client.search_client.close()
            self.client.search_client = None
        replies = [response(calls=[call("web_fetch", {"url": page().url})]), response("页面内容")]
        with patch.object(self.client, "_complete", new_callable=AsyncMock, side_effect=replies) as complete, \
             patch.object(self.client.fetch_client, "fetch", new_callable=AsyncMock, return_value=page()) as fetch:
            self.assertEqual(await self.client.chat(self.messages), "页面内容")
        fetch.assert_awaited_once()
        self.assertEqual([t["function"]["name"] for t in complete.await_args.kwargs["tools"]], ["web_fetch"])

    async def test_parallel_calls_respect_fetch_budget(self):
        calls = [call("web_fetch", {"url": f"https://example.com/{i}"}, f"f{i}") for i in range(3)]
        replies = [response(calls=calls), response("已读取两页")]
        with patch.object(self.client, "_complete", new_callable=AsyncMock, side_effect=replies) as complete, \
             patch.object(self.client.fetch_client, "fetch", new_callable=AsyncMock, return_value=page()) as fetch:
            await self.client.chat(self.messages)
        self.assertEqual(fetch.await_count, 2)
        results = [json.loads(m["content"]) for m in complete.await_args.args[0] if m["role"] == "tool"]
        self.assertEqual(results[-1]["error"], "fetch_limit")

    async def test_fetch_unavailable_is_reported_to_model(self):
        replies = [response(calls=[call("web_fetch", {"url": page().url})]), response("目前读不到具体记录")]
        with patch.object(self.client, "_complete", new_callable=AsyncMock, side_effect=replies) as complete, \
             patch.object(self.client.fetch_client, "fetch", new_callable=AsyncMock, side_effect=FetchUnavailable(404)):
            self.assertEqual(await self.client.chat(self.messages), "目前读不到具体记录")
        data = next(json.loads(m["content"]) for m in complete.await_args.args[0] if m["role"] == "tool")
        self.assertEqual(data["error"], "fetch_unavailable")
        self.assertNotIn("content", data)

    async def test_dsml_is_retried_as_native_tool_call_and_never_replayed(self):
        for marker in ["<｜DSML｜ calls>", "<｜｜DSML｜｜ calls>", "<|DSML|tool_calls>", '<invoke name="web_fetch">']:
            replies = [response(marker + "broken"), response(calls=[call("web_fetch", {"url": page().url})]), response("正常回答")]
            with self.subTest(marker=marker), \
                 patch.object(self.client, "_complete", new_callable=AsyncMock, side_effect=replies) as complete, \
                 patch.object(self.client.fetch_client, "fetch", new_callable=AsyncMock, return_value=page()) as fetch:
                self.assertEqual(await self.client.chat(self.messages), "正常回答")
                fetch.assert_awaited_once()
                self.assertNotIn(marker + "broken", json.dumps(complete.await_args.args[0], ensure_ascii=False))

    async def test_repeated_dsml_fails_after_one_retry_without_executing_text(self):
        with patch.object(self.client, "_complete", new_callable=AsyncMock, return_value=response('<｜DSML｜ invoke name="web_fetch">')) as complete, \
             patch.object(self.client.fetch_client, "fetch", new_callable=AsyncMock) as fetch:
            with self.assertRaisesRegex(ValueError, "异常工具标记"):
                await self.client.chat(self.messages)
            self.assertEqual(complete.await_count, 2)
            fetch.assert_not_awaited()

    async def test_dsml_is_guarded_even_when_both_tools_disabled(self):
        for name in ["search_client", "fetch_client"]:
            client = getattr(self.client, name)
            if client is not None:
                await client.close()
                setattr(self.client, name, None)
        with patch.object(self.client, "_complete", new_callable=AsyncMock, side_effect=[response('<｜DSML｜ calls>'), response('无法联网读取')]) as complete:
            self.assertEqual(await self.client.chat(self.messages), '无法联网读取')
            self.assertNotIn('tools', complete.await_args.kwargs)

    async def test_markup_alongside_native_call_is_not_replayed(self):
        replies = [response('<｜DSML｜ calls>', calls=[call('web_fetch', {'url': page().url})]), response('答案')]
        with patch.object(self.client, '_complete', new_callable=AsyncMock, side_effect=replies) as complete, \
             patch.object(self.client.fetch_client, 'fetch', new_callable=AsyncMock, return_value=page()):
            await self.client.chat(self.messages)
        assistant = next(m for m in complete.await_args.args[0] if m['role'] == 'assistant')
        self.assertIsNone(assistant['content'])

    async def test_unknown_tool_and_invalid_arguments_do_not_fetch(self):
        calls = [call('get_image', {'url': page().url}, 'unknown'),
                 call('web_fetch', {'url': page().url, 'extra': True}, 'invalid')]
        with patch.object(self.client, '_complete', new_callable=AsyncMock, side_effect=[response(calls=calls), response('无法处理')]) as complete, \
             patch.object(self.client.fetch_client, 'fetch', new_callable=AsyncMock) as fetch:
            await self.client.chat(self.messages)
        fetch.assert_not_awaited()
        errors = [json.loads(m['content'])['error'] for m in complete.await_args.args[0] if m['role'] == 'tool']
        self.assertEqual(errors, ['unknown_tool', 'invalid_url'])


if __name__ == '__main__':
    unittest.main()
