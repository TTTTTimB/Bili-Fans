"""用浏览器自己取 Cookie。

背景：SESSDATA 是 HttpOnly，页面里的 JavaScript 读不到（document.cookie 拿不到它）。
但浏览器的调试协议（CDP）能读到**全部** Cookie，包括 HttpOnly 的。

于是这里的做法是：
  起一个带独立 profile 的 Chrome → 打开 bilibili.com → 用 CDP 把 Cookie 读出来 →
  存到本地文件。登录状态记在这个 profile 里，下次再跑不用重新扫码。

只依赖标准库：通过 `--remote-debugging-pipe`（fd 3 / fd 4）跟 Chrome 通信，
消息是 NUL 分隔的 JSON，不需要 WebSocket 库。
"""

from __future__ import annotations

import json
import os
import select
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

APP_DIR = Path.home() / ".bilifans"
PROFILE_DIR = APP_DIR / "chrome-profile"
COOKIE_FILE = Path.home() / ".bilifans_cookie"
BILI_DOMAIN = "bilibili.com"
DEFAULT_URL = "https://www.bilibili.com/"

# 这些字段拿不到也不影响用，但一起存着更稳（少触发风控）
WANTED = ("SESSDATA", "bili_jct", "buvid3", "buvid4", "DedeUserID")

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
CHROME_COMMANDS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge")


class BrowserError(Exception):
    """启动浏览器 / 通信失败。"""


def find_chrome() -> str | None:
    override = os.environ.get("BILIFANS_CHROME", "").strip()
    if override:
        return override if Path(override).exists() else None
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    for name in CHROME_COMMANDS:
        found = shutil.which(name)
        if found:
            return found
    return None


# ---------------------------------------------------------------- CDP


