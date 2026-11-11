"""本地网页看板：`bilifans serve` 打开 http://127.0.0.1:8787

只监听本机回环地址，不对外网开放；页面里的所有数据都来自本地 SQLite。
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import __version__
from .api import ApiError, BiliClient
from .config import Config
from .db import (
    Account,
    churn_rows,
    connect,
    fan_counts,
    fan_page,
    fan_totals,
    last_snapshot,
    latest_fan_crawl,
    list_accounts,
    recent_crawls,
    snapshot_count,
    upsert_account,
)
from . import login as qrlogin
from .fans import crawl as crawl_fans
from .fans import summarize_churn, survival_days
from .http import HttpError, Transport
from .qr import encode as qr_encode
from .qr import to_svg as qr_svg
from .report import bar_chart_svg, line_chart_svg
from .service import backfill_account, collect, ensure_account, poll_account, resolve_cookie
from .timeutil import day_start_ts, get_tz, local_day, now_ts, to_local

DEFAULT_PORT = 8787
MAX_LOG_LINES = 200
QR_TTL = 180          # B 站二维码的有效期（秒）

STARTED = time.time()


def _code_mtime() -> float:
    """源码最后修改时间，用来判断「跑的是不是旧代码」。"""
    root = Path(__file__).resolve().parent
    return max((f.stat().st_mtime for f in root.glob("*.py")), default=0.0)


def is_stale() -> bool:
    """请求时重新 stat 一次：源码比进程启动还新，说明跑的是旧代码。"""
    return _code_mtime() > STARTED + 1


# ---------------------------------------------------------------- 后台任务


@dataclass
class Job:
    id: str
    action: str
    status: str = "running"          # running / done / failed
    lines: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float | None = None

    def log(self, line: str) -> None:
        self.lines.append(str(line))
        del self.lines[:-MAX_LOG_LINES]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "status": self.status,
            "lines": self.lines,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
        }


class JobRunner:
    """网页上点按钮会跑几十秒（抓 1000 个粉丝要 20 次请求），所以放后台线程。"""

    def __init__(self, keep: int = 20) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._seq = 0
        self._keep = keep

    def start(self, action: str, fn: Callable[[Callable[[str], None]], None]) -> Job:
        with self._lock:
            self._seq += 1
            job = Job(id=f"{int(time.time())}-{self._seq}", action=action)
            self._jobs[job.id] = job
            self._order.append(job.id)
            for old in self._order[:-self._keep]:
                self._jobs.pop(old, None)
            self._order = self._order[-self._keep:]
        thread = threading.Thread(target=self._run, args=(job, fn), daemon=True)
        thread.start()
        return job

    def _run(self, job: Job, fn: Callable[[Callable[[str], None]], None]) -> None:
        try:
            fn(job.log)
            job.status = "done"
        except Exception as exc:  # 后台任务不能把整个服务弄挂
            job.log(f"✗ {exc}")
            job.status = "failed"
        finally:
            job.finished = time.time()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)


class LoginStore:
    """扫码登录的会话：内存里存一会儿，10 分钟没动静就清掉。"""

    def __init__(self, ttl: float = 600.0, done_ttl: float = 120.0) -> None:
        self._items: dict[str, tuple[object, float]] = {}
        # 已经登录成功的会话：B 站的码只能用一次，但前端可能并发/重试，
        # 所以把结果缓存下来，重复轮询直接返回成功，别让「一次成功、一次失效」打架
        self._done: dict[str, tuple[dict, float]] = {}
        self._lock = threading.Lock()
        self._seq = 0
        self._ttl = ttl
        self._done_ttl = done_ttl

    def add(self, session) -> str:
        with self._lock:
            self._seq += 1
            sid = f"qr{int(time.time())}-{self._seq}"
            self._prune()
            self._items[sid] = (session, time.time())
            return sid

    def get(self, sid: str):
        with self._lock:
            item = self._items.get(sid)
            if not item:
                return None
            session, created = item
            return session if time.time() - created < self._ttl else None

    def drop(self, sid: str) -> None:
        with self._lock:
            self._items.pop(sid, None)

    def complete(self, sid: str, payload: dict) -> None:
        with self._lock:
            self._done[sid] = (payload, time.time())

    def get_done(self, sid: str) -> dict | None:
        with self._lock:
            item = self._done.get(sid)
            if not item:
                return None
            payload, created = item
            return payload if time.time() - created < self._done_ttl else None

    def _prune(self) -> None:
        now = time.time()
        for sid in [k for k, (_, t) in self._items.items() if now - t > self._ttl]:
            self._items.pop(sid, None)
        for sid in [k for k, (_, t) in self._done.items() if now - t > self._done_ttl]:
            self._done.pop(sid, None)


# ---------------------------------------------------------------- 数据组装


def _fmt_ts(ts: int | None, tz) -> str:
    if not ts:
        return ""
    return to_local(ts, tz).strftime("%m-%d %H:%M")


def build_overview(conn, uid: int | None, days: int, cfg: Config, tz, cookie: str) -> dict[str, Any]:
    accounts = list_accounts(conn)
    payload: dict[str, Any] = {
        "accounts": [{"uid": a.uid, "name": a.name or "未命名", "label": a.label} for a in accounts],
        "days": days,
        "cookie_ok": bool(cookie),
        "tz": cfg.timezone,
    }
    if not accounts:
        payload["empty"] = "no_account"
        return payload

    if uid is None or all(a.uid != uid for a in accounts):
        uid = accounts[0].uid
    acc = next(a for a in accounts if a.uid == uid)

    rows, summary = collect(conn, acc.uid, days, tz)
    last = last_snapshot(conn, acc.uid)
    alive, gone_total = fan_counts(conn, acc.uid)
    crawl_row = latest_fan_crawl(conn, acc.uid)

    active = summary.exact_days + summary.est_days
    payload.update(
        {
            "uid": acc.uid,
            "name": acc.name or "未命名",
            "label": acc.label,
            "has_data": active > 0,
            "warmup": active < 2,   # 只有一天数据时画不出趋势，前端换成引导文案
            "range": {
                "start": rows[0].day.isoformat() if rows else "",
                "end": rows[-1].day.isoformat() if rows else "",
            },
            "kpi": {
                "new_total": summary.new_total,
                "lost_total": summary.lost_total,
                "net_total": summary.net_total,
                "avg_new": round(summary.avg_new, 1),
                "avg_lost": round(summary.avg_lost, 1),
                "churn_rate": summary.churn_rate,
                "exact_days": summary.exact_days,
                "est_days": summary.est_days,
                "days": len(rows),
                "last_fans": summary.last_fans,
            },
            "charts": {
                "bars": bar_chart_svg(rows) if active else "",
                "line": line_chart_svg(rows) if active else "",
            },
            "rows": [
                {
                    "day": r.day.isoformat(),
                    "fans": r.close_fans,
                    "net": r.net,
                    "new": r.display_new,
                    "lost": r.display_lost,
                    "exact": r.has_exact,
                    "samples": r.samples,
                    "source": r.source,
                    "note": r.note,
                }
                for r in rows
            ],
            "churn": _churn_payload(conn, acc, days, tz, crawl_row),
            "meta": {
                "stale": is_stale(),
                "snapshots": snapshot_count(conn, acc.uid),
                "last_poll": _fmt_ts(last.ts if last else None, tz),
                "last_poll_ts": last.ts if last else None,
                "fans_tracked": alive,
                "fans_gone": gone_total,
                "last_crawl": _fmt_ts(crawl_row.started_ts, tz) if crawl_row else "",
                "crawl_complete": bool(crawl_row and crawl_row.complete),
                "crawls": [
                    {
                        "at": _fmt_ts(c.started_ts, tz),
                        "fetched": c.fetched,
                        "total": c.total,
                        "complete": c.complete,
                        "note": c.note,
                    }
                    for c in recent_crawls(conn, acc.uid, limit=3)
                ],
            },
        }
    )
    return payload


def _churn_payload(conn, acc: Account, days: int, tz, crawl_row) -> dict[str, Any]:
    today = local_day(now_ts(), tz)
    start = today - timedelta(days=max(1, days) - 1)
    rows = churn_rows(conn, acc.uid, day_start_ts(start, tz))
    summary = summarize_churn(rows)

    if crawl_row is None or crawl_row.fetched == 0:
        note = "还没有抓过粉丝名单，先点「抓粉丝名单」（需要 Cookie），抓两次之后才会出现名单。"
        ready = False
    elif not crawl_row.complete:
        total = f"，总粉丝 {crawl_row.total:,}" if crawl_row.total else ""
        note = (
            f"上次只覆盖最近 {crawl_row.fetched} 名粉丝{total}，更早的粉丝取关检测不到"
            "（B 站限制：登录后最多看前 1000 名粉丝）。"
        )
        ready = True
    else:
        note = ""
        ready = True

    return {
        "ready": ready,
        "note": note,
        "count": summary["count"],
        "quick_count": summary["quick_count"],
        "avg_survival": summary["avg_survival"],
        "items": [
            {
                "uname": r.uname or "（已注销）",
                "uid": r.fan_uid,
                "followed": to_local(r.mtime, tz).strftime("%Y-%m-%d") if r.mtime else "—",
                "gone": to_local(r.gone_ts, tz).strftime("%m-%d"),
                "survival": survival_days(r.mtime, r.gone_ts),
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------- HTTP


class AppHandler(BaseHTTPRequestHandler):
    cfg: Config
    cookie_override: str = ""     # serve --cookie 传进来的话就固定用它
    tz = None
    jobs: JobRunner
    logins: LoginStore
    server_version = "bilifans"

    # 关掉逐条请求日志，控制台只留关键信息
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    # ---------- 工具 ----------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _ok_host(self) -> bool:
        """只允许本机访问，顺手挡掉 DNS rebinding。"""
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def _db(self):
        return connect(self.cfg.db_path())

    def _cookie(self) -> str:
        """每个请求都重新解析一次，这样看板开着的时候去 login，刷新就生效。"""
        if self.cookie_override:
            return self.cookie_override
        try:
            return resolve_cookie(self.cfg)[0]
        except Exception:
            return ""

    def _client(self) -> BiliClient:
        cookie = self._cookie()
        transport = Transport(timeout=self.cfg.timeout, retries=self.cfg.retries, cookie=cookie)
        return BiliClient(transport, cookie=cookie)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _query(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _uid(self, raw: str | None) -> int | None:
        try:
            return int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802
        if not self._ok_host():
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return

        if path == "/api/overview":
            q = self._query()
            days = max(1, min(365, int(q.get("days") or 30)))
            conn = self._db()
            try:
                payload = build_overview(conn, self._uid(q.get("uid")), days, self.cfg, self.tz, self._cookie())
            finally:
                conn.close()
            self._json(payload)
            return

        if path == "/api/fans":
            q = self._query()
            conn = self._db()
            try:
                accounts = list_accounts(conn)
                if not accounts:
                    self._json({"items": [], "total": 0, "page": 1, "size": 50,
                                "totals": {"all": 0, "active": 0, "gone": 0}})
                    return
                uid = self._uid(q.get("uid")) or accounts[0].uid
                status = q.get("status") or "all"
                if status not in ("all", "active", "gone"):
                    status = "all"
                try:
                    page = max(1, int(q.get("page") or 1))
                except ValueError:
                    page = 1
                try:
                    size = min(200, max(1, int(q.get("size") or 50)))
                except ValueError:
                    size = 50
                rows, total = fan_page(
                    conn, uid, status=status, query=q.get("q", ""), limit=size, offset=(page - 1) * size
                )
                totals = fan_totals(conn, uid)
            finally:
                conn.close()
            self._json({
                "uid": uid,
                "status": status,
                "page": page,
                "size": size,
                "total": total,
                "totals": totals,
                "items": [
                    {
                        "uid": r.fan_uid,
                        "uname": r.uname or "（已注销）",
                        "followed": to_local(r.mtime, self.tz).strftime("%Y-%m-%d") if r.mtime else "—",
                        "gone": to_local(r.gone_ts, self.tz).strftime("%Y-%m-%d") if r.gone_ts else "",
                        "last_seen": to_local(r.last_seen_ts, self.tz).strftime("%m-%d"),
                        "active": r.active,
                        "mutual": r.attribute == 6,
                    }
                    for r in rows
                ],
            })
            return

        if path == "/api/job":
            job = self.jobs.get(self._query().get("id", ""))
            if not job:
                self._json({"error": "not found"}, 404)
            else:
                self._json(job.as_dict())
            return

        if path == "/api/login/qr":
            self._login_poll(self._query().get("id", ""))
            return

        if path == "/api/health":
            self._json({
                "app": "bilifans",
                "ok": True,
                "started": int(STARTED),
                "stale": is_stale(),
                "version": __version__,
            })
            return

        if path == "/favicon.png":
            from pathlib import Path as _Path

            icon = _Path(__file__).resolve().parents[2] / "assets" / "icon.png"
            try:
                self._send(200, icon.read_bytes(), "image/png")
            except OSError:
                self._send(204, b"", "image/png")
            return

        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._ok_host():
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        path = urlparse(self.path).path

        if path == "/api/login/qr":
            self._login_start()
            return

        if path == "/api/account":
            body = self._body()
            try:
                uid = int(body.get("uid"))
            except (TypeError, ValueError):
                self._json({"error": "UID 必须是数字"}, 400)
                return
            if uid <= 0:
                self._json({"error": "UID 不合法"}, 400)
                return
            conn = self._db()
            try:
                client = self._client()
                name = str(body.get("name") or "").strip()
                if not name:
                    try:
                        profile = client.profile(uid)
                        name = profile.name if profile else ""
                    except (ApiError, HttpError):
                        name = ""
                acc = upsert_account(conn, uid, name, "")
            finally:
                conn.close()
            self._json({"ok": True, "uid": acc.uid, "label": acc.label})
            return

        if path == "/api/run":
            body = self._body()
            action = str(body.get("action") or "")
            uid = self._uid(str(body.get("uid") or ""))
            if action not in ("poll", "backfill", "fans", "login"):
                self._json({"error": "未知操作"}, 400)
                return
            job = self.jobs.start(action, self._make_task(action, uid))
            self._json({"ok": True, "job": job.id, "action": action})
            return

        self._json({"error": "not found"}, 404)

    # ---------- 后台任务 ----------

    def _targets(self, conn, uid: int | None) -> list[Account]:
        accounts = list_accounts(conn)
        if uid is None:
            return accounts
        return [acc for acc in accounts if acc.uid == uid] or [ensure_account(conn, uid)]

    def _login_start(self) -> None:
        """生成一张二维码。不需要登录态。"""
        client = self._client()
        try:
            session = qrlogin.begin(client)
        except (ApiError, HttpError) as exc:
            self._json({"error": str(exc)}, 502)
            return
        sid = self.logins.add(session)
        self._json({
            "id": sid,
            "svg": qr_svg(qr_encode(session.url), scale=6),
            "ttl": QR_TTL,
            "cookie_ok": bool(self._cookie()),
        })

    def _login_poll(self, sid: str) -> None:
        cached = self.logins.get_done(sid)
        if cached is not None:
            self._json(cached)
            return

        session = self.logins.get(sid)
        if session is None:
            self._json({"status": "expired", "error": "二维码已失效，请刷新"}, 404)
            return
        client = session.client or self._client()
        try:
            state = qrlogin.check(client, session)
        except (ApiError, HttpError) as exc:
            self._json({"status": "waiting", "error": str(exc), "age": round(session.age(), 1)})
            return

        payload: dict[str, object] = {
            "status": state.status,
            "uid": state.values.get("DedeUserID", ""),
            "age": round(session.age(), 1),
            "ttl": QR_TTL,
            "cookie_ok": bool(self._cookie()),
        }
        if state.status == "confirmed":
            from . import browser

            cookie, _values = qrlogin.finalize(client, state)
            try:
                path = browser.save_cookie(browser.COOKIE_FILE, cookie)
                payload["saved"] = str(path)
            except OSError as exc:
                payload["error"] = f"Cookie 保存失败：{exc}"
            payload["cookie_ok"] = True
            self.logins.complete(sid, payload)
            self.logins.drop(sid)
        self._json(payload)

    def _login_task(self, log: Callable[[str], None]) -> None:
        """在页面里点「登录」，等于在终端跑 bilifans login。"""
        from . import browser

        log("正在启动浏览器…（首次需要在弹出的窗口里扫码登录 B 站）")
        log("  登录状态会被这个浏览器 profile 记住，以后不用再扫")

        def on_wait(left: float) -> None:
            log(f"  等待扫码…还剩 {int(left)} 秒")

        try:
            result = browser.capture_cookie(timeout=180, on_wait=on_wait)
        except browser.BrowserError as exc:
            log(f"✗ {exc}")
            log("  也可以按 docs/get-cookie.md 手动复制 Cookie")
            return

        path = browser.save_cookie(browser.COOKIE_FILE, result.cookie)
        log(f"✓ 已保存到 {path}（权限 600）")
        log(f"  {result.masked}")
        log("  回到页面会自动显示「Cookie 已配置」，接下来可以回填数据、抓粉丝名单了")

    def _make_task(self, action: str, uid: int | None) -> Callable[[Callable[[str], None]], None]:
        def task(log: Callable[[str], None]) -> None:
            if action == "login":
                self._login_task(log)
                return

            conn = self._db()
            try:
                accounts = self._targets(conn, uid)
                if not accounts:
                    log("还没有添加任何账号")
                    return
                client = self._client()

                if action == "poll":
                    for acc in accounts:
                        ok, msg = poll_account(conn, client, acc, self.tz)
                        log(("✓ " if ok else "✗ ") + msg)

                elif action == "backfill":
                    cookie = self._cookie()
                    if not cookie:
                        log("✗ 未配置 Cookie，无法读取创作中心数据")
                        log("  先运行 bilifans login，然后点右上角「刷新」再试")
                        return
                    for i, acc in enumerate(accounts):
                        log(f"→ {acc.label}")
                        ok, msg = backfill_account(
                            conn, client, acc, cookie=cookie, creator_url=self.cfg.creator_url, log=log
                        )
                        log(("✓ " if ok else "✗ ") + msg)
                        if i + 1 < len(accounts):
                            time.sleep(0.6)

                elif action == "fans":
                    if not self._cookie():
                        log("✗ 未配置 Cookie，B 站要求登录才给看粉丝列表")
                        return
                    owner = client.nav()
                    for i, acc in enumerate(accounts):
                        log(f"→ {acc.label}")
                        if owner and owner[0] != acc.uid:
                            log(f"✗ Cookie 登录的是 UID {owner[0]}（{owner[1]}），不是 {acc.uid}，已跳过")
                            continue
                        out = crawl_fans(client, conn, acc.uid, progress=log)
                        log(f"✓ 名单 {out.fetched} 人 · {out.coverage}")
                        if out.baseline:
                            log("  首次抓取，已建基线；再抓一次才能看出谁取关了")
                        else:
                            log(f"  新关注 {len(out.new_fans)} 人 · 确认取关 {len(out.gone)} 人")
                            for row in out.gone[:20]:
                                log(f"    - {row.uname or row.fan_uid}（{row.fan_uid}）")
                        if out.guard_skipped:
                            log(f"  ⚠ {out.note}")
                        if i + 1 < len(accounts):
                            time.sleep(0.4)
            finally:
                conn.close()

        return task


# ---------------------------------------------------------------- 页面

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/png" href="/favicon.png">
<title>粉丝变化看板</title>
<style>
:root{
  --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; --bg:#f7f8fa; --card:#fff;
  --up:#16a34a; --down:#e11d48; --accent:#2563eb; --warn:#c2410c;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:24px 20px 64px}
header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:6px}
h1{font-size:20px;margin:0;letter-spacing:.01em}
.grow{flex:1}
select,button{font:inherit;color:inherit}
select{padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:#fff}
button{cursor:pointer;border:1px solid var(--line);background:#fff;border-radius:8px;padding:7px 13px;
  transition:.15s;white-space:nowrap}
button:hover{border-color:#cbd5e1;background:#f8fafc}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.primary:hover{background:#1d4ed8}
button:disabled{opacity:.5;cursor:not-allowed}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#fff}
.seg button{border:0;border-radius:0;padding:6px 12px}
.seg button.on{background:var(--ink);color:#fff}
.sub{color:var(--muted);font-size:12.5px;display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin-bottom:16px}
.dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--up);margin-right:6px;vertical-align:1px}
.dot.bad{background:var(--down)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.card .k{color:var(--muted);font-size:12px;margin-bottom:6px}
.card .v{font-size:24px;font-weight:650;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.card .d{color:var(--muted);font-size:11px;margin-top:4px}
.up{color:var(--up)} .down{color:var(--down)}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:16px}
h2{font-size:14px;margin:0 0 2px;font-weight:650}
.hint{color:var(--muted);font-size:12px;margin:0 0 14px}
.actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.log{margin-top:12px;background:#0b1220;color:#cbd5e1;border-radius:10px;padding:12px 14px;
  font:12px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace;max-height:260px;overflow:auto;display:none;white-space:pre-wrap}
.log.show{display:block}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:13px}
th{text-align:right;color:var(--muted);font-weight:500;font-size:12px;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
td{padding:7px 10px;text-align:right;border-bottom:1px solid #f1f5f9;white-space:nowrap}
tbody tr:hover{background:#fafbfc}
.tag{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;line-height:18px}
.tag.exact{background:#ecfdf5;color:#047857}
.tag.est{background:#fff7ed;color:#c2410c}
.est-num{color:var(--warn)}
svg{display:block;width:100%;height:auto;overflow:visible}
.tick{fill:var(--muted);font-size:11px}
.grid{stroke:#eef2f6;stroke-width:1}
.axis{stroke:#cbd5e1;stroke-width:1}
.empty{padding:44px 20px;text-align:center;color:var(--muted)}
.empty input{padding:8px 12px;border:1px solid var(--line);border-radius:8px;width:180px;font:inherit}
.note{color:var(--warn);font-size:12px;margin-top:10px}
.steps{margin:4px 0 0;padding-left:20px;color:var(--muted);font-size:12.5px}
.steps li{margin:5px 0}
.crawls{margin-top:14px;font-size:12.5px;color:var(--muted)}
.crawl-line{display:flex;align-items:center;gap:6px;margin:5px 0}
.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.toolbar input{padding:7px 11px;border:1px solid var(--line);border-radius:8px;font:inherit;width:220px}
.toolbar input:focus{outline:2px solid #bfdbfe;border-color:#93c5fd}
.pager{color:var(--muted);font-size:12.5px;display:flex;align-items:center;gap:8px}
.pager button{padding:4px 10px;font-size:12.5px}
.pill{font-size:12px;font-weight:400;color:var(--muted);margin-left:8px}
.linkbtn{border:0;background:none;color:var(--accent);padding:0 4px;font-size:12.5px;
  text-decoration:underline;cursor:pointer}
.linkbtn:hover{color:#1d4ed8}
.modal{position:fixed;inset:0;background:rgba(15,23,42,.45);display:none;
  align-items:center;justify-content:center;z-index:50;backdrop-filter:blur(2px)}
.modal.show{display:flex}
.modal-card{background:#fff;border-radius:16px;padding:22px 24px 20px;width:min(400px,92vw);
  text-align:center;box-shadow:0 24px 60px rgba(15,23,42,.28)}
.modal-card h3{margin:0 0 2px;font-size:16px}
.modal-card .sub2{color:var(--muted);font-size:12px;margin:0 0 12px}
.qrbox{width:236px;height:236px;margin:0 auto;border:1px solid var(--line);border-radius:10px;
  padding:8px;background:#fff;position:relative}
.qrbox.expired svg{opacity:.12}
.qrbox .overlay{position:absolute;inset:0;display:none;align-items:center;justify-content:center;
  color:var(--muted);font-size:13px;letter-spacing:.05em}
.qrbox.expired .overlay{display:flex}
.qrbox svg{width:100%;height:100%;display:block}
.qr-status{color:var(--muted);font-size:12.5px;min-height:20px;margin:10px 0 14px}
.qr-actions{display:flex;gap:8px;justify-content:center;align-items:center;flex-wrap:wrap}
tr.gone-row td{color:#94a3b8}
.skeleton{height:260px;border-radius:10px;background:linear-gradient(90deg,#f1f5f9,#f8fafc,#f1f5f9);
  background-size:200% 100%;animation:sk 1.2s infinite}
@keyframes sk{0%{background-position:0 0}100%{background-position:-200% 0}}
</style></head>
<body><div class="wrap">
<header>
  <h1>粉丝变化看板</h1>
  <div class="grow"></div>
  <select id="account" title="切换账号"></select>
  <div class="seg" id="range">
    <button data-days="7">7 天</button>
    <button data-days="30" class="on">30 天</button>
  </div>
  <button id="add" title="添加要监控的 B 站账号">+ 账号</button>
  <button id="refresh" title="重新读取本地数据">刷新</button>
</header>
<div class="sub" id="status"></div>

<div id="app"><div class="skeleton"></div></div>
</div>

<div class="modal" id="qrModal">
  <div class="modal-card">
    <h3>扫码登录 B 站</h3>
    <p class="sub2" id="qrHint">手机 B 站 App 扫一扫 · 3 分钟有效，过期自动换新</p>
    <div class="qrbox" id="qrBox"></div>
    <p class="qr-status" id="qrStatus">正在获取二维码…</p>
    <div class="qr-actions">
      <button id="qrRefresh">刷新二维码</button>
      <button id="qrClose" class="primary">关闭</button>
    </div>
    <p style="margin:12px 0 0"><button class="linkbtn" id="qrFallback">扫不动？改用浏览器窗口登录</button></p>
  </div>
</div>

<script>
const $ = (s) => document.querySelector(s);
const state = {uid: null, days: 30, loading: false, job: null, cookieOk: false,
               fan: {q: "", status: "all", page: 1}};

const nf = (n) => (n === null || n === undefined) ? "—" : n.toLocaleString("zh-CN");
const signed = (n) => (n === null || n === undefined) ? "—" : (n > 0 ? "+" : "") + n.toLocaleString("zh-CN");
const pct = (v) => (v === null || v === undefined) ? "—" : (v * 100).toFixed(2) + "%";

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function renderStatus(d) {
  const box = $("#status");
  box.textContent = "";
  const cookie = el("span");
  cookie.appendChild(el("i", "dot" + (d.cookie_ok ? "" : " bad")));
  cookie.appendChild(document.createTextNode(d.cookie_ok ? "Cookie 已配置" : "未配置 Cookie"));
  if (!d.cookie_ok) {
    cookie.appendChild(document.createTextNode(" · "));
    const jump = el("button", "linkbtn", "立即登录");
    jump.onclick = () => {
      const target = document.getElementById("loginBtn");
      if (target) { target.scrollIntoView({behavior: "smooth", block: "center"}); target.click(); }
    };
    cookie.appendChild(jump);
  }
  box.appendChild(cookie);
  if (d.meta) {
    box.appendChild(el("span", "", "最后采集 " + (d.meta.last_poll || "—")));
    box.appendChild(el("span", "", "采样点 " + nf(d.meta.snapshots)));
    box.appendChild(el("span", "", "粉丝名单 " + (d.meta.last_crawl || "未抓取")));
  }
  box.appendChild(el("span", "", "时区 " + d.tz));
}

function renderEmpty(d) {
  const box = $("#app");
  box.textContent = "";
  const sec = el("section");
  const wrap = el("div", "empty");
  wrap.appendChild(el("div", "", "还没有添加账号"));
  const p = el("div", "", "输入一个 B 站 UID，开始记录它的粉丝变化");
  p.style.margin = "8px 0 16px";
  wrap.appendChild(p);
  const input = el("input");
  input.placeholder = "例如 123456789";
  input.id = "uidInput";
  const btn = el("button", "primary", "添加");
  btn.style.marginLeft = "8px";
  btn.onclick = async () => {
    const uid = (input.value || "").trim();
    if (!/^\d+$/.test(uid)) { alert("UID 只能是数字"); return; }
    btn.disabled = true; btn.textContent = "添加中…";
    try {
      await api("/api/account", {method: "POST", headers: {"Content-Type": "application/json"},
                                 body: JSON.stringify({uid: Number(uid)})});
      await load();
    } catch (e) { alert("添加失败：" + e.message); btn.disabled = false; btn.textContent = "添加"; }
  };
  wrap.appendChild(input); wrap.appendChild(btn);
  sec.appendChild(wrap);
  box.appendChild(sec);
}

function renderKpi(d) {
  const k = d.kpi;
  const unk = d.warmup;   // 只有一天采样时，「新增/流失」还没有对比基准，显示 0 会误导
  const dash = "—";
  const cards = el("div", "cards");
  const data = [
    ["当前粉丝", nf(k.last_fans), "", "采集口径"],
    ["新增粉丝", unk ? dash : nf(k.new_total), unk ? "" : "up", unk ? "需要至少 2 天数据" : "日均 " + k.avg_new],
    ["流失粉丝", unk ? dash : nf(k.lost_total), unk ? "" : "down", unk ? "需要至少 2 天数据" : "日均 " + k.avg_lost],
    ["净增", unk ? dash : signed(k.net_total), unk ? "" : (k.net_total >= 0 ? "up" : "down"),
     (k.exact_days + k.est_days) + " 天累计"],
    ["日均掉粉率", unk ? dash : pct(k.churn_rate), "", "掉粉 / 粉丝规模"],
  ];
  for (const [name, value, cls, desc] of data) {
    const card = el("div", "card");
    card.appendChild(el("div", "k", name));
    card.appendChild(el("div", "v " + cls, value));
    card.appendChild(el("div", "d", desc));
    cards.appendChild(card);
  }
  return cards;
}

function renderTable(d) {
  const tb = el("tbody");
  for (const r of d.rows) {
    const tr = el("tr");
    tr.appendChild(el("td", "", r.day));
    tr.appendChild(el("td", "", nf(r.fans)));
    const net = el("td", r.net > 0 ? "up" : (r.net < 0 ? "down" : ""), signed(r.net));
    if (r.net === null) net.title = "还没有前一天的收盘价，等明天就能算出净增";
    tr.appendChild(net);
    const mark = (v) => (r.exact || v === null || v === undefined) ? "" : "≈";
    const newTd = el("td", r.exact ? "" : "est-num", mark(r.new) + nf(r.new));
    const lostTd = el("td", r.exact ? "" : "est-num", mark(r.lost) + nf(r.lost));
    if (r.new === null) newTd.title = "这段时间还没有可比较的采样点";
    if (r.lost === null) lostTd.title = "这段时间还没有可比较的采样点";
    tr.appendChild(newTd);
    tr.appendChild(lostTd);
    tr.appendChild(el("td", "", r.samples || "—"));
    const src = el("td");
    src.appendChild(el("span", "tag " + (r.exact ? "exact" : "est"), r.exact ? "创作中心" : "采样估算"));
    tr.appendChild(src);
    tb.appendChild(tr);
  }
  const table = el("table");
  const head = el("thead");
  const hr = el("tr");
  ["日期", "总粉丝", "净增", "新增", "掉粉", "采样点", "数据源"].forEach((t) => hr.appendChild(el("th", "", t)));
  head.appendChild(hr);
  table.appendChild(head); table.appendChild(tb);
  return table;
}

function renderWarmup(d) {
  const sec = el("section");
  sec.appendChild(el("h2", "", "正在积累数据"));
  const p = el("p", "hint");
  p.textContent = `已经记下当前粉丝 ${nf(d.kpi.last_fans)}，但只有 ${d.kpi.days} 天采样，还画不出趋势。`;
  sec.appendChild(p);

  const ul = el("ul", "steps");
  const items = [
    "让定时任务继续跑（每 5 分钟一次），明天这时候就有完整的每日净增了。",
    "想立刻看到精确的「新增 / 掉粉」：先点「登录 B 站（扫码）」，再点「回填近 30 天官方数据」，一次拿回 30 天。",
    "想看「谁取关了我」：点「抓粉丝名单」，明天再抓一次就能比对出名单。",
  ];
  for (const text of items) ul.appendChild(el("li", "", text));
  sec.appendChild(ul);
  return sec;
}

function renderChurn(c) {
  const sec = el("section");
  sec.appendChild(el("h2", "", "谁取关了我"));
  sec.appendChild(el("p", "hint", "靠两次粉丝名单快照比对得出；B 站不提供「谁取关了我」的接口。"));

  if (!c.items.length) {
    sec.appendChild(el("div", "empty", c.ready ? "这段时间没有检测到取关" : "还没有可用的取关数据"));
  } else {
    const table = el("table");
    const head = el("thead"), hr = el("tr");
    ["昵称", "UID", "关注时间", "存续", "取关"].forEach((t) => hr.appendChild(el("th", "", t)));
    head.appendChild(hr); table.appendChild(head);
    const tb = el("tbody");
    for (const it of c.items) {
      const tr = el("tr");
      tr.appendChild(el("td", "", it.uname));
      tr.appendChild(el("td", "", it.uid));
      tr.appendChild(el("td", "", it.followed));
      tr.appendChild(el("td", "", it.survival === null ? "—" : it.survival + " 天"));
      tr.appendChild(el("td", "", it.gone));
      tb.appendChild(tr);
    }
    table.appendChild(tb);
    sec.appendChild(table);
    const line = el("p", "hint");
    line.style.margin = "10px 0 0";
    line.textContent = `共 ${c.count} 人，其中 ${c.quick_count} 人关注不到一周就走了` +
      (c.avg_survival !== null ? `，平均存续 ${c.avg_survival.toFixed(1)} 天` : "");
    sec.appendChild(line);
  }
  if (c.note) { const n = el("p", "note", c.note); sec.appendChild(n); }
  return sec;
}

function renderActions(d) {
  const sec = el("section");
  sec.appendChild(el("h2", "", "采集"));
  sec.appendChild(el("p", "hint", "所有操作都在本机执行；抓粉丝名单需要 Cookie，可能需要十几秒。"));
  const box = el("div", "actions");
  const mk = (label, action, primary) => {
    const b = el("button", primary ? "primary" : "", label);
    b.onclick = () => runJob(action);
    b.dataset.action = action;
    return b;
  };
  const needLogin = !d.cookie_ok;
  const login = el("button", needLogin ? "primary" : "", needLogin ? "登录 B 站（扫码）" : "重新登录");
  login.id = "loginBtn";
  login.onclick = openLogin;
  box.appendChild(login);
  box.appendChild(mk("采一次快照", "poll", !needLogin));

  const backfill = mk("回填近 30 天官方数据", "backfill");
  const fans = mk("抓粉丝名单", "fans");
  if (needLogin) {
    backfill.disabled = true;
    fans.disabled = true;
    backfill.title = fans.title = "先点左边的「登录 B 站」";
  }
  box.appendChild(backfill);
  box.appendChild(fans);
  sec.appendChild(box);
  if (needLogin) {
    sec.appendChild(el("p", "note",
      "还没有登录：点「登录 B 站（扫码）」，二维码会直接出现在弹窗里，手机扫一下就完成。"));
  }

  if (d.meta && d.meta.crawls && d.meta.crawls.length) {
    const history = el("div", "crawls");
    history.appendChild(el("div", "", "最近抓取记录（拿来核对是否抓全）"));
    for (const c of d.meta.crawls) {
      const line = el("div", "crawl-line");
      line.appendChild(el("i", "dot" + (c.complete ? "" : " bad")));
      const totalTxt = c.total ? ` / 共 ${nf(c.total)}` : "";
      line.appendChild(document.createTextNode(`${c.at} · 抓到 ${nf(c.fetched)} 人${totalTxt} · `));
      line.appendChild(el("span", c.complete ? "up" : "est-num", c.complete ? "完整" : (c.note || "不完整")));
      history.appendChild(line);
    }
    sec.appendChild(history);
  }
  const log = el("div", "log");
  log.id = "log";
  sec.appendChild(log);
  return sec;
}

function render(d) {
  $("#status") && renderStatus(d);
  if (d.empty === "no_account") { renderEmpty(d); return; }

  const sel = $("#account");
  const keep = sel.value;
  sel.textContent = "";
  for (const a of d.accounts) {
    const opt = el("option", "", a.label);
    opt.value = a.uid;
    sel.appendChild(opt);
  }
  sel.value = keep && [...sel.options].some((o) => o.value === keep) ? keep : String(d.uid);
  state.uid = Number(sel.value);

  const app = $("#app");
  app.textContent = "";

  if (!d.has_data) {
    const sec = el("section");
    sec.appendChild(el("h2", "", d.label));
    sec.appendChild(el("p", "hint", "这个号还没有任何数据。先点下面的「采一次快照」或「回填近 30 天官方数据」。"));
    app.appendChild(sec);
    app.appendChild(renderActions(d));
    return;
  }

  if (d.meta && d.meta.stale) {
    const warn = el("section");
    warn.style.borderColor = "#fbbf24";
    warn.style.background = "#fffbeb";
    warn.appendChild(el("h2", "", "⚠ 这个看板在跑旧代码"));
    warn.appendChild(el("p", "hint",
      "源码更新过，但服务是很早启动的。重启服务（关掉再重新打开 App）后新功能才会生效。"));
    app.appendChild(warn);
  }

  app.appendChild(renderKpi(d));

  if (d.warmup) {
    app.appendChild(renderWarmup(d));
  } else {
    const bars = el("section");
    bars.appendChild(el("h2", "", "每日新增 / 流失"));
    bars.appendChild(el("p", "hint", "绿色为新增，红色为流失；颜色越浅表示该天是采样估算。"));
    const bw = el("div"); bw.innerHTML = d.charts.bars; bars.appendChild(bw);
    app.appendChild(bars);

    const line = el("section");
    line.appendChild(el("h2", "", "粉丝总数趋势"));
    line.appendChild(el("p", "hint", "按每天最后一个采样点绘制。"));
    const lw = el("div"); lw.innerHTML = d.charts.line; line.appendChild(lw);
    app.appendChild(line);
  }

  app.appendChild(renderActions(d));
  app.appendChild(renderChurn(d.churn));

  const tab = el("section");
  tab.appendChild(el("h2", "", "逐日明细"));
  tab.appendChild(el("p", "hint", `净增 = 新增 − 流失；≈ 为采样推算的下限。${d.range.start} ~ ${d.range.end}`));
  tab.appendChild(renderTable(d));
  app.appendChild(tab);

  state.cookieOk = d.cookie_ok;
  app.appendChild(renderFansShell());
  loadFans();
}

function renderFansShell() {
  const sec = el("section");
  const h = el("h2", "", "粉丝名单");
  const pill = el("span", "pill", "");
  pill.id = "fanCount";
  h.appendChild(pill);
  sec.appendChild(h);
  sec.appendChild(el("p", "hint",
    "核对用：这就是本地存下来的完整名单。数量和 B 站粉丝数对得上，说明抓取成功；搜个昵称、翻两页也能确认。"));
  if (!state.cookieOk) {
    sec.appendChild(el("p", "note", "未配置 Cookie，抓不了名单。点上面的「登录 B 站（扫码）」，登录完刷新即可。"));
  }

  const bar = el("div", "toolbar");
  const input = el("input");
  input.id = "fanQ";
  input.placeholder = "搜索昵称或 UID…";
  input.value = state.fan.q;
  bar.appendChild(input);

  const seg = el("div", "seg");
  seg.id = "fanStatus";
  for (const [key, label] of [["all", "全部"], ["active", "在关注"], ["gone", "已取关"]]) {
    const b = el("button", state.fan.status === key ? "on" : "", label);
    b.dataset.status = key;
    b.onclick = () => {
      state.fan.status = key;
      state.fan.page = 1;
      seg.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      loadFans();
    };
    seg.appendChild(b);
  }
  bar.appendChild(seg);
  bar.appendChild(el("span", "grow"));
  const pager = el("span", "pager");
  pager.id = "fanPager";
  bar.appendChild(pager);
  sec.appendChild(bar);

  const holder = el("div");
  holder.id = "fanTable";
  sec.appendChild(holder);

  input.addEventListener("input", () => {
    clearTimeout(input._timer);
    input._timer = setTimeout(() => {
      state.fan.q = input.value.trim();
      state.fan.page = 1;
      loadFans();
    }, 300);
  });
  return sec;
}

async function loadFans() {
  const holder = $("#fanTable");
  if (!holder) return;
  const q = new URLSearchParams({status: state.fan.status, page: state.fan.page, size: 50});
  if (state.uid) q.set("uid", state.uid);
  if (state.fan.q) q.set("q", state.fan.q);

  let d;
  try { d = await api("/api/fans?" + q.toString()); }
  catch (e) { holder.textContent = "读取名单失败：" + e.message; return; }

  const t = d.totals || {all: 0, active: 0, gone: 0};
  $("#fanCount").textContent = t.all ? `共 ${nf(t.all)} 人 · 在关注 ${nf(t.active)} · 已取关 ${nf(t.gone)}` : "";

  holder.textContent = "";
  if (!d.items.length) {
    holder.appendChild(el("div", "empty", d.total ? "没有匹配的记录" :
      (t.all ? "这个筛选下没有记录" : "还没有粉丝名单，点上面的「抓粉丝名单」")));
    $("#fanPager").textContent = "";
    return;
  }

  const table = el("table");
  const head = el("thead"), hr = el("tr");
  ["昵称", "UID", "关注时间", "状态", "取关 / 最近记录"].forEach((x) => hr.appendChild(el("th", "", x)));
  head.appendChild(hr); table.appendChild(head);

  const tb = el("tbody");
  for (const it of d.items) {
    const tr = el("tr", it.active ? "" : "gone-row");
    tr.appendChild(el("td", "", it.uname));
    tr.appendChild(el("td", "", it.uid));
    tr.appendChild(el("td", "", it.followed));
    const st = el("td");
    st.appendChild(el("span", "tag " + (it.active ? "exact" : "none"),
                      it.active ? (it.mutual ? "互粉" : "在关注") : "已取关"));
    tr.appendChild(st);
    tr.appendChild(el("td", "", it.active ? it.last_seen : it.gone));
    tb.appendChild(tr);
  }
  table.appendChild(tb);
  holder.appendChild(table);

  const pages = Math.max(1, Math.ceil(d.total / d.size));
  const pager = $("#fanPager");
  pager.textContent = "";
  const prev = el("button", "", "上一页");
  prev.disabled = d.page <= 1;
  prev.onclick = () => { state.fan.page = Math.max(1, d.page - 1); loadFans(); };
  const next = el("button", "", "下一页");
  next.disabled = d.page >= pages;
  next.onclick = () => { state.fan.page = Math.min(pages, d.page + 1); loadFans(); };
  pager.appendChild(prev);
  pager.appendChild(el("span", "", `第 ${d.page} / ${pages} 页 · 共 ${nf(d.total)} 条`));
  pager.appendChild(next);
}

let qrTimer = null;
let qrTick = null;
let qrId = null;
let qrDeadline = 0;
let qrState = "";
let qrRetries = 0;
let qrPolling = false;       // 防止两个请求同时在飞（B 站的码只认一次）
let qrSawScanned = false;    // 已经扫过码：此时失效多半是「确认了但没接住」

function openLogin() {
  qrRetries = 0;
  qrSawScanned = false;
  $("#qrModal").classList.add("show");
  refreshQr();
}

function closeLogin() {
  clearInterval(qrTimer);
  clearInterval(qrTick);
  qrTimer = qrTick = null;
  qrId = null;
  qrDeadline = 0;
  qrState = "";
  qrPolling = false;
  qrSawScanned = false;
  $("#qrModal").classList.remove("show");
}

function setQrStatus(text, state) {
  if (state) qrState = state;
  $("#qrStatus").textContent = text;
}

function startCountdown() {
  clearInterval(qrTick);
  qrTick = setInterval(() => {
    if (qrState !== "waiting") return;
    const left = Math.max(0, Math.round((qrDeadline - Date.now()) / 1000));
    if (left > 0) setQrStatus(`等待扫码…（${left} 秒后失效）`, "waiting");
  }, 1000);
}

async function refreshQr() {
  clearInterval(qrTimer);
  clearInterval(qrTick);
  qrId = null;
  qrPolling = false;
  qrState = "loading";
  $("#qrBox").classList.remove("expired");
  $("#qrBox").textContent = "";
  $("#qrStatus").textContent = "正在获取二维码…";
  try {
    const d = await api("/api/login/qr", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: "{}",
    });
    qrId = d.id;
    qrDeadline = Date.now() + (d.ttl || 180) * 1000;
    $("#qrHint").textContent = d.cookie_ok
      ? "当前已经是登录状态；扫码会切换成手机上的那个号"
      : "手机 B 站 App 扫一扫 · 3 分钟有效，过期自动换新";
    $("#qrBox").innerHTML = d.svg + '<div class="overlay">已失效</div>';
    setQrStatus("等待扫码…", "waiting");
    startCountdown();
    qrTimer = setInterval(pollQr, 2000);
  } catch (e) {
    setQrStatus("获取二维码失败：" + e.message, "error");
  }
}

async function pollQr() {
  if (!qrId || qrPolling || qrState === "done") return;
  qrPolling = true;
  let d;
  try {
    d = await api("/api/login/qr?id=" + encodeURIComponent(qrId));
  } catch (e) {
    qrPolling = false;
    clearInterval(qrTimer);
    clearInterval(qrTick);
    $("#qrBox").classList.add("expired");
    if (qrRetries++ < 3) { setQrStatus("会话断了，正在换一张二维码…", "expired"); setTimeout(refreshQr, 600); }
    else setQrStatus("连不上登录接口，点「刷新二维码」重试", "error");
    return;
  }

  qrPolling = false;

  if (d.status === "scanned") {
    qrSawScanned = true;
    setQrStatus("已扫码，请在手机上点「确认登录」", "scanned");
    return;
  }
  if (d.status === "confirmed") {
    clearInterval(qrTimer);
    clearInterval(qrTick);
    setQrStatus("✓ 登录成功，正在刷新页面…", "done");
    qrState = "done";
    setTimeout(async () => { closeLogin(); await load(); }, 700);
    return;
  }
  if (d.status === "expired") {
    clearInterval(qrTimer);
    clearInterval(qrTick);
    $("#qrBox").classList.add("expired");
    const age = Math.round(d.age || 0);

    // 已经登录成功了：这时候再纠结这张码过没过期没有意义，直接收工
    if (d.cookie_ok) {
      setQrStatus("✓ 已登录，正在刷新…", "done");
      qrState = "done";
      setTimeout(async () => { closeLogin(); await load(); }, 500);
      return;
    }

    // 扫过码之后失效：八成是手机上已经确认、但那一拍没接住。
    // 这时别急着换码（换了你就得再扫一次、又登录一次），先再核对一次。
    if (qrSawScanned && qrRetries++ < 2) {
      setQrStatus("已确认，正在核对登录状态…", "expired");
      setTimeout(() => { qrPolling = false; pollQr(); }, 400);
      return;
    }
    if (!qrSawScanned && qrRetries++ < 3) {
      setQrStatus(`二维码失效了（存在了 ${age} 秒），正在自动换一张…`, "expired");
      setTimeout(refreshQr, 800);
      return;
    }
    setQrStatus("没拿到登录态。点「刷新二维码」重试，或改用浏览器窗口登录", "error");
  }
}

$("#qrClose").onclick = closeLogin;
$("#qrRefresh").onclick = () => { qrRetries = 0; refreshQr(); };
$("#qrFallback").onclick = () => {
  closeLogin();
  const target = document.getElementById("loginBtn");
  if (target) { target.scrollIntoView({behavior: "smooth", block: "center"}); }
  runJob("login");
};
$("#qrModal").onclick = (e) => { if (e.target.id === "qrModal") closeLogin(); };

async function load() {
  if (state.loading) return;
  state.loading = true;
  $("#refresh").disabled = true;
  try {
    const q = new URLSearchParams({days: state.days});
    if (state.uid) q.set("uid", state.uid);
    const d = await api("/api/overview?" + q.toString());
    render(d);
  } catch (e) {
    $("#app").textContent = "";
    const sec = el("section");
    sec.appendChild(el("div", "empty", "读取失败：" + e.message));
    $("#app").appendChild(sec);
  } finally {
    state.loading = false;
    $("#refresh").disabled = false;
  }
}

async function runJob(action) {
  const buttons = document.querySelectorAll(".actions button");
  buttons.forEach((b) => b.disabled = true);
  const log = $("#log");
  log.classList.add("show");
  log.textContent = "启动中…";
  try {
    const r = await api("/api/run", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action, uid: state.uid}),
    });
    await follow(r.job);
  } catch (e) {
    log.textContent = "启动失败：" + e.message;
  } finally {
    buttons.forEach((b) => b.disabled = false);
  }
}

function follow(id) {
  return new Promise((resolve) => {
    const timer = setInterval(async () => {
      try {
        const j = await api("/api/job?id=" + encodeURIComponent(id));
        $("#log").textContent = j.lines.join("\n") || "执行中…";
        $("#log").scrollTop = $("#log").scrollHeight;
        if (j.status !== "running") {
          clearInterval(timer);
          $("#log").textContent += "\n\n" + (j.status === "done" ? "✓ 完成，正在刷新数据…" : "✗ 失败");
          await load();
          resolve();
        }
      } catch (e) { /* 网络抖动就继续等 */ }
    }, 600);
  });
}

$("#refresh").onclick = load;
$("#add").onclick = async () => {
  const raw = (prompt("输入要添加的 B 站 UID（纯数字）") || "").trim();
  if (!raw) return;
  if (!/^\d+$/.test(raw)) { alert("UID 只能是数字"); return; }
  try {
    const r = await api("/api/account", {method: "POST", headers: {"Content-Type": "application/json"},
                                         body: JSON.stringify({uid: Number(raw)})});
    state.uid = r.uid;
    await load();
  } catch (e) { alert("添加失败：" + e.message); }
};
$("#account").onchange = (e) => {
  state.uid = Number(e.target.value);
  state.fan = {q: "", status: "all", page: 1};
  load();
};
$("#range").onclick = (e) => {
  const btn = e.target.closest("button[data-days]");
  if (!btn) return;
  document.querySelectorAll("#range button").forEach((b) => b.classList.toggle("on", b === btn));
  state.days = Number(btn.dataset.days);
  load();
};
load();
if (location.hash === "#login") setTimeout(openLogin, 400);   // 可以直接用 #login 深链
</script>
</body></html>
"""


