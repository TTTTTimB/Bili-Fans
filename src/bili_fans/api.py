"""B 站数据接口封装。

用到的两个接口：

1) 公开接口（无需登录，任何人可查）
   GET https://api.bilibili.com/x/relation/stat?vmid={uid}
   -> data.follower 是当前总粉丝数。
   只有「总数」，没有「新增 / 流失」，所以只能靠高频采样推算。

2) 创作中心接口（需要 UP 主本人 Cookie 里的 SESSDATA）
   GET https://member.bilibili.com/x/web/index/stat
   -> data.fan_recent_thirty.follow    {"20261101": 12, ...} 每天涨粉数
      data.fan_recent_thirty.unfollow  {"20261101": 3,  ...} 每天掉粉数
   一次请求拿到近 30 天的精确明细，是首选数据源。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping

from .http import HttpError, Transport
from .timeutil import parse_compact_day

API_BASE = "https://api.bilibili.com"
MEMBER_BASE = "https://member.bilibili.com"

STAT_URL = f"{API_BASE}/x/relation/stat"
FOLLOWERS_URL = f"{API_BASE}/x/relation/followers"
CARD_URL = f"{API_BASE}/x/web-interface/card"
CREATOR_STAT_URL = f"{MEMBER_BASE}/x/web/index/stat"
PASSPORT_BASE = "https://passport.bilibili.com"
QR_GENERATE_URL = f"{PASSPORT_BASE}/x/passport-login/web/qrcode/generate"
QR_POLL_URL = f"{PASSPORT_BASE}/x/passport-login/web/qrcode/poll"

# 粉丝列表超过可翻页上限时 B 站返回的 code
CODE_TOO_MANY_PAGES = 22007

CODE_HINTS = {
    CODE_TOO_MANY_PAGES: "B 站只允许翻这么多页",
    -101: "Cookie 无效或已过期，请重新登录 B 站复制 SESSDATA",
    -352: "触发风控，降低频率或换一个网络的 Cookie 再试",
    -400: "请求参数错误（Cookie 不完整时也会报这个）",
    -404: "账号不存在",
    -412: "被 B 站拦截，稍等几分钟再试",
    -509: "请求过于频繁，被限流",
}


class ApiError(Exception):
    def __init__(self, code: int, message: str, *, url: str = ""):
        self.code = code
        self.message = message
        self.url = url
        hint = CODE_HINTS.get(code)
        super().__init__(f"code={code} {message}" + (f"（{hint}）" if hint and hint not in message else ""))


@dataclass(frozen=True)
class Profile:
    uid: int
    name: str
    sign: str = ""
    fans: int | None = None


@dataclass(frozen=True)
class Stat:
    fans: int
    following: int = 0


@dataclass(frozen=True)
class CreatorDay:
    """创作中心口径的一天：这两个数字是 B 站自己算好的，可直接采信。"""

    day: date
    new_fans: int
    lost_fans: int
    total_fans: int | None = None


@dataclass(frozen=True)
class Fan:
    """粉丝列表里的一行。mid 是唯一标识，昵称随时会变，只能用来展示。"""

    mid: int
    uname: str = ""
    mtime: int = 0          # 成为粉丝的时间戳
    face: str = ""
    sign: str = ""
    attribute: int = 0      # 0 未回关 / 2 已关注 / 6 互粉


@dataclass
class FanPage:
    fans: list[Fan] = field(default_factory=list)
    total: int | None = None
    page: int = 1
    capped: bool = False    # 撞上 B 站的可查看上限


@dataclass
class QRSession:
    """扫码登录的一张二维码。url 是二维码内容，key 用来轮询状态。"""

    key: str
    url: str


@dataclass
class QRLoginState:
    status: str = "waiting"          # waiting / scanned / confirmed / expired
    cookie: str = ""
    values: dict[str, str] = field(default_factory=dict)


@dataclass
class CreatorReport:
    days: list[CreatorDay] = field(default_factory=list)
    total_fans: int | None = None
    incr_fans: int | None = None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


class BiliClient:
    def __init__(self, transport: Transport | None = None, *, cookie: str = ""):
        self.transport = transport or Transport()
        self.cookie = normalize_cookie(cookie) or self.transport.cookie

    # ---------- 底层 ----------

    def _get(self, url: str, params: Mapping[str, Any] | None, referer: str) -> dict[str, Any]:
        try:
            payload = self.transport.get_json(url, params, referer=referer)
        except HttpError:
            raise
        if not isinstance(payload, dict):
            raise ApiError(-1, "返回格式不是 JSON 对象", url=url)
        code = _as_int(payload.get("code"))
        if code not in (0, None):
            raise ApiError(code, str(payload.get("message") or ""), url=url)
        return payload

    # ---------- 公开数据 ----------

    def stat(self, uid: int) -> Stat:
        payload = self._get(STAT_URL, {"vmid": uid}, f"https://space.bilibili.com/{uid}")
        data = payload.get("data") or {}
        fans = _as_int(data.get("follower"))
        if fans is None:
            fans = _as_int(data.get("fans"))
        if fans is None:
            raise ApiError(-1, "响应里找不到粉丝数字段", url=STAT_URL)
        return Stat(fans=fans, following=_as_int(data.get("following")) or 0)

    def profile(self, uid: int) -> Profile | None:
        """昵称不是必需品，失败就返回 None，不打断主流程。"""
        try:
            payload = self._get(CARD_URL, {"mid": uid}, f"https://space.bilibili.com/{uid}")
        except (ApiError, HttpError):
            return None
        card = (payload.get("data") or {}).get("card") or {}
        name = str(card.get("name") or "").strip()
        if not name:
            return None
        return Profile(uid=uid, name=name, sign=str(card.get("sign") or ""), fans=_as_int(card.get("fans")))

    def followers(self, uid: int, pn: int = 1, ps: int = 50) -> FanPage:
        """拉自己（或公开账号）的粉丝明细。

        注意 B 站的限制：登录后自己最多能看前 1000 名，别人只能看前 100 名 / 5 页。
        超出时会返回 22007，这里当成「翻到头了」而不是错误。
        """
        try:
            payload = self._get(
                FOLLOWERS_URL, {"vmid": uid, "pn": pn, "ps": ps}, f"https://space.bilibili.com/{uid}"
            )
        except ApiError as exc:
            if exc.code == CODE_TOO_MANY_PAGES:
                return FanPage(fans=[], page=pn, capped=True)
            raise
        data = payload.get("data") or {}
        raw = data.get("list")
        if not isinstance(raw, list):
            raw = data.get("items") or []
        fans = [_parse_fan(item) for item in raw if isinstance(item, dict)]
        return FanPage(
            fans=[f for f in fans if f.mid],
            total=_as_int(data.get("total")),
            page=pn,
        )

    # ---------- 扫码登录 ----------

    def qr_generate(self) -> QRSession:
        payload = self._get(QR_GENERATE_URL, None, "https://www.bilibili.com/")
        data = payload.get("data") or {}
        url, key = str(data.get("url") or ""), str(data.get("qrcode_key") or "")
        if not url or not key:
            raise ApiError(-1, "接口没返回二维码内容", url=QR_GENERATE_URL)
        return QRSession(key=key, url=url)

    def fingerprint(self) -> tuple[str, str]:
        """拿一对 buvid，补进 Cookie 里可以明显降低 -352 的概率。"""
        payload = self._get(f"{API_BASE}/x/frontend/finger/spi", None, "https://www.bilibili.com/")
        data = payload.get("data") or {}
        return str(data.get("b_3") or ""), str(data.get("b_4") or "")

    def qr_poll(self, key: str) -> QRLoginState:
        payload = self._get(QR_POLL_URL, {"qrcode_key": key}, "https://www.bilibili.com/")
        data = payload.get("data") or {}
        code = _as_int(data.get("code"))
        if code == 86101:
            return QRLoginState(status="waiting")
        if code == 86090:
            return QRLoginState(status="scanned")
        if code == 86038:
            return QRLoginState(status="expired")
        if code != 0:
            return QRLoginState(status="expired")

        values = _cookie_values(str(data.get("url") or ""))
        if not values.get("SESSDATA"):
            raise ApiError(-1, "登录成功但没拿到 SESSDATA", url=QR_POLL_URL)
        return QRLoginState(status="confirmed", values=values, cookie=build_cookie(values))

    # ---------- 创作中心 ----------

    def me(self) -> tuple[int, str]:
        """严格的「我是谁」：失败会抛异常，方便告诉用户到底哪里不对。"""
        if not self.cookie:
            raise ApiError(-101, "未配置 Cookie")
        payload = self._get(f"{API_BASE}/x/web-interface/nav", None, "https://www.bilibili.com/")
        data = payload.get("data") or {}
        if not data.get("isLogin"):
            raise ApiError(-101, "账号未登录")
        mid = _as_int(data.get("mid"))
        if not mid:
            raise ApiError(-101, "响应里没有 mid")
        return mid, str(data.get("uname") or "")

    def nav(self) -> tuple[int, str] | None:
        """宽松版：只在能确认身份时返回，用来防止把 A 的数据写到 B 的账号上。"""
        if not self.cookie:
            return None
        try:
            return self.me()
        except (ApiError, HttpError):
            return None

    def creator_report(self, url: str = CREATOR_STAT_URL) -> CreatorReport:
        if not self.cookie:
            raise ApiError(-101, "未配置 Cookie，无法读取创作中心数据", url=url)
        payload = self._get(url, None, f"{MEMBER_BASE}/platform/home")
        data = payload.get("data") or {}

        recent = data.get("fan_recent_thirty") or {}
        follow = recent.get("follow") or {}
        unfollow = recent.get("unfollow") or {}

        # 两边日期取并集；缺的一侧按 0 处理（B 站某天无数据时不会返回该键）
        keys = sorted({str(k) for k in follow} | {str(k) for k in unfollow})
        days: list[CreatorDay] = []
        for key in keys:
            try:
                day = parse_compact_day(key)
            except ValueError:
                continue
            days.append(
                CreatorDay(
                    day=day,
                    new_fans=max(0, _as_int(follow.get(key)) or 0),
                    lost_fans=abs(_as_int(unfollow.get(key)) or 0),
                )
            )

        total = _as_int(data.get("total_fans"))
        days = [CreatorDay(d.day, d.new_fans, d.lost_fans, total) for d in days]
        return CreatorReport(
            days=sorted(days, key=lambda d: d.day),
            total_fans=total,
            incr_fans=_as_int(data.get("incr_fans")),
        )


WANTED_COOKIES = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "buvid3", "buvid4")


def _cookie_values(url: str) -> dict[str, str]:
    """登录成功后 B 站把凭据拼在跳转链接的 query 里。"""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    return {k: v[0] for k, v in query.items() if k in WANTED_COOKIES and v}


def build_cookie(values: dict[str, str]) -> str:
    """关键字段排前面，其余原样带上去（别把 buvid 之类漏掉）。"""
    ordered = [k for k in WANTED_COOKIES if k in values] + [k for k in values if k not in WANTED_COOKIES]
    return "; ".join(f"{k}={values[k]}" for k in ordered)


def _parse_fan(item: dict[str, Any]) -> Fan:
    return Fan(
        mid=_as_int(item.get("mid") or item.get("uid")) or 0,
        uname=str(item.get("uname") or item.get("name") or "").strip(),
        mtime=_as_int(item.get("mtime")) or 0,
        face=str(item.get("face") or "").strip(),
        sign=str(item.get("sign") or "").strip(),
        attribute=_as_int(item.get("attribute")) or 0,
    )


def normalize_cookie(raw: str | None) -> str:
    """把用户可能粘贴的各种形态归一成一段 Cookie。

    支持：
      - 纯 SESSDATA 值          -> SESSDATA=值
      - SESSDATA=xxx; bili_jct=y
      - DevTools 整行           -> cookie: SESSDATA=xxx; ...
      - 从 cURL 里抠出来的       -> -H 'cookie: SESSDATA=xxx'
    """
    if not raw:
        return ""
    text = str(raw).strip().strip('"').strip("'").strip()
    if not text:
        return ""
    # 允许直接粘贴 "Cookie:" 头，或带 -H 'cookie: ...' 的 cURL 片段
    match = re.search(r"cookie\s*:\s*([^\"']+)", text, re.I)
    if match:
        text = match.group(1).strip()
    text = text.replace("\n", " ").replace("\r", " ").strip()
    if not text:
        return ""
    if "=" not in text:
        return f"SESSDATA={text}"
    return text
