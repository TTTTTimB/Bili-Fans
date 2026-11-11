"""时间工具：所有「一天」都按本地时区（默认 Asia/Shanghai）切分。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Shanghai"


def get_tz(name: str | None = None) -> ZoneInfo:
    return ZoneInfo(name or DEFAULT_TZ)


def now_ts() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


def to_local(ts: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ts, tz=tz)


def local_day(ts: int, tz: ZoneInfo) -> date:
    return to_local(ts, tz).date()


def day_start_ts(day: date, tz: ZoneInfo) -> int:
    """该日本地 00:00:00 的时间戳。"""
    return int(datetime.combine(day, time(0, 0), tzinfo=tz).timestamp())


def parse_day(text: str) -> date:
    return date.fromisoformat(text.strip())


def day_key(day: date) -> str:
    return day.isoformat()


def parse_compact_day(text: str) -> date:
    """解析 B 站返回的 YYYYMMDD。"""
    return datetime.strptime(text.strip(), "%Y%m%d").date()


def iter_days(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def fmt_ts(ts: int, tz: ZoneInfo) -> str:
    return to_local(ts, tz).strftime("%Y-%m-%d %H:%M")


def fmt_clock(ts: int, tz: ZoneInfo) -> str:
    return to_local(ts, tz).strftime("%H:%M")
