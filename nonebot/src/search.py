"""DeepSeek 官方搜索接口；搜索结果只作为当前请求的参考资料。"""

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field


class SearchConfig(BaseModel):
    enabled: bool = True
    max_results: int = Field(default=3, ge=1, le=5)
    max_searches: int = Field(default=1, ge=1, le=3)
    timeout_seconds: float = Field(default=15, ge=1, le=60)
    summary_chars: int = Field(default=1200, ge=100, le=3000)


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    summary: str
    date: str


class SearchUnavailable(Exception):
    def __init__(self, status_code: int | None = None):
        super().__init__("联网搜索暂不可用")
        self.status_code = status_code


class WebSearchClient:
    endpoint = "https://api.deepseek.com/v1/tools/search"

    def __init__(self, api_key: str, config: SearchConfig):
        self.config = config
        self.http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=config.timeout_seconds,
            follow_redirects=False,
        )

    async def search(self, query: str) -> list[SearchResult]:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 128:
            raise ValueError("搜索词须为 1 到 128 个字符")
        try:
            response = await self.http.post(
                self.endpoint,
                json={
                    "query": query.strip(),
                    "count": self.config.max_results,
                    "need_content": False,
                },
            )
            response.raise_for_status()
            items = response.json().get("results")
            if not isinstance(items, list):
                raise ValueError("搜索响应格式错误")
        except httpx.HTTPStatusError as error:
            raise SearchUnavailable(error.response.status_code) from None
        except (httpx.RequestError, ValueError, AttributeError):
            raise SearchUnavailable() from None

        results = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or len(url) > 8192:
                continue
            try:
                parsed = urlsplit(url)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or any(ord(char) < 32 for char in url)
                ):
                    continue
            except ValueError:
                continue
            if url in seen:
                continue
            seen.add(url)

            def clean(value, limit):
                return " ".join(value.split())[:limit] if isinstance(value, str) else ""

            results.append(SearchResult(
                title=clean(item.get("title"), 160) or "网页",
                url=url,
                summary=clean(item.get("summary") or item.get("snippet"), self.config.summary_chars),
                date=clean(item.get("date"), 64),
            ))
            if len(results) >= self.config.max_results:
                break
        return results

    async def close(self):
        await self.http.aclose()


SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "搜索公开网页。问题涉及最新信息、时效性事实，或需要核实且已有知识不足时使用；"
            "闲聊、角色互动、改写或不需要联网的推理题通常不使用。"
            "只发送回答所需的公开搜索词，不发送群聊原文、个人隐私、密钥或整段聊天记录。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "1 到 128 个字符的公开网页搜索词"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}
