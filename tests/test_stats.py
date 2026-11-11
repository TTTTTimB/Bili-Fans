"""统计逻辑的单元测试：这是整个软件最需要正确性的地方。"""

from __future__ import annotations

from datetime import date

import pytest

from bili_fans.db import Snapshot
from bili_fans.stats import (
    SOURCE_CREATOR,
    SOURCE_NONE,
    SOURCE_TICK,
    build_rows,
    summarize,
)
from bili_fans.timeutil import day_start_ts, get_tz

TZ = get_tz("Asia/Shanghai")
D1, D2, D3 = date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)


def at(day: date, hour: int, minute: int = 0) -> int:
    return day_start_ts(day, TZ) + hour * 3600 + minute * 60


def test_net_uses_previous_close_as_baseline():
    snaps = [
        Snapshot(at(D1, -1, 0), 1000),  # 前一天 23:00，作为基准
        Snapshot(at(D1, 9), 1005),
        Snapshot(at(D1, 21), 1003),
        Snapshot(at(D2, 12), 1010),
    ]
    rows = build_rows(snaps, {}, D1, D2, TZ)
    assert [r.net for r in rows] == [3, 7]
    assert rows[0].close_fans == 1003
    assert rows[0].base_fans == 1000


def test_estimate_separates_gain_and_loss():
    snaps = [
        Snapshot(at(D1, -1, 0), 1000),
        Snapshot(at(D1, 9), 1005),   # +5
        Snapshot(at(D1, 21), 1003),  # -2
    ]
    rows = build_rows(snaps, {}, D1, D1, TZ)
    row = rows[0]
    assert (row.est_new, row.est_lost) == (5, 2)
    assert row.net == 3               # 净增 = 5 - 2
    assert row.source == SOURCE_TICK
    assert not row.has_exact


def test_creator_numbers_win_and_are_marked_exact():
    snaps = [
        Snapshot(at(D1, -1, 0), 1000),
        Snapshot(at(D1, 21), 1003),
    ]
    rows = build_rows(snaps, {"2026-11-01": (6, 1)}, D1, D1, TZ)
    row = rows[0]
    assert row.source == SOURCE_CREATOR
    assert row.has_exact
    assert (row.new_fans, row.lost_fans, row.net) == (6, 1, 5)
    assert (row.est_new, row.est_lost) == (3, 0)  # 估算仍在，便于对比


def test_creator_only_day_without_snapshots():
    rows = build_rows([], {"2026-11-02": (10, 4)}, D2, D2, TZ)
    row = rows[0]
    assert row.source == SOURCE_CREATOR
    assert row.net == 6
    assert row.close_fans is None
    assert row.samples == 0


def test_missing_day_is_none_not_zero():
    rows = build_rows([], {}, D1, D3, TZ)
    assert [r.source for r in rows] == [SOURCE_NONE] * 3
    assert all(r.net is None and r.display_new is None for r in rows)


def test_gap_larger_than_a_day_is_dropped_from_estimate():
    snaps = [
        Snapshot(at(D1, -1, 0), 1000),
        Snapshot(at(D1, 9), 1010),
        # 11-02 整天没采
        Snapshot(at(D3, 12), 1040),
    ]
    rows = build_rows(snaps, {}, D1, D3, TZ)
    d2, d3 = rows[1], rows[2]
    assert d2.source == SOURCE_NONE
    # 11-01 09:00 -> 11-03 12:00 跨了两天，既算不出单日毛新增，也拆不出单日净增
    assert d3.est_new is None and d3.est_lost is None
    assert d3.net is None
    assert "断档" in d3.note
    # 但总粉丝数照样记下来了
    assert d3.close_fans == 1040


def test_single_sample_day_is_flagged():
    snaps = [Snapshot(at(D1, -1, 0), 100), Snapshot(at(D1, 15), 108)]
    row = build_rows(snaps, {}, D1, D1, TZ)[0]
    assert "只有 1 个采样点" in row.note


def test_mismatch_between_snapshot_and_creator_is_noted():
    snaps = [Snapshot(at(D1, -1, 0), 1000), Snapshot(at(D1, 22), 1100)]
    rows = build_rows(snaps, {"2026-11-01": (50, 10)}, D1, D1, TZ)
    assert rows[0].note.startswith("采样净增")
    assert "100" in rows[0].note


def test_summary_totals_and_fallbacks():
    snaps = [
        Snapshot(at(D1, -1, 0), 1000),
        Snapshot(at(D1, 12), 1010),
        Snapshot(at(D2, 12), 1015),
        Snapshot(at(D3, 12), 1009),
    ]
    creator = {"2026-11-01": (12, 2), "2026-11-02": (8, 3)}
    rows = build_rows(snaps, creator, D1, D3, TZ)
    s = summarize(rows)

    assert (s.exact_days, s.est_days) == (2, 1)
    # 11-03 没有官方数据，退回采样估算：1015 -> 1009 全算掉粉
    assert s.lost_total == 2 + 3 + 6
    assert s.new_total == 12 + 8 + 0
    assert s.net_total == (10) + (5) + (-6)
    assert s.days == 3
    assert s.best_new is rows[0]
    assert s.worst_lost is rows[2]


def test_summary_is_safe_on_empty_input():
    s = summarize([])
    assert s.days == 0
    assert s.avg_new == 0.0
    assert s.churn_rate is None
    assert s.exact_ratio == 0.0


def test_churn_rate_uses_average_scale():
    snaps = [Snapshot(at(D1, 9), 10_000), Snapshot(at(D1, 21), 9_900)]
    s = summarize(build_rows(snaps, {}, D1, D1, TZ))
    # 掉粉 100 / 粉丝 9900 ≈ 1.01%
    assert s.churn_rate == pytest.approx(100 / 9900, rel=1e-6)
