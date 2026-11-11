"""网页看板的接口测试：真起一个服务，用 HTTP 打进去。"""

from __future__ import annotations

import http.client
import json
import threading
from urllib.parse import quote
from datetime import timedelta

import pytest

from bili_fans.api import CreatorDay, Fan
from bili_fans.config import Config
from bili_fans.db import (
    add_snapshot,
    connect,
    mark_missing_gone,
    upsert_account,
    upsert_creator_days,
    upsert_fans,
)
from bili_fans.server import build_overview, make_server
from bili_fans.timeutil import day_start_ts, get_tz, local_day, now_ts

TZ = get_tz("Asia/Shanghai")
DAY = 86400


@pytest.fixture()
def env(tmp_path):
    db = tmp_path / "gui.db"
    conn = connect(db)
    upsert_account(conn, 100, "测试号")
    today = local_day(now_ts(), TZ)
    fans = 1000
    days = []
    for i in range(6):
        day = today - timedelta(days=5 - i)
        for h in (9, 21):
            fans += 5
            add_snapshot(conn, 100, day_start_ts(day, TZ) + h * 3600, fans)
        days.append(CreatorDay(day=day, new_fans=12, lost_fans=4, total_fans=fans))
    upsert_creator_days(conn, 100, days)

    # 12 个粉丝，其中 2 个已取关
    fans = [Fan(mid=2000 + i, uname=f"粉丝{i:02d}", mtime=now_ts() - (i + 1) * DAY) for i in range(12)]
    upsert_fans(conn, 100, fans, now_ts() - 60)
    alive = [f for f in fans if f.mid not in (2000, 2005)]
    upsert_fans(conn, 100, alive, now_ts())
    mark_missing_gone(conn, 100, now_ts())
    conn.close()

    cfg = Config(db=str(db))
    httpd, port = make_server(cfg, port=0, cookie="")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"port": port, "cfg": cfg, "db": db}
    finally:
        httpd.shutdown()
        httpd.server_close()


