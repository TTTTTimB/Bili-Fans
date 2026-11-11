"""粉丝名单比对测试：这是「谁取关了我」功能的全部正确性所在。"""

from __future__ import annotations

from datetime import date

import pytest

from bili_fans.api import Fan, FanPage
from bili_fans.db import churn_rows, connect, fan_counts, latest_fan_crawl
from bili_fans.fans import crawl, summarize_churn, survival_days
from bili_fans.timeutil import day_start_ts, get_tz

TZ = get_tz("Asia/Shanghai")
DAY = 86400
NOW = day_start_ts(date(2026, 11, 12), TZ) + 3600


def fans_from(ids, base_follow=1_700_000_000):
    return [Fan(mid=i, uname=f"用户{i}", mtime=base_follow + i) for i in ids]


class FakeClient:
    """按页返回预置名单，模拟 x/relation/followers。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def followers(self, uid, pn=1, ps=50):
        self.calls += 1
        if pn - 1 >= len(self.pages):
            return FanPage(fans=[], page=pn, total=sum(len(p) for p in self.pages))
        return FanPage(fans=self.pages[pn - 1], page=pn, total=sum(len(p) for p in self.pages))


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "f.db")
    yield c
    c.close()


def test_first_crawl_builds_baseline_without_events(conn):
    client = FakeClient([fans_from(range(1, 51)), fans_from(range(51, 61))])
    out = crawl(client, conn, owner_uid=9, ts=NOW, sleep=lambda _: None)

    assert out.baseline is True
    assert out.fetched == 60
    assert out.complete is True
    assert out.gone == []
    alive, gone = fan_counts(conn, 9)
    assert (alive, gone) == (60, 0)
    # 首次抓取不能伪造 60 条「新关注」流水
    assert conn.execute("SELECT COUNT(*) AS n FROM fan_event").fetchone()["n"] == 0


def test_second_crawl_detects_who_unfollowed(conn):
    crawl(FakeClient([fans_from(range(1, 61))]), conn, owner_uid=9, ts=NOW - DAY, sleep=lambda _: None)

    # 3~5 号消失（取关），同时来了 100、101 号
    remaining = list(range(1, 3)) + list(range(6, 61)) + [100, 101]
    out = crawl(FakeClient([fans_from(remaining)]), conn, owner_uid=9, ts=NOW, sleep=lambda _: None)

    assert out.baseline is False
    assert [r.fan_uid for r in out.gone] == [3, 4, 5]
    assert {f.mid for f in out.new_fans} == {100, 101}
    assert out.complete is True

    alive, gone = fan_counts(conn, 9)
    assert alive == 59 and gone == 3

    rows = churn_rows(conn, 9, NOW - DAY)
    assert {r.fan_uid for r in rows} == {3, 4, 5}
    # 取关的人，昵称保留最后一次看到的样子
    assert rows[0].uname.startswith("用户")


def test_returning_fan_is_counted_again(conn):
    crawl(FakeClient([fans_from([1, 2, 3])]), conn, owner_uid=9, ts=NOW - 2 * DAY, sleep=lambda _: None)
    crawl(FakeClient([fans_from([1, 2])]), conn, owner_uid=9, ts=NOW - DAY, sleep=lambda _: None)
    out = crawl(FakeClient([fans_from([1, 2, 3])]), conn, owner_uid=9, ts=NOW, sleep=lambda _: None)

    assert [f.mid for f in out.new_fans] == [3]   # 3 号取关后又回来了
    assert out.gone == []
    alive, gone = fan_counts(conn, 9)
    assert (alive, gone) == (3, 0)


def test_suspiciously_short_crawl_skips_unfollow_detection(conn):
    crawl(FakeClient([fans_from(range(1, 101))]), conn, owner_uid=9, ts=NOW - DAY, sleep=lambda _: None)

    class Truncated:
        """接口自报 100 人，但只吐出 10 个就撞上限了 —— 典型的风控/截断。"""

        def followers(self, uid, pn=1, ps=50):
            if pn == 1:
                return FanPage(fans=fans_from(range(1, 11)), page=1, total=100)
            return FanPage(fans=[], page=pn, total=100, capped=True)

    out = crawl(Truncated(), conn, owner_uid=9, ts=NOW, sleep=lambda _: None)

    assert out.guard_skipped is True
    assert out.gone == []
    assert "跳过取关判定" in out.note
    assert fan_counts(conn, 9) == (100, 0)


def test_capped_response_marks_incomplete(conn):
    class CapClient:
        """大号常见情况：抓到第 2 页时 B 站返回 22007。"""

        def followers(self, uid, pn=1, ps=50):
            if pn == 1:
                return FanPage(fans=fans_from(range(1, 51)), page=1, total=5000)
            return FanPage(fans=[], page=pn, total=5000, capped=True)

    out = crawl(CapClient(), conn, owner_uid=9, ts=NOW, sleep=lambda _: None)
    assert out.complete is False
    assert out.fetched == 50
    crawl_row = latest_fan_crawl(conn, 9)
    assert crawl_row.complete is False
    assert "上限" in crawl_row.note


def test_limit_stops_pagination(conn):
    pages = [fans_from(range(i * 50, i * 50 + 50)) for i in range(6)]
    client = FakeClient(pages)
    out = crawl(client, conn, owner_uid=9, limit=120, ps=50, ts=NOW, sleep=lambda _: None)
    assert out.fetched == 120
    assert out.complete is False       # 还有 180 人没抓，不算全量
    assert client.calls == 3


def test_network_failure_marks_crawl_failed(conn):
    class Boom:
        def followers(self, uid, pn=1, ps=50):
            raise RuntimeError("网络炸了")

    with pytest.raises(RuntimeError):
        crawl(Boom(), conn, owner_uid=9, ts=NOW, sleep=lambda _: None)
    row = latest_fan_crawl(conn, 9, finished_only=False)
    assert row is not None and row.complete is False
    assert "中断" in row.note


def test_survival_days():
    assert survival_days(NOW - 10 * DAY, NOW) == 10
    assert survival_days(NOW, NOW) == 0
    assert survival_days(None, NOW) is None
    assert survival_days(NOW + DAY, NOW) is None      # 数据脏了就别算


def test_summarize_churn_counts_quick_quitters(conn):
    crawl(FakeClient([fans_from(range(1, 11))]), conn, owner_uid=9, ts=NOW - 30 * DAY, sleep=lambda _: None)
    out = crawl(FakeClient([fans_from(range(6, 11))]), conn, owner_uid=9, ts=NOW, sleep=lambda _: None)
    summary = summarize_churn(out.gone)
    assert summary["count"] == 5
    assert summary["known_since"] == 5
    assert 0 <= summary["quick_count"] <= 5
