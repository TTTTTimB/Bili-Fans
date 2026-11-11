"""CLI 与网页看板共用的业务逻辑（不涉及任何输入输出）。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

from .api import ApiError, BiliClient, normalize_cookie
from .db import (
    Account,
    add_snapshot,
    creator_days_between,
    get_account,
    last_snapshot,
    snapshots_between,
    upsert_account,
    upsert_creator_days,
)
from .http import HttpError
from .stats import SOURCE_NONE, DayRow, Summary, build_rows, summarize
from .timeutil import day_key, day_start_ts, local_day, now_ts, to_local


def window(days: int, tz, today: date | None = None) -> tuple[date, date]:
    """报告窗口：最近 days 天，含今天。"""
    end = today or local_day(now_ts(), tz)
    return end - timedelta(days=max(1, days) - 1), end


def collect(conn, uid: int, days: int, tz) -> tuple[list[DayRow], Summary]:
    """把快照和官方每日数据拼成表格。"""
    start, end = window(days, tz)
    # 多取两天，保证第一天也有「前一天收盘价」做基准
    snaps = snapshots_between(conn, uid, day_start_ts(start, tz) - 2 * 86400, now_ts() + 60)
    creator = creator_days_between(conn, uid, day_key(start), day_key(end))
    rows = build_rows(snaps, creator, start, end, tz)
    # 刚开始用时，窗口前面那些「还没开始监控」的空白天直接裁掉
    first = next((i for i, r in enumerate(rows) if r.source != SOURCE_NONE), None)
    if first:
        rows = rows[first:]
    return rows, summarize(rows)


def ensure_account(conn, uid: int) -> Account:
    acc = get_account(conn, uid)
    return acc if acc else upsert_account(conn, uid, "", "")


def poll_account(conn, client: BiliClient, acc: Account, tz) -> tuple[bool, str]:
    """采一次总粉丝数。返回 (是否成功, 人类可读的一行话)。"""
    ts = now_ts()
    prev = last_snapshot(conn, acc.uid)
    try:
        stat = client.stat(acc.uid)
    except (ApiError, HttpError) as exc:
        return False, f"{acc.label} 采集失败：{exc}"
    add_snapshot(conn, acc.uid, ts, stat.fans, stat.following, "public")

    delta = ""
    if prev:
        delta = f"  {stat.fans - prev.fans:+,}"
    clock = to_local(ts, tz).strftime("%m-%d %H:%M:%S")
    return True, f"{clock}  {acc.label}  {stat.fans:,}{delta}"


def backfill_account(
    conn,
    client: BiliClient,
    acc: Account,
    *,
    cookie: str,
    creator_url: str,
    log: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """拉创作中心近 30 天的官方涨/掉粉。"""
    if not cookie:
        return False, "未配置 Cookie：请设置环境变量 BILI_COOKIE='SESSDATA=...' 或加 --cookie"

    owner = client.nav()
    if owner and owner[0] != acc.uid:
        return False, (
            f"Cookie 登录的是 UID {owner[0]}（{owner[1]}），不是 {acc.uid}。"
            "创作中心只返回登录账号自己的数据，已跳过以免写错账号。"
        )

    try:
        report = client.creator_report(creator_url)
    except (ApiError, HttpError) as exc:
        return False, f"{acc.label} 拉取失败：{exc}"
    if not report.days:
        return False, f"{acc.label} 接口返回了空数据，可能是账号还没有粉丝数据"

    n = upsert_creator_days(conn, acc.uid, report.days)
    first, last = report.days[0].day, report.days[-1].day
    new_sum = sum(d.new_fans for d in report.days)
    lost_sum = sum(d.lost_fans for d in report.days)
    if owner and not acc.name:
        upsert_account(conn, acc.uid, owner[1], "")
    if log:
        log(f"  写入 {n} 天（{first} ~ {last}）")
    return True, (
        f"{acc.label}  写入 {n} 天（{first} ~ {last}），"
        f"区间新增 {new_sum:,} 掉粉 {lost_sum:,}"
    )


def resolve_cookie(cfg, cli_cookie: str = "") -> tuple[str, str]:
    """优先级：--cookie > 环境变量 > bilifans login 存下来的文件。

    文件是在**每次调用时**读的，所以看板开着的时候去登录，刷新就能生效。
    """
    if cli_cookie:
        return normalize_cookie(cli_cookie), "--cookie"
    env = cfg.cookie_from_env()
    if env:
        return normalize_cookie(env), f"环境变量 {cfg.cookie_env}"

    from . import browser

    saved = browser.load_cookie(browser.COOKIE_FILE)
    if saved:
        return normalize_cookie(saved), f"登录缓存 {browser.COOKIE_FILE}"
    return "", ""