# ---------------------------------------------------------------- 启动


def running_health(port: int, host: str = "127.0.0.1", timeout: float = 0.8) -> dict | None:
    """端口上如果是我们的看板，返回它的 /api/health。"""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data if isinstance(data, dict) and data.get("app") == "bilifans" else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def is_running(port: int, host: str = "127.0.0.1", timeout: float = 0.8) -> bool:
    """端口上是不是已经有我们的看板在跑（用来让快捷键可以反复按）。"""
    import urllib.error
    import urllib.request

    if running_health(port, host, timeout) is not None:
        return True

    # 老版本还没有 /api/health，退一步认页面标题
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=timeout) as resp:
            return "粉丝变化看板" in resp.read(2048).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return False


def make_server(cfg: Config, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT, cookie: str = ""):
    handler = type(
        "BoundAppHandler",
        (AppHandler,),
        {
            "cfg": cfg,
            "cookie_override": cookie,
            "tz": get_tz(cfg.timezone),
            "jobs": JobRunner(),
            "logins": LoginStore(),
        },
    )
    for candidate in range(port, port + 12):
        try:
            httpd = ThreadingHTTPServer((host, candidate), handler)
        except OSError:
            continue
        return httpd, int(httpd.server_address[1])  # port=0 时由系统分配
    raise OSError(f"端口 {port}~{port + 11} 都被占用了，换个 --port 试试")


def serve(
    cfg: Config,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    cookie: str = "",
    open_browser: bool = True,
) -> int:
    url = f"http://{host}:{port}/"
    health = running_health(port, host)
    if health is not None:
        print(f"看板已经在运行：{url}", flush=True)
        if health.get("stale"):
            print("  ⚠ 正在跑的是旧代码（启动后源码有更新），要重启才能用上新功能：", flush=True)
            print("    先在活动监视器里结束那个 python 进程，再重新执行本命令", flush=True)
        else:
            print("  （不重复启动，直接打开页面）", flush=True)
        if open_browser:
            webbrowser.open(url)
        return 0

    httpd, actual = make_server(cfg, host=host, port=port, cookie=cookie)
    url = f"http://{host}:{actual}/"
    print(f"网页看板已启动：{url}", flush=True)
    print(f"  数据库 {cfg.db_path()}", flush=True)
    print(f"  Cookie {'已配置' if cookie else '未配置（只能看估算值）'}", flush=True)
    print("  按 Ctrl+C 停止\n", flush=True)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0