class ChromeCDP:
    """最小可用的 Chrome DevTools Protocol 客户端（pipe 模式）。"""

    def __init__(self, chrome: str, profile_dir: Path, *, url: str = DEFAULT_URL, headless: bool = False,
                 timeout: float = 30.0):
        self.chrome = chrome
        self.profile_dir = Path(profile_dir)
        self.url = url
        self.headless = headless
        self.timeout = timeout
        self.proc: subprocess.Popen | None = None
        self._buf = b""
        self._next_id = 0
        self._in = -1
        self._out = -1
        self._session: str | None = None

    # ---------- 生命周期 ----------

    def __enter__(self) -> "ChromeCDP":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        if sys.platform == "win32":
            raise BrowserError("Windows 暂不支持自动取 Cookie，请按 docs/get-cookie.md 手动复制")
        if not Path(self.chrome).exists() and not shutil.which(self.chrome):
            raise BrowserError(f"找不到浏览器：{self.chrome}")
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        read_fd, write_fd = os.pipe()   # 浏览器读 fd3
        out_fd, sink_fd = os.pipe()     # 浏览器写 fd4
        args = [
            str(self.chrome),
            f"--user-data-dir={self.profile_dir}",
            "--remote-debugging-pipe",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            self.url,
        ]
        if self.headless:
            args.insert(-1, "--headless=new")
            args.insert(-1, "--disable-gpu")

        # 用 sh 把 0/1 重定向成 fd3/fd4：Python 的 preexec_fn 在关 fd 之前执行，
        # 直接 dup2 会被随后的 close_fds 关掉，所以交给 shell 做。
        shell = 'exec "$0" "$@" 3<&0 4>&1 0</dev/null 1>/dev/null'
        try:
            self.proc = subprocess.Popen(
                ["/bin/sh", "-c", shell, *args],
                stdin=read_fd,
                stdout=sink_fd,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        finally:
            os.close(read_fd)
            os.close(sink_fd)
        self._in = write_fd
        self._out = out_fd

        # 浏览器起不来时管道会立刻 EOF，这里给个明确的错误而不是傻等
        deadline = time.time() + self.timeout
        while True:
            try:
                self.call("Browser.getVersion", timeout=2)
                break
            except (TimeoutError, BrowserError) as exc:
                if self.proc.poll() is not None:
                    self._cleanup_fds()
                    raise BrowserError("浏览器启动失败（若已有 Chrome 在用同一个 profile，请先关掉）") from exc
                if time.time() > deadline:
                    self._cleanup_fds()
                    raise BrowserError("连不上浏览器调试通道，启动超时") from exc
                time.sleep(0.2)

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.call("Browser.close", timeout=3)
                self.proc.wait(timeout=8)
            except Exception:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        self._cleanup_fds()

    def _cleanup_fds(self) -> None:
        for fd in (self._in, self._out):
            if fd is not None and fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._in = self._out = -1

    # ---------- 协议 ----------

    def _send(self, payload: dict) -> None:
        if self._in < 0:
            raise BrowserError("调试通道已关闭")
        try:
            os.write(self._in, json.dumps(payload).encode() + b"\0")
        except OSError as exc:
            raise BrowserError("写入浏览器失败") from exc

    def _read(self, deadline: float) -> dict:
        while b"\0" not in self._buf:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("等待浏览器响应超时")
            ready, _, _ = select.select([self._out], [], [], remaining)
            if not ready:
                raise TimeoutError("等待浏览器响应超时")
            chunk = os.read(self._out, 65536)
            if not chunk:
                raise BrowserError("浏览器关闭了调试通道")
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\0", 1)
        return json.loads(raw)

    def call(self, method: str, params: dict | None = None, *, session: str | None = None,
             timeout: float = 15.0) -> dict:
        self._next_id += 1
        msg_id = self._next_id
        payload: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session:
            payload["sessionId"] = session
        self._send(payload)
        deadline = time.time() + timeout
        while True:
            message = self._read(deadline)
            if message.get("id") != msg_id:
                continue  # 事件消息，忽略
            if "error" in message:
                raise BrowserError(f"{method} 失败：{message['error'].get('message')}")
            return message.get("result", {})

    # ---------- 业务 ----------

    def attach_page(self) -> str:
        pages = [t for t in self.call("Target.getTargets")["targetInfos"] if t["type"] == "page"]
        if not pages:
            created = self.call("Target.createTarget", {"url": self.url})
            target_id = created["targetId"]
        else:
            target_id = pages[0]["targetId"]
        result = self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        self._session = result["sessionId"]
        return self._session

    def navigate(self, url: str) -> None:
        """导航到目标页。失败/超时都不算致命：URL 已经在启动参数里了，页面会自己加载。"""
        session = self._session or self.attach_page()
        try:
            self.call("Page.navigate", {"url": url}, session=session, timeout=10)
        except (BrowserError, TimeoutError):
            pass

    def cookies(self) -> list[dict]:
        session = self._session or self.attach_page()
        result = self.call("Network.getAllCookies", session=session)
        return result.get("cookies", [])

    def set_cookie(self, name: str, value: str, domain: str = ".bilibili.com",
                   expires: float | None = None) -> None:
        """给自己写测试用例用的，正常流程不需要。

        注意：不传 expires 就是会话 Cookie，关掉浏览器就没了；想跨进程复用必须给过期时间。
        """
        session = self._session or self.attach_page()
        params: dict[str, Any] = {"name": name, "value": value, "domain": domain, "path": "/"}
        if expires is not None:
            params["expires"] = expires
        self.call("Network.setCookie", params, session=session)


# ---------------------------------------------------------------- 取 Cookie


@dataclass
class CookieResult:
    cookie: str = ""
    values: dict[str, str] = field(default_factory=dict)
    logged_in: bool = False
    seconds: float = 0.0

    @property
    def names(self) -> list[str]:
        return sorted(self.values)

    @property
    def uid(self) -> str:
        return self.values.get("DedeUserID", "")

    @property
    def masked(self) -> str:
        """只露头尾，避免把完整密钥打到终端/日志里。"""
        parts = []
        for key in ("SESSDATA", "bili_jct", "DedeUserID"):
            value = self.values.get(key)
            if not value:
                continue
            if key != "DedeUserID":
                # 短值也不许露出来：六个字符的密钥一样是密钥
                value = f"{value[:6]}…{value[-4:]}" if len(value) > 8 else "…"
            parts.append(f"{key}={value}")
        extra = len(self.values) - len(parts)
        text = "; ".join(parts)
        return f"{text}（另有 {extra} 个字段）" if extra > 0 else text


def _cookie_header(cookies: list[dict]) -> tuple[str, dict[str, str]]:
    """把 CDP 的 cookie 列表拼成请求头，只保留 bilibili 域名下的。"""
    picked: dict[str, str] = {}
    for item in cookies:
        domain = str(item.get("domain") or "")
        name = str(item.get("name") or "")
        if not name or BILI_DOMAIN not in domain:
            continue
        picked[name] = str(item.get("value") or "")
    # 关键字段排前面，读日志时一眼能看到
    ordered = [k for k in WANTED if k in picked] + [k for k in picked if k not in WANTED]
    return "; ".join(f"{k}={picked[k]}" for k in ordered), picked


def capture_cookie(
    profile_dir: Path = PROFILE_DIR,
    *,
    url: str = DEFAULT_URL,
    timeout: float = 180.0,
    headless: bool = False,
    poll: float = 1.0,
    chrome: str | None = None,
    client_factory: Callable[..., Any] = ChromeCDP,
    on_wait: Callable[[float], None] | None = None,
) -> CookieResult:
    """打开 bilibili.com，等到 SESSDATA 出现就把它取回来。

    - profile 是持久的，登录过就会记住，下次几秒就能拿完。
    - 没登录时会一直等（默认 180 秒），足够你扫码。
    """
    browser = chrome or find_chrome()
    if not browser:
        raise BrowserError(
            "没找到 Chrome / Chromium / Edge。\n"
            "  装一个，或者设置 BILIFANS_CHROME=/path/to/chrome；\n"
            "  也可以按 docs/get-cookie.md 手动复制 Cookie。"
        )

    started = time.time()
    with client_factory(browser, Path(profile_dir), url=url, headless=headless, timeout=30) as cdp:
        cdp.attach_page()
        cdp.navigate(url)  # 失败也无所谓，cookie 照样能读
        deadline = started + timeout
        while True:
            try:
                header, values = _cookie_header(cdp.cookies())
            except BrowserError as exc:
                raise BrowserError("浏览器中途被关闭了，没能取到 Cookie") from exc
            if "SESSDATA" in values:
                return CookieResult(
                    cookie=header,
                    values=values,
                    logged_in=True,
                    seconds=time.time() - started,
                )
            if time.time() > deadline:
                raise BrowserError(
                    f"等了 {int(timeout)} 秒还没登录成功。请在弹出的浏览器窗口里扫码登录"
                    "（或加 --timeout 再试）。"
                )
            if on_wait:
                on_wait(deadline - time.time())
            time.sleep(poll)


# ---------------------------------------------------------------- 本地保存


def save_cookie(path: Path = COOKIE_FILE, cookie: str = "") -> Path:
    """写到用户目录，权限 600。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(cookie.strip() + "\n", encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(path)
    return path


def load_cookie(path: Path = COOKIE_FILE) -> str:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return text


def cookie_path_label() -> str:
    return str(COOKIE_FILE)
