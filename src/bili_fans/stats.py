"""核心统计逻辑：把「快照序列」和「创作中心每日数据」拼成按天的一张表。

两条口径，务必分清：

净增（net） = 当天最后一次采样 - 前一天最后一次采样      —— 一定准确
新增/掉粉   = 创作中心直接给出的两个数字                —— 精确，但只有 T+1 才有
估算新增    = Σ 相邻两次采样中「变多」的那部分           —— 只是下限估计

为什么估算只是下限：两次采样之间如果同时发生了 +5 和 -3，总粉丝只 +2，
估算结果只会记下 +2，那 3 个取关被淹没在净增里了。采样越密，误差越小，
但永远不可能靠总粉丝数 100% 还原出新增和掉粉。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Mapping, Sequence

from .db import Snapshot
from .timeutil import day_key, day_start_ts, iter_days, local_day

SOURCE_CREATOR = "creator"
SOURCE_TICK = "tick"
SOURCE_NONE = "none"


@dataclass
class DayRow:
    day: date
    net: int | None = None
    new_fans: int | None = None          # 创作中心口径，精确
    lost_fans: int | None = None         # 创作中心口径，精确
    est_new: int | None = None           # 采样估算，下限
    est_lost: int | None = None          # 采样估算，下限
    close_fans: int | None = None        # 当天最后一个总粉丝数
    base_fans: int | None = None         # 当天 0 点前的总粉丝数
    base_ts: int | None = None           # 基准点的时间
    samples: int = 0
    first_ts: int | None = None
    last_ts: int | None = None
    source: str = SOURCE_NONE
    note: str = ""

    @property
    def has_exact(self) -> bool:
        return self.source == SOURCE_CREATOR

    @property
    def display_new(self) -> int | None:
        return self.new_fans if self.has_exact else self.est_new

    @property
    def display_lost(self) -> int | None:
        return self.lost_fans if self.has_exact else self.est_lost


@dataclass
class Summary:
    days: int = 0
    day_rows: list[DayRow] = field(default_factory=list)
    new_total: int = 0
    lost_total: int = 0
    net_total: int = 0
    exact_days: int = 0
    est_days: int = 0
    empty_days: int = 0
    first_fans: int | None = None
    last_fans: int | None = None
    best_new: DayRow | None = None
    worst_lost: DayRow | None = None
    churn_rate: float | None = None   # 平均每天掉粉 / 平均粉丝规模

    @property
    def avg_new(self) -> float:
        active = self.exact_days + self.est_days
        return self.new_total / active if active else 0.0

    @property
    def avg_lost(self) -> float:
        active = self.exact_days + self.est_days
        return self.lost_total / active if active else 0.0

    @property
    def exact_ratio(self) -> float:
        return self.exact_days / self.days if self.days else 0.0


def build_rows(
    snapshots: Sequence[Snapshot],
    creator: Mapping[str, tuple[int, int]],
    start_day: date,
    end_day: date,
    tz,
    *,
    tolerance: int = 3,
) -> list[DayRow]:
    """按天汇总。snapshots 需按时间升序，且最好包含 start_day 之前的一两条作为基准。"""
    snaps = sorted(snapshots, key=lambda s: s.ts)
    by_day: dict[date, list[Snapshot]] = {}
    for snap in snaps:
        by_day.setdefault(local_day(snap.ts, tz), []).append(snap)

    # 逐对相邻快照累计「涨了多少 / 掉了多少」，并记到后一条所属的那天
    est_new: dict[date, int] = {}
    est_lost: dict[date, int] = {}
    tick_days: set[date] = set()   # 这一天至少有一对相邻采样，估算才有意义
    for prev, cur in zip(snaps, snaps[1:]):
        day_prev = local_day(prev.ts, tz)
        day_cur = local_day(cur.ts, tz)
        if day_cur < start_day or day_cur > end_day:
            continue
        if (day_cur - day_prev).days > 1:
            continue  # 中间断采超过一天，这段跨度无法归到某一天，直接丢弃
        tick_days.add(day_cur)
        delta = cur.fans - prev.fans
        if delta > 0:
            est_new[day_cur] = est_new.get(day_cur, 0) + delta
        elif delta < 0:
            est_lost[day_cur] = est_lost.get(day_cur, 0) + (-delta)

    rows: list[DayRow] = []
    baseline: int | None = None
    baseline_ts: int | None = None
    if snaps:
        earliest = day_start_ts(start_day, tz)
        before = [s for s in snaps if s.ts < earliest]
        if before:
            baseline = before[-1].fans
            baseline_ts = before[-1].ts

    for day in iter_days(start_day, end_day):
        key = day_key(day)
        day_snaps = by_day.get(day, [])
        row = DayRow(day=day, samples=len(day_snaps))
        if day_snaps:
            row.first_ts = day_snaps[0].ts
            row.last_ts = day_snaps[-1].ts
            row.close_fans = day_snaps[-1].fans
            row.base_fans = baseline
            row.base_ts = baseline_ts

        exact = creator.get(key)
        if exact is not None:
            new_fans, lost_fans = int(exact[0]), int(exact[1])
            row.new_fans = new_fans
            row.lost_fans = lost_fans
            row.source = SOURCE_CREATOR
            row.net = new_fans - lost_fans
            if row.close_fans is not None and baseline is not None:
                snap_net = row.close_fans - baseline
                if abs(snap_net - row.net) > max(tolerance, int(0.002 * max(1, baseline))):
                    row.note = f"采样净增 {snap_net:+d} 与官方 {row.net:+d} 不一致"
            row.est_new = est_new.get(day, 0) if day in by_day else None
            row.est_lost = est_lost.get(day, 0) if day in by_day else None
        elif day_snaps:
            row.source = SOURCE_TICK
            if day in tick_days:
                row.est_new = est_new.get(day, 0)
                row.est_lost = est_lost.get(day, 0)
            else:
                # 一天只有一条采样、或者前面完全没数据：算不出加减，别硬凑 0
                row.est_new = row.est_lost = None
            if baseline is not None and day in tick_days:
                row.net = row.close_fans - baseline
            if len(day_snaps) == 1:
                row.note = "当天只有 1 个采样点，估算不可靠"
        else:
            row.source = SOURCE_NONE
            row.note = "无数据"

        if (
            row.base_ts is not None
            and row.source != SOURCE_NONE
            and row.base_ts < day_start_ts(day, tz) - 93600
        ):
            extra = "前面断档超过一天，这段时间的变化无法拆到单日，这一行只显示总数"
            row.note = f"{row.note}；{extra}" if row.note else extra

        rows.append(row)
        if day_snaps:
            baseline = day_snaps[-1].fans
            baseline_ts = day_snaps[-1].ts

    return rows


def summarize(rows: Iterable[DayRow]) -> Summary:
    rows = list(rows)
    s = Summary(days=len(rows), day_rows=rows)
    fans_pool: list[int] = []
    for row in rows:
        new = row.display_new
        lost = row.display_lost
        if row.source == SOURCE_NONE:
            s.empty_days += 1
            continue
        if row.has_exact:
            s.exact_days += 1
        else:
            s.est_days += 1
        s.new_total += new or 0
        s.lost_total += lost or 0
        if row.net is not None:
            s.net_total += row.net
        if row.close_fans:
            fans_pool.append(row.close_fans)
        if row.first_ts is not None and s.first_fans is None:
            s.first_fans = row.base_fans if row.base_fans is not None else row.close_fans
        if row.close_fans is not None:
            s.last_fans = row.close_fans
        if new is not None and (s.best_new is None or new > (s.best_new.display_new or 0)):
            s.best_new = row
        if lost is not None and (s.worst_lost is None or lost > (s.worst_lost.display_lost or 0)):
            s.worst_lost = row

    active = s.exact_days + s.est_days
    if fans_pool and active:
        avg_fans = sum(fans_pool) / len(fans_pool)
        if avg_fans > 0:
            s.churn_rate = (s.lost_total / active) / avg_fans
    return s
