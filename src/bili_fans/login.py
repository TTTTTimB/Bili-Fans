"""扫码登录：拿二维码 → 轮询状态 → 换回 Cookie。

B 站把登录凭据直接放在「登录成功后的跳转链接」的 query 里，
所以整个过程不需要浏览器，也不需要解析 HTML。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .api import ApiError, BiliClient, QRLoginState, QRSession, build_cookie
from .http import HttpError
from .timeutil import now_ts

DEFAULT_TIMEOUT = 180.0
DEFAULT_INTERVAL = 2.0


@dataclass
class LoginSession:
    session: QRSession
    started: float = field(default_factory=time.time)
    # 生成二维码用的那个客户端：轮询必须用同一个（同一份 Cookie / buvid），
    # 否则 B 站会认为不是同一个会话，直接返回失效。
    client: BiliClient | None = None

    @property
    def key(self) -> str:
        return self.session.key

    @property
    def url(self) -> str:
        return self.session.url

    def age(self) -> float:
        return time.time() - self.started


def begin(client: BiliClient) -> LoginSession:
    """申请一张二维码。不需要任何登录态。"""
    return LoginSession(session=client.qr_generate(), client=client)


def check(client: BiliClient, session: LoginSession) -> QRLoginState:
    """查一次状态。"""
    return client.qr_poll(session.key)


def wait(
    client: BiliClient,
    session: LoginSession,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
    on_tick: Callable[[float, str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> QRLoginState:
    """一直轮询到扫码成功、二维码失效或超时。

    on_tick(剩余秒数, 当前状态) 用来刷进度，返回的状态里 status 可能是
    confirmed / expired / waiting（超时）。
    """
    deadline = session.started + timeout
    state = QRLoginState(status="waiting")
    while True:
        try:
            state = check(client, session)
        except ApiError:
            state = QRLoginState(status="waiting")

        if state.status in ("confirmed", "expired"):
            return state

        remaining = deadline - time.time()
        if on_tick:
            on_tick(max(0.0, remaining), state.status)
        if remaining <= 0:
            return QRLoginState(status="waiting")
        sleep(min(interval, remaining))


def describe(state: str) -> str:
    return {
        "waiting": "等待扫码",
        "scanned": "已扫码，请在手机上确认",
        "confirmed": "登录成功",
        "expired": "二维码已过期",
    }.get(state, state)


def started_at_label() -> str:
    return time.strftime("%H:%M:%S", time.localtime(now_ts()))


def finalize(client: BiliClient, state: QRLoginState) -> tuple[str, dict[str, str]]:
    """登录成功后补齐 buvid，再拼成最终的 Cookie。

    扫码接口只给 SESSDATA / bili_jct / DedeUserID，没有 buvid3/buvid4；
    少这些字段时创作中心接口更容易吃 -352。
    """
    values = dict(state.values)
    try:
        buvid3, buvid4 = client.fingerprint()
        if buvid3:
            values.setdefault("buvid3", buvid3)
        if buvid4:
            values.setdefault("buvid4", buvid4)
    except (ApiError, HttpError):
        pass
    return build_cookie(values), values
