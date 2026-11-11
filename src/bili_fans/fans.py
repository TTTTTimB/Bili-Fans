"""粉丝名单快照对比：反推「谁取关了我」。

B 站没有任何接口直接告诉你「谁取关了你」，只有「当前粉丝列表」。
所以唯一可行的办法是：

    每天抓一份完整名单 → 和上一次比 → 上次在、这次不在的人 = 取关了

这份文件负责「抓 + 比 + 落库」这条链路，可测试的部分都做成了纯函数。

必须知道的限制（B 站定的，不是本软件偷懒）：
- 登录后最多只能翻到自己「最近 1000 名粉丝」，更早的粉丝根本拿不到。
- 粉丝数 <= 1000 时是**全量覆盖**，取关检测是完整的。
- 粉丝数 > 1000 时只能看到「最近关注的那批人里谁取关了」，更早的粉丝取关永远发现不了。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .api import BiliClient, Fan, FanPage
from .db import (
    ChurnRow,
    finish_fan_crawl,
    latest_fan_crawl,
    mark_missing_gone,
    start_fan_crawl,
    upsert_fans,
)
from .timeutil import now_ts

DEFAULT_LIMIT = 1000      # B 站登录态可见上限
DEFAULT_PAGE_SIZE = 50
MAX_PAGES = 60            # 兜底，防止死循环
GUARD_RATIO = 0.8         # 本次抓到的人数 < 上次的 80% 就认为接口异常，不做取关判定


@dataclass
class CrawlOutcome:
    crawl_id: int
    fetched: int = 0
    total: int | None = None
    complete: bool = False
    new_fans: list[Fan] = field(default_factory=list)
    gone: list[ChurnRow] = field(default_factory=list)
    baseline: bool = False          # 第一次抓取，只建基线
    guard_skipped: bool = False      # 因为数量异常，跳过了取关判定
    note: str = ""

    @property
    def coverage(self) -> str:
        if self.complete:
            return "全量"
        if self.total:
            return f"最近 {self.fetched} 人（总 {self.total:,}）"
        return f"最近 {self.fetched} 人"


def crawl(
    client: BiliClient,
    conn,
    owner_uid: int,
    *,
    limit: int = DEFAULT_LIMIT,
    ps: int = DEFAULT_PAGE_SIZE,
    pause: float = 0.35,
    sleep: Callable[[float], None] = time.sleep,
    ts: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> CrawlOutcome:
    """抓一次完整名单并和上一次对比。网络错误会原样抛出，但已先把这次标记为失败。"""
    started = int(ts or now_ts())
    prev = latest_fan_crawl(conn, owner_uid)
    crawl_id = start_fan_crawl(conn, owner_uid, started)
    outcome = CrawlOutcome(crawl_id=crawl_id, baseline=prev is None)

    collected: dict[int, Fan] = {}
    total: int | None = None
    capped = False
    reached_end = False

    try:
        page_no = 1
        while len(collected) < limit:
            page: FanPage = client.followers(owner_uid, pn=page_no, ps=ps)
            if page.total is not None:
                total = page.total
            if page.capped:
                capped = True
                break
            if not page.fans:
                reached_end = True
                break
            for fan in page.fans:
                collected.setdefault(fan.mid, fan)
            if progress:
                progress(f"  第 {page_no} 页，累计 {len(collected)} 人")
            if len(page.fans) < ps or (total is not None and len(collected) >= total):
                reached_end = True
                break
            page_no += 1
            if page_no > MAX_PAGES:
                break
            if pause:
                sleep(pause)
    except Exception as exc:  # 抓一半失败：记账后交给上层处理，不在这里吞掉
        finish_fan_crawl(
            conn,
            crawl_id,
            fetched=len(collected),
            total=total,
            complete=False,
            note=f"抓取中断：{exc}",
            ts=started,
        )
        raise

    # 名单按「最近关注」排序，超限时保留最近的 limit 个人
    fans = list(collected.values())[:limit]
    outcome.fetched = len(fans)
    outcome.total = total
    # 只有「拿到的人数达到接口自报的总数」才算全量；
    # 接口不给 total 时，退而用「翻到了最后一页」判断。
    if total is not None:
        outcome.complete = bool(not capped and len(fans) >= total)
    else:
        outcome.complete = bool(not capped and reached_end)

    outcome.new_fans = upsert_fans(
        conn, owner_uid, fans, started, crawl_id, emit_events=not outcome.baseline
    )

    # 只有在「这次确实抓全了」或「至少和上次差不多多」时才敢判定取关，
    # 否则一次失败的抓取会把所有人都标成取关。
    expected = prev.fetched if prev else 0
    trusted = outcome.complete or (expected > 0 and len(fans) >= expected * GUARD_RATIO)
    if not outcome.baseline and not trusted:
        outcome.guard_skipped = True
        outcome.note = f"本次只抓到 {len(fans)} 人（上次 {expected} 人），疑似接口异常，已跳过取关判定"
    elif not outcome.baseline:
        outcome.gone = mark_missing_gone(conn, owner_uid, started, crawl_id)

    if not outcome.note:
        if capped:
            outcome.note = "撞上 B 站可查看上限，本次名单不完整"
        elif not outcome.complete:
            outcome.note = f"仅覆盖最近 {len(fans)} 名粉丝"

    finish_fan_crawl(
        conn,
        crawl_id,
        fetched=len(fans),
        total=total,
        complete=outcome.complete,
        note=outcome.note,
        ts=started,
    )
    return outcome


def survival_days(mtime: int | None, gone_ts: int) -> int | None:
    """从关注到取关一共待了几天。"""
    if not mtime or mtime <= 0 or mtime > gone_ts:
        return None
    return max(0, (gone_ts - mtime) // 86400)


def summarize_churn(rows: Sequence[ChurnRow]) -> dict:
    durations = [d for r in rows if (d := survival_days(r.mtime, r.gone_ts)) is not None]
    quick = [d for d in durations if d <= 7]
    return {
        "count": len(rows),
        "avg_survival": (sum(durations) / len(durations)) if durations else None,
        "quick_count": len(quick),     # 关注不到一周就跑了的人
        "known_since": len(durations),
    }
