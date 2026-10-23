"""通过 DeepSeek 官方 Fetch API 读取公开网页，不在宿主机直接请求目标网站。"""

from dataclasses import dataclass
import ipaddress
import re
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field


class FetchConfig(BaseModel):
    enabled: bool = False
    max_fetches: int = Field(default=2, ge=1, le=3)
    timeout_seconds: float = Field(default=30, ge=1, le=60)
    content_chars: int = Field(default=6000, ge=500, le=20000)


@dataclass(frozen=True)
class FetchResult:
    title: str
    url: str
    content: str
    truncated: bool


class FetchUnavailable(Exception):
    def __init__(self, status_code: int | None = None):
        super().__init__("网页抓取暂不可用")
        self.status_code = status_code


def normalize_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("网页地址必须是字符串")
    url = value.strip()
    # 兼容模型把完整 Markdown 链接误填到参数里，不从任意文本提取网址。
    link = re.fullmatch(r'(?:image)?!?\[[^\]\r\n]*\]\((https?://[^\s)]+)\)', url)
    if link:
        url = link.group(1)
    elif url.startswith('<') and url.endswith('>'):
        url = url[1:-1]
    if not 1 <= len(url) <= 8192 or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("网页地址格式错误")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower().rstrip('.')
        port = parts.port
        if (parts.scheme not in {'http', 'https'} or not host
                or parts.username is not None or parts.password is not None
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError("网页地址格式错误")
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')) or '.' not in host:
            raise ValueError("只能抓取公开网页")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("只能抓取公开网页")
    except ValueError:
        raise ValueError("只支持无账号密码的公开 HTTP/HTTPS 网页地址") from None
    return url


class WebFetchClient:
    endpoint = 'https://api.deepseek.com/v1/tools/fetch'

    def __init__(self, api_key: str, config: FetchConfig):
        self.config = config
        self.http = httpx.AsyncClient(
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=config.timeout_seconds,
            follow_redirects=False,
        )

    async def fetch(self, url: str) -> FetchResult:
        url = normalize_url(url)
        try:
            response = await self.http.post(self.endpoint, json={'url': url})
            response.raise_for_status()
            page = response.json()
            content = page.get('content')
            title = page.get('title')
            if not isinstance(content, str) or not content.strip():
                raise ValueError("网页未返回可读取的正文")
        except httpx.HTTPStatusError as error:
            raise FetchUnavailable(error.response.status_code) from None
        except (httpx.RequestError, ValueError, AttributeError):
            raise FetchUnavailable() from None
        content = content.strip()
        return FetchResult(
            title=' '.join(title.split())[:160] if isinstance(title, str) else '',
            url=url,
            content=content[:self.config.content_chars],
            truncated=len(content) > self.config.content_chars,
        )

    async def close(self):
        await self.http.aclose()


FETCH_TOOL = {
    'type': 'function',
    'function': {
        'name': 'web_fetch',
        'description': (
            '打开一个公开 HTTP/HTTPS 网页，读取标题和正文。'
            '用户提供链接，或搜索摘要不足以回答而需要读取原文时使用。'
            'url 必须是纯网址，不要附带 image、Markdown 或其他文字。'
            '不支持登录、验证码绕过或执行网页脚本；不可读取的内容不能编造。'
        ),
        'parameters': {
            'type': 'object',
            'properties': {'url': {'type': 'string', 'description': '要读取的公开网页的完整 HTTP/HTTPS URL'}},
            'required': ['url'],
            'additionalProperties': False,
        },
    },
}
