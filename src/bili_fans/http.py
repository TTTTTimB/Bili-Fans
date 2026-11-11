"""极简 HTTP 层：只用标准库，带 UA / Referer / Cookie、gzip 与退避重试。"""

from __future__ import annotations

import gzip
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 这些状态码值得等一会儿再试
RETRY_STATUS = frozenset({412, 418, 429, 500, 502, 503, 504})


class HttpError(Exception):
    """请求失败。status 为 HTTP 状态码，body 为已解码的响应文本。"""

    def __init__(self, message: str, *, status: int | None = None, url: str = "", body: str = ""):
        super().__init__(message)
        self.status = status
        self.url = url
        self.body = body


def _decompress(raw: bytes, encoding: str) -> bytes:
    if encoding.lower().strip() == "gzip":
        try:
            return gzip.decompress(raw)
        except OSError:  # 少数情况下服务器没真的压缩
            return raw
    return raw


def _default_backend(req: urllib.request.Request, timeout: float) -> tuple[int, bytes, Mapping[str, str]]:
    """返回 (状态码, body, headers)。4xx/5xx 也返回而不抛异常，交给上层判断。"""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, _decompress(raw, resp.headers.get("Content-Encoding", "")), resp.headers
    except urllib.error.HTTPError as exc:  # 4xx / 5xx 仍可能带着有用的 JSON
        raw = exc.read() or b""
        return exc.code, _decompress(raw, exc.headers.get("Content-Encoding", "") or ""), exc.headers
    except urllib.error.URLError as exc:
        raise HttpError(f"网络错误：{exc.reason}", url=req.full_url) from exc
    except TimeoutError as exc:
        raise HttpError(f"请求超时：{req.full_url}", url=req.full_url) from exc


class Transport:
    """所有出网请求的唯一出口，方便测试时替换 backend。"""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        retries: int = 3,
        backoff: float = 0.8,
        headers: Mapping[str, str] | None = None,
        cookie: str = "",
        backend: Callable[[urllib.request.Request, float], tuple[int, bytes, Mapping[str, str]]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.backoff = backoff
        self.headers = {
            "User-Agent": DEFAULT_UA,
            "Accept": "application/json, text/plain, */*",
            # 轮询用同一个 URL，被 CDN 缓存住就会永远返回旧状态
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if headers:
            self.headers.update(headers)
        self.cookie = (cookie or "").strip()
        self._backend = backend or _default_backend
        self._sleep = sleep

    def get_json(self, url: str, params: Mapping[str, Any] | None = None, *, referer: str = "") -> Any:
        if params:
            sep = "&" if urllib.parse.urlparse(url).query else "?"
            url = f"{url}{sep}{urllib.parse.urlencode(params)}"
        return json.loads(self.get_text(url, referer=referer))

    def get_text(self, url: str, *, referer: str = "") -> str:
        headers = dict(self.headers)
        if referer:
            headers["Referer"] = referer
        if self.cookie:
            headers["Cookie"] = self.cookie

        last: HttpError | None = None
        for attempt in range(self.retries):
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                status, raw, _ = self._backend(req, self.timeout)
            except HttpError as exc:
                last = exc
            else:
                text = raw.decode("utf-8", "replace")
                if status < 400:
                    return text
                last = HttpError(f"HTTP {status}", status=status, url=url, body=text)
                if status not in RETRY_STATUS:
                    raise last
            if attempt + 1 < self.retries:
                # 指数退避 + 抖动，避免被判定为爬虫
                delay = self.backoff * (2**attempt) * (0.6 + random.random() * 0.8)
                self._sleep(delay)
        assert last is not None
        raise last
