"""存储层测试：幂等性最重要，定时任务会反复写同一天的数据。"""

from __future__ import annotations

import pytest

from bili_fans.api import CreatorDay, Fan
from bili_fans.db import (
    add_snapshot,
    fan_page,
    fan_totals,
    finish_fan_crawl,
    mark_missing_gone,
    recent_crawls,
    start_fan_crawl,
    upsert_fans,
    add_snapshots_bulk,
    connect,
    creator_days_between,
    delete_account,
    get_account,
    last_snapshot,
    list_accounts,
    snapshot_count,
    snapshots_between,
    upsert_account,
    upsert_creator_days,
)
from datetime import date


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def test_account_crud(conn):
    acc = upsert_account(conn, 100, "阿测试", "备注")
    assert acc.uid == 100 and acc.name == "阿测试"
    assert get_account(conn, 100).note == "备注"

    upsert_account(conn, 100, "", "")  # 空值不覆盖已有内容
    assert get_account(conn, 100).name == "阿测试"

    assert [a.uid for a in list_accounts(conn)] == [100]
    assert delete_account(conn, 100) > 0
    assert list_accounts(conn) == []


def test_label_humanises_missing_name(conn):
    acc = upsert_account(conn, 555)
    assert acc.label == "UID 555"
    assert upsert_account(conn, 556, "有名字").label == "有名字（UID 556）"


def test_snapshot_same_timestamp_is_upserted(conn):
    assert add_snapshot(conn, 1, 1000, 10) is True
    add_snapshot(conn, 1, 1000, 11)  # 同一秒重复采集
    assert snapshot_count(conn, 1) == 1
    assert last_snapshot(conn, 1).fans == 11


def test_snapshots_between_is_ordered(conn):
    for ts in (300, 100, 200):
        add_snapshot(conn, 1, ts, ts)
    rows = snapshots_between(conn, 1, 100, 250)
    assert [r.ts for r in rows] == [100, 200]


def test_bulk_insert(conn):
    n = add_snapshots_bulk(conn, [(1, 10, 100, None, "demo"), (1, 20, 105, None, "demo")])
    assert n == 2
    assert snapshot_count(conn, 1) == 2
    assert add_snapshots_bulk(conn, []) == 0


def test_creator_days_are_idempotent(conn):
    d1 = CreatorDay(date(2026, 11, 1), 5, 2, 1000)
    d2 = CreatorDay(date(2026, 11, 2), 9, 0, 1009)
    upsert_creator_days(conn, 7, [d1, d2])
    upsert_creator_days(conn, 7, [CreatorDay(date(2026, 11, 1), 6, 1, 1001)])  # 修正昨天的数
    got = creator_days_between(conn, 7, "2026-11-01", "2026-11-30")
    assert got == {"2026-11-01": (6, 1), "2026-11-02": (9, 0)}


def test_creator_range_filter(conn):
    upsert_creator_days(
        conn,
        7,
        [CreatorDay(date(2026, 10, 31), 1, 0), CreatorDay(date(2026, 11, 5), 2, 3)],
    )
    got = creator_days_between(conn, 7, "2026-11-01", "2026-11-30")
    assert got == {"2026-11-05": (2, 3)}


# ---------- 粉丝名单分页 / 筛选 ----------


def _seed_fans(conn, owner=7, count=30):
    fans = [Fan(mid=1000 + i, uname=f"粉丝{i:02d}", mtime=1_700_000_000 + i) for i in range(count)]
    upsert_fans(conn, owner, fans, 5_000)


def test_fan_totals_and_pagination(conn):
    _seed_fans(conn)
    assert fan_totals(conn, 7) == {"all": 30, "active": 30, "gone": 0}

    rows, total = fan_page(conn, 7, limit=10, offset=0)
    assert total == 30 and len(rows) == 10
    rows2, _ = fan_page(conn, 7, limit=10, offset=10)
    assert {r.fan_uid for r in rows} & {r.fan_uid for r in rows2} == set()   # 不重不漏

    rows, total = fan_page(conn, 7, limit=100)
    assert rows[0].fan_uid == 1029          # 关注时间新的在前
    assert all(r.active for r in rows)


def test_fan_page_status_filter_and_search(conn):
    _seed_fans(conn)
    # 只把 25 个人再刷一遍 last_seen，剩 5 个就成「取关」
    alive = [Fan(mid=1000 + i, uname=f"粉丝{i:02d}", mtime=1_700_000_000 + i) for i in range(25)]
    upsert_fans(conn, 7, alive, 6_000)
    gone = mark_missing_gone(conn, 7, 6_000)
    assert len(gone) == 5

    assert fan_totals(conn, 7) == {"all": 30, "active": 25, "gone": 5}
    rows, total = fan_page(conn, 7, status="gone")
    assert total == 5 and all(r.gone_ts == 6_000 for r in rows)

    rows, total = fan_page(conn, 7, query="粉丝01")
    assert total == 1 and rows[0].uname == "粉丝01"

    rows, total = fan_page(conn, 7, query="1011")   # 按 UID 搜
    assert total == 1 and rows[0].fan_uid == 1011


def test_recent_crawls(conn):
    cid = start_fan_crawl(conn, 7, 100)
    finish_fan_crawl(conn, cid, fetched=30, total=30, complete=True, ts=101)
    cid2 = start_fan_crawl(conn, 7, 200)
    finish_fan_crawl(conn, cid2, fetched=10, total=30, complete=False, note="撞上限", ts=201)

    rows = recent_crawls(conn, 7, limit=5)
    assert [r.fetched for r in rows] == [10, 30]     # 新的在前
    assert rows[0].complete is False and rows[0].note == "撞上限"


# ---------- 逐次抓取的名单留档 ----------


def test_save_and_read_snapshot(conn):
    from bili_fans.db import (save_fan_snapshot, snapshot_counts, snapshot_fans)

    cid = start_fan_crawl(conn, 7, 100)
    fans = [Fan(mid=3000 + i, uname=f"粉丝{i:02d}", mtime=1_700_000_000 + i) for i in range(5)]
    assert save_fan_snapshot(conn, cid, 7, fans) == 5

    rows, total = snapshot_fans(conn, cid)
    assert total == 5 and len(rows) == 5
    assert rows[0][0] == 3004                     # 关注时间新的在前

    rows, total = snapshot_fans(conn, cid, query="粉丝01")
    assert total == 1 and rows[0][1] == "粉丝01"

    assert snapshot_counts(conn, 7) == [(cid, 5)]


def test_snapshot_is_per_crawl(conn):
    from bili_fans.db import save_fan_snapshot, snapshot_fans

    c1 = start_fan_crawl(conn, 7, 100)
    save_fan_snapshot(conn, c1, 7, [Fan(mid=i, uname=f"f{i}") for i in (1, 2, 3)])
    c2 = start_fan_crawl(conn, 7, 200)
    save_fan_snapshot(conn, c2, 7, [Fan(mid=i, uname=f"f{i}") for i in (1, 2)])

    _, t1 = snapshot_fans(conn, c1)
    _, t2 = snapshot_fans(conn, c2)
    assert (t1, t2) == (3, 2)      # 两次抓取各自留档，互不影响


def test_snapshot_dedupes_within_one_crawl(conn):
    from bili_fans.db import save_fan_snapshot, snapshot_fans

    cid = start_fan_crawl(conn, 7, 100)
    save_fan_snapshot(conn, cid, 7, [Fan(mid=1, uname="a")])
    save_fan_snapshot(conn, cid, 7, [Fan(mid=1, uname="a")])
    _, total = snapshot_fans(conn, cid)
    assert total == 1