def request(env, path, *, method="GET", body=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", env["port"], timeout=5)
    headers = {}
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    if host:
        headers["Host"] = host
    # 中文查询参数要 percent-encode，浏览器也是这么发的
    conn.request(method, quote(path, safe="/:?=&"), body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", "replace")
    conn.close()
    return resp.status, raw


def test_index_page_renders(env):
    status, body = request(env, "/")
    assert status == 200
    assert "粉丝变化看板" in body
    assert "谁取关了我" in body


def test_overview_returns_rows_and_charts(env):
    status, body = request(env, "/api/overview?days=7")
    assert status == 200
    data = json.loads(body)
    assert data["uid"] == 100
    assert data["has_data"] is True
    assert data["kpi"]["new_total"] == 72
    assert data["kpi"]["lost_total"] == 24
    assert len(data["rows"]) == 6
    assert data["charts"]["bars"].startswith("<svg")
    assert data["charts"]["line"].startswith("<svg")
    assert data["churn"]["ready"] is False          # 还没抓过粉丝名单


def test_overview_without_accounts(tmp_path):
    db = tmp_path / "empty.db"
    connect(db).close()
    cfg = Config(db=str(db))
    conn = connect(db)
    try:
        data = build_overview(conn, 30, cfg, TZ, "")
    finally:
        conn.close()
    assert data["empty"] == "no_account"
    assert "accounts" not in data          # 已经取消了账号切换


def test_unknown_uid_falls_back_to_first_account(env):
    status, body = request(env, "/api/overview?uid=999&days=7")
    data = json.loads(body)
    assert status == 200
    assert data["uid"] == 100


def test_days_is_clamped(env):
    status, body = request(env, "/api/overview?days=99999")
    assert status == 200
    assert json.loads(body)["days"] == 365


def test_run_job_requires_known_action(env):
    status, body = request(env, "/api/run", method="POST", body={"action": "rm-rf"})
    assert status == 400


def test_run_job_without_cookie_reports_friendly_error(env):
    status, body = request(env, "/api/run", method="POST", body={"action": "fans", "uid": 100})
    assert status == 200
    job_id = json.loads(body)["job"]

    for _ in range(50):
        status, body = request(env, f"/api/job?id={job_id}")
        job = json.loads(body)
        if job["status"] != "running":
            break
        threading.Event().wait(0.05)

    assert job["status"] == "done"
    assert any("Cookie" in line for line in job["lines"])


def test_unknown_job_returns_404(env):
    status, _ = request(env, "/api/job?id=nope")
    assert status == 404


def test_unknown_path_returns_404(env):
    status, _ = request(env, "/api/nothing")
    assert status == 404


def test_rejects_foreign_host_header(env):
    """挡掉 DNS rebinding：只有本机 Host 才放行。"""
    status, _ = request(env, "/", host="evil.example.com")
    assert status == 403


# ---------------- 粉丝名单 ----------------


def test_fans_endpoint_returns_everyone(env):
    status, body = request(env, "/api/fans")
    assert status == 200
    data = json.loads(body)
    assert data["total"] == 12
    assert data["totals"] == {"all": 12, "active": 10, "gone": 2}
    assert len(data["items"]) == 12
    item = data["items"][0]
    assert set(item) == {"uid", "uname", "followed", "gone", "last_seen", "active", "mutual"}


def test_fans_endpoint_filters_by_status(env):
    status, body = request(env, "/api/fans?status=gone")
    data = json.loads(body)
    assert data["total"] == 2
    assert all(not it["active"] for it in data["items"])
    assert all(it["gone"] for it in data["items"])


def test_fans_endpoint_search_by_name_and_uid(env):
    _, body = request(env, "/api/fans?q=粉丝0")
    assert json.loads(body)["total"] == 10

    _, body = request(env, "/api/fans?q=2003")
    data = json.loads(body)
    assert data["total"] == 1
    assert data["items"][0]["uid"] == 2003


def test_fans_endpoint_pagination(env):
    _, body = request(env, "/api/fans?size=5&page=3")
    data = json.loads(body)
    assert data["total"] == 12
    assert len(data["items"]) == 2          # 最后一页只剩 2 个
    assert data["page"] == 3


def test_fans_endpoint_rejects_bad_status(env):
    _, body = request(env, "/api/fans?status=hack")
    assert json.loads(body)["status"] == "all"


def test_cookie_shows_up_without_restart(env, monkeypatch, tmp_path):
    """看板开着的时候去 login，刷新一下就该认出新 Cookie。"""
    from bili_fans import browser

    cookie_file = tmp_path / "cookie"
    monkeypatch.setattr(browser, "COOKIE_FILE", cookie_file)
    monkeypatch.delenv("BILI_COOKIE", raising=False)

    _, body = request(env, "/api/overview")
    assert json.loads(body)["cookie_ok"] is False

    browser.save_cookie(cookie_file, "SESSDATA=fresh")
    _, body = request(env, "/api/overview")
    assert json.loads(body)["cookie_ok"] is True


def test_health_endpoint_reports_version(env):
    status, body = request(env, "/api/health")
    assert status == 200
    data = json.loads(body)
    assert data["app"] == "bilifans" and data["ok"] is True
    assert data["version"]
    assert isinstance(data["started"], int)
    assert data["stale"] in (True, False)


def test_overview_flags_stale_code(env, monkeypatch):
    from bili_fans import server

    _, body = request(env, "/api/overview")
    assert json.loads(body)["meta"]["stale"] is False

    monkeypatch.setattr(server, "is_stale", lambda: True)
    _, body = request(env, "/api/overview")
    assert json.loads(body)["meta"]["stale"] is True




def test_manual_cookie_paste(env, monkeypatch, tmp_path):
    """扫码一直不行时的兜底：直接粘贴 Cookie。"""
    from bili_fans import browser

    cookie_file = tmp_path / "cookie"
    monkeypatch.setattr(browser, "COOKIE_FILE", cookie_file)

    status, body = request(env, "/api/cookie", method="POST",
                           body={"text": "cookie: SESSDATA=abc123def456ghi789jkl; bili_jct=xyz"})
    assert status == 200
    assert json.loads(body)["ok"] is True
    assert cookie_file.read_text().strip() == "SESSDATA=abc123def456ghi789jkl; bili_jct=xyz"


def test_manual_cookie_rejects_garbage(env, monkeypatch, tmp_path):
    from bili_fans import browser

    monkeypatch.setattr(browser, "COOKIE_FILE", tmp_path / "cookie")
    status, body = request(env, "/api/cookie", method="POST", body={"text": "hello"})
    assert status == 400
    assert "SESSDATA" in json.loads(body)["error"]


def test_fans_endpoint_snapshot_mode(env):
    """看某一次抓取留档的名单。"""
    from bili_fans.api import Fan
    from bili_fans.db import connect, save_fan_snapshot, start_fan_crawl

    conn = connect(env["db"])
    try:
        cid = start_fan_crawl(conn, 100, 1_700_000_000)
        save_fan_snapshot(conn, cid, 100, [Fan(mid=9000 + i, uname=f"存档{i}", mtime=1_700_000_000 + i) for i in range(4)])
    finally:
        conn.close()

    status, body = request(env, f"/api/fans?crawl={cid}")
    assert status == 200
    data = json.loads(body)
    assert data["mode"] == "snapshot" and data["crawl"] == cid
    assert data["total"] == 4
    assert all(it["uname"].startswith("存档") for it in data["items"])

    # 搜索也支持
    _, body = request(env, f"/api/fans?crawl={cid}&q=存档2")
    assert json.loads(body)["total"] == 1


def test_overview_lists_crawl_ids(env):
    from bili_fans.db import connect, finish_fan_crawl, start_fan_crawl

    conn = connect(env["db"])
    try:
        cid = start_fan_crawl(conn, 100, 1_700_000_000)
        finish_fan_crawl(conn, cid, fetched=10, total=10, complete=True, ts=1_700_000_100)
    finally:
        conn.close()

    _, body = request(env, "/api/overview")
    crawls = json.loads(body)["meta"]["crawls"]
    assert crawls and crawls[0]["id"] == cid
