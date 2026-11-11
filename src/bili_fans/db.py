"""SQLite 存储层。三张表：账号、粉丝数快照、创作中心每日明细。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .api import CreatorDay
from .timeutil import day_key, now_ts

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    uid        INTEGER PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    added_at   INTEGER NOT NULL
);

-- 高频采样的总粉丝数，每秒一条也不会冲突（主键 uid+ts）
CREATE TABLE IF NOT EXISTS snapshot (
    uid       INTEGER NOT NULL,
    ts        INTEGER NOT NULL,
    fans      INTEGER NOT NULL,
    following INTEGER,
    source    TEXT NOT NULL DEFAULT 'public',
    PRIMARY KEY (uid, ts)
);
CREATE INDEX IF NOT EXISTS idx_snapshot_uid_ts ON snapshot (uid, ts);

-- 创作中心口径的每日涨粉/掉粉，按 uid+日期唯一，可反复覆盖写入
CREATE TABLE IF NOT EXISTS creator_day (
    uid         INTEGER NOT NULL,
    day         TEXT NOT NULL,
    new_fans    INTEGER NOT NULL,
    lost_fans   INTEGER NOT NULL,
    total_fans  INTEGER,
    fetched_at  INTEGER NOT NULL,
    PRIMARY KEY (uid, day)
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

-- 粉丝名单快照。B 站没有「谁取关了我」这种接口，只能靠每次抓名单做差集
CREATE TABLE IF NOT EXISTS fan (
    owner_uid     INTEGER NOT NULL,
    fan_uid       INTEGER NOT NULL,
    uname         TEXT NOT NULL DEFAULT '',
    mtime         INTEGER,
    face          TEXT NOT NULL DEFAULT '',
    sign          TEXT NOT NULL DEFAULT '',
    attribute     INTEGER NOT NULL DEFAULT 0,
    first_seen_ts INTEGER NOT NULL,
    last_seen_ts  INTEGER NOT NULL,
    gone_ts       INTEGER,               -- 非空表示「已确认取关」
    PRIMARY KEY (owner_uid, fan_uid)
);
CREATE INDEX IF NOT EXISTS idx_fan_owner_gone ON fan (owner_uid, gone_ts);

-- 涨粉 / 取关流水，给报告按天统计用
CREATE TABLE IF NOT EXISTS fan_event (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_uid INTEGER NOT NULL,
    fan_uid   INTEGER NOT NULL,
    kind      TEXT NOT NULL,             -- follow / unfollow
    ts        INTEGER NOT NULL,
    uname     TEXT NOT NULL DEFAULT '',
    crawl_id  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fan_event_owner_ts ON fan_event (owner_uid, ts);

-- 每次抓到的完整名单（原样留档，逐次可回看/可导出）
CREATE TABLE IF NOT EXISTS fan_snapshot (
    crawl_id  INTEGER NOT NULL,
    owner_uid INTEGER NOT NULL,
    fan_uid   INTEGER NOT NULL,
    uname     TEXT NOT NULL DEFAULT '',
    mtime     INTEGER,
    PRIMARY KEY (crawl_id, fan_uid)
);
CREATE INDEX IF NOT EXISTS idx_fan_snapshot_owner ON fan_snapshot (owner_uid, fan_uid);

-- 每次抓取留痕：抓了多少、是否完整，出问题时用来排查
CREATE TABLE IF NOT EXISTS fan_crawl (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_uid   INTEGER NOT NULL,
    started_ts  INTEGER NOT NULL,
    finished_ts INTEGER,
    fetched     INTEGER NOT NULL DEFAULT 0,
    total       INTEGER,
    complete    INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT ''
);
"""


@dataclass(frozen=True)
class Account:
    uid: int
    name: str
    note: str = ""
    added_at: int = 0

    @property
    def label(self) -> str:
        return f"{self.name}（UID {self.uid}）" if self.name and self.name != str(self.uid) else f"UID {self.uid}"


@dataclass(frozen=True)
class Snapshot:
    ts: int
    fans: int
    following: int | None = None


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    if db_path.parent and not db_path.parent.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------- 账号 ----------


def upsert_account(conn: sqlite3.Connection, uid: int, name: str = "", note: str = "") -> Account:
    conn.execute(
        """INSERT INTO account (uid, name, note, added_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(uid) DO UPDATE SET
             name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE account.name END,
             note = CASE WHEN excluded.note <> '' THEN excluded.note ELSE account.note END""",
        (uid, name or "", note or "", now_ts()),
    )
    conn.commit()
    return get_account(conn, uid)  # type: ignore[return-value]


def get_account(conn: sqlite3.Connection, uid: int) -> Account | None:
    row = conn.execute("SELECT uid, name, note, added_at FROM account WHERE uid = ?", (uid,)).fetchone()
    return Account(**dict(row)) if row else None


def list_accounts(conn: sqlite3.Connection) -> list[Account]:
    rows = conn.execute("SELECT uid, name, note, added_at FROM account ORDER BY added_at").fetchall()
    return [Account(**dict(r)) for r in rows]


def delete_account(conn: sqlite3.Connection, uid: int) -> int:
    """连同历史数据一起删除，返回删掉的行数。"""
    n = 0
    for table in ("snapshot", "creator_day", "account"):
        cur = conn.execute(f"DELETE FROM {table} WHERE uid = ?", (uid,))
        n += cur.rowcount
    conn.commit()
    return n


# ---------- 快照 ----------


def add_snapshot(
    conn: sqlite3.Connection, uid: int, ts: int, fans: int, following: int | None = None, source: str = "public"
) -> bool:
    """返回 True 表示新写入，False 表示这一秒已有记录（重复采样被忽略）。"""
    cur = conn.execute(
        """INSERT INTO snapshot (uid, ts, fans, following, source) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(uid, ts) DO UPDATE SET fans = excluded.fans, following = excluded.following""",
        (uid, int(ts), int(fans), following, source),
    )
    conn.commit()
    return cur.rowcount > 0


def snapshots_between(conn: sqlite3.Connection, uid: int, start_ts: int, end_ts: int) -> list[Snapshot]:
    rows = conn.execute(
        "SELECT ts, fans, following FROM snapshot WHERE uid = ? AND ts >= ? AND ts <= ? ORDER BY ts",
        (uid, int(start_ts), int(end_ts)),
    ).fetchall()
    return [Snapshot(ts=r["ts"], fans=r["fans"], following=r["following"]) for r in rows]


def last_snapshot(conn: sqlite3.Connection, uid: int) -> Snapshot | None:
    row = conn.execute(
        "SELECT ts, fans, following FROM snapshot WHERE uid = ? ORDER BY ts DESC LIMIT 1", (uid,)
    ).fetchone()
    return Snapshot(ts=row["ts"], fans=row["fans"], following=row["following"]) if row else None


def snapshot_count(conn: sqlite3.Connection, uid: int) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM snapshot WHERE uid = ?", (uid,)).fetchone()
    return int(row["n"])


def add_snapshots_bulk(conn: sqlite3.Connection, rows: Iterable[tuple[int, int, int, int | None, str]]) -> int:
    """批量写入 (uid, ts, fans, following, source)，用于灌历史/演示数据。"""
    payload = [(int(u), int(t), int(f), fw, s) for u, t, f, fw, s in rows]
    if not payload:
        return 0
    conn.executemany(
        """INSERT INTO snapshot (uid, ts, fans, following, source) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(uid, ts) DO UPDATE SET fans = excluded.fans, following = excluded.following""",
        payload,
    )
    conn.commit()
    return len(payload)


# ---------- 创作中心每日明细 ----------


def upsert_creator_days(conn: sqlite3.Connection, uid: int, days: Iterable[CreatorDay]) -> int:
    rows = [
        (uid, day_key(d.day), int(d.new_fans), int(d.lost_fans), d.total_fans, now_ts())
        for d in days
    ]
    if not rows:
        return 0
    conn.executemany(
        """INSERT INTO creator_day (uid, day, new_fans, lost_fans, total_fans, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(uid, day) DO UPDATE SET
             new_fans = excluded.new_fans,
             lost_fans = excluded.lost_fans,
             total_fans = COALESCE(excluded.total_fans, creator_day.total_fans),
             fetched_at = excluded.fetched_at""",
        rows,
    )
    conn.commit()
    return len(rows)


def creator_days_between(conn: sqlite3.Connection, uid: int, start_day: str, end_day: str) -> dict[str, tuple[int, int]]:
    rows = conn.execute(
        "SELECT day, new_fans, lost_fans FROM creator_day WHERE uid = ? AND day >= ? AND day <= ?",
        (uid, start_day, end_day),
    ).fetchall()
    return {r["day"]: (int(r["new_fans"]), int(r["lost_fans"])) for r in rows}


def latest_creator_day(conn: sqlite3.Connection, uid: int) -> str | None:
    row = conn.execute("SELECT MAX(day) AS d FROM creator_day WHERE uid = ?", (uid,)).fetchone()
    return row["d"] if row and row["d"] else None


def clear_account_data(conn: sqlite3.Connection, uid: int) -> int:
    """只清历史数据，保留账号本身（演示数据重灌时用）。"""
    n = 0
    for table in ("snapshot", "creator_day"):
        n += conn.execute(f"DELETE FROM {table} WHERE uid = ?", (uid,)).rowcount
    for table in ("fan", "fan_event"):
        n += conn.execute(f"DELETE FROM {table} WHERE owner_uid = ?", (uid,)).rowcount
    n += conn.execute("DELETE FROM fan_snapshot WHERE owner_uid = ?", (uid,)).rowcount
    n += conn.execute("DELETE FROM fan_crawl WHERE owner_uid = ?", (uid,)).rowcount
    conn.commit()
    return n


# ---------- meta ----------


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, json.dumps(value, ensure_ascii=False)),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["v"])
    except (TypeError, ValueError):
        return default


# ---------- 粉丝名单 ----------


@dataclass(frozen=True)
class FanCrawl:
    id: int
    owner_uid: int
    started_ts: int
    finished_ts: int | None
    fetched: int
    total: int | None
    complete: bool
    note: str = ""


@dataclass(frozen=True)
class FanRow:
    fan_uid: int
    uname: str
    mtime: int | None
    first_seen_ts: int
    last_seen_ts: int
    gone_ts: int | None
    attribute: int = 0

    @property
    def active(self) -> bool:
        return self.gone_ts is None


@dataclass(frozen=True)
class ChurnRow:
    fan_uid: int
    uname: str
    mtime: int | None
    gone_ts: int


def start_fan_crawl(conn: sqlite3.Connection, owner_uid: int, ts: int) -> int:
    cur = conn.execute(
        "INSERT INTO fan_crawl (owner_uid, started_ts) VALUES (?, ?)", (owner_uid, int(ts))
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_fan_crawl(
    conn: sqlite3.Connection,
    crawl_id: int,
    *,
    fetched: int,
    total: int | None,
    complete: bool,
    note: str = "",
    ts: int | None = None,
) -> None:
    conn.execute(
        """UPDATE fan_crawl SET finished_ts = ?, fetched = ?, total = ?, complete = ?, note = ?
           WHERE id = ?""",
        (int(ts or now_ts()), int(fetched), total, 1 if complete else 0, note, crawl_id),
    )
    conn.commit()


def latest_fan_crawl(conn: sqlite3.Connection, owner_uid: int, *, finished_only: bool = True) -> FanCrawl | None:
    sql = "SELECT * FROM fan_crawl WHERE owner_uid = ?"
    if finished_only:
        sql += " AND finished_ts IS NOT NULL"
    row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", (owner_uid,)).fetchone()
    if not row:
        return None
    return FanCrawl(
        id=row["id"],
        owner_uid=row["owner_uid"],
        started_ts=row["started_ts"],
        finished_ts=row["finished_ts"],
        fetched=row["fetched"],
        total=row["total"],
        complete=bool(row["complete"]),
        note=row["note"],
    )


def upsert_fans(
    conn: sqlite3.Connection,
    owner_uid: int,
    fans: Iterable,
    ts: int,
    crawl_id: int | None = None,
    *,
    emit_events: bool = True,
) -> list:
    """写入本次抓到的粉丝，返回「新关注」的那批（含取关后又回来的）。

    首次抓取（emit_events=False）只建基线，不往流水里塞几百条假的「新关注」。
    """
    known: dict[int, int | None] = {
        r["fan_uid"]: r["gone_ts"]
        for r in conn.execute("SELECT fan_uid, gone_ts FROM fan WHERE owner_uid = ?", (owner_uid,))
    }
    fresh = []
    for fan in fans:
        if fan.mid not in known:
            fresh.append(fan)
        elif known[fan.mid] is not None:  # 之前取关过，现在又回来了
            fresh.append(fan)

    conn.executemany(
        """INSERT INTO fan
             (owner_uid, fan_uid, uname, mtime, face, sign, attribute, first_seen_ts, last_seen_ts, gone_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
           ON CONFLICT(owner_uid, fan_uid) DO UPDATE SET
             uname = excluded.uname,
             mtime = COALESCE(excluded.mtime, fan.mtime),
             face = excluded.face,
             sign = excluded.sign,
             attribute = excluded.attribute,
             last_seen_ts = excluded.last_seen_ts,
             gone_ts = NULL""",
        [
            (owner_uid, f.mid, f.uname, f.mtime or None, f.face, f.sign, f.attribute, int(ts), int(ts))
            for f in fans
        ],
    )
    if emit_events and fresh:
        conn.executemany(
            "INSERT INTO fan_event (owner_uid, fan_uid, kind, ts, uname, crawl_id) VALUES (?, ?, 'follow', ?, ?, ?)",
            [(owner_uid, f.mid, int(ts), f.uname, crawl_id) for f in fresh],
        )
    conn.commit()
    return fresh


def mark_missing_gone(
    conn: sqlite3.Connection, owner_uid: int, ts: int, crawl_id: int | None = None
) -> list:
    """本次没出现的粉丝 → 判定为取关。返回这批人（昵称还是最后一次看到的名字）。"""
    rows = conn.execute(
        "SELECT fan_uid, uname, mtime FROM fan WHERE owner_uid = ? AND gone_ts IS NULL AND last_seen_ts < ?",
        (owner_uid, int(ts)),
    ).fetchall()
    if not rows:
        return []
    payload = [(int(ts), owner_uid, r["fan_uid"]) for r in rows]
    conn.executemany("UPDATE fan SET gone_ts = ? WHERE owner_uid = ? AND fan_uid = ?", payload)
    conn.executemany(
        "INSERT INTO fan_event (owner_uid, fan_uid, kind, ts, uname, crawl_id) VALUES (?, ?, 'unfollow', ?, ?, ?)",
        [(owner_uid, r["fan_uid"], int(ts), r["uname"], crawl_id) for r in rows],
    )
    conn.commit()
    return [
        ChurnRow(fan_uid=r["fan_uid"], uname=r["uname"], mtime=r["mtime"], gone_ts=int(ts)) for r in rows
    ]


def fan_counts(conn: sqlite3.Connection, owner_uid: int) -> tuple[int, int]:
    """(还在关注的, 已确认取关的)"""
    row = conn.execute(
        """SELECT
             SUM(CASE WHEN gone_ts IS NULL THEN 1 ELSE 0 END) AS alive,
             SUM(CASE WHEN gone_ts IS NOT NULL THEN 1 ELSE 0 END) AS gone
           FROM fan WHERE owner_uid = ?""",
        (owner_uid,),
    ).fetchone()
    return int(row["alive"] or 0), int(row["gone"] or 0)


def churn_rows(conn: sqlite3.Connection, owner_uid: int, since_ts: int) -> list[ChurnRow]:
    rows = conn.execute(
        """SELECT fan_uid, uname, mtime, gone_ts FROM fan
           WHERE owner_uid = ? AND gone_ts IS NOT NULL AND gone_ts >= ?
           ORDER BY gone_ts DESC, fan_uid""",
        (owner_uid, int(since_ts)),
    ).fetchall()
    return [
        ChurnRow(fan_uid=r["fan_uid"], uname=r["uname"], mtime=r["mtime"], gone_ts=r["gone_ts"])
        for r in rows
    ]


def fan_events_between(
    conn: sqlite3.Connection, owner_uid: int, start_ts: int, end_ts: int
) -> dict[str, int]:
    rows = conn.execute(
        """SELECT kind, COUNT(*) AS n FROM fan_event
           WHERE owner_uid = ? AND ts >= ? AND ts <= ? GROUP BY kind""",
        (owner_uid, int(start_ts), int(end_ts)),
    ).fetchall()
    return {r["kind"]: int(r["n"]) for r in rows}


def fan_totals(conn: sqlite3.Connection, owner_uid: int) -> dict[str, int]:
    """名单规模：all / active / gone。"""
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN gone_ts IS NULL THEN 1 ELSE 0 END) AS alive,
                  SUM(CASE WHEN gone_ts IS NOT NULL THEN 1 ELSE 0 END) AS gone
           FROM fan WHERE owner_uid = ?""",
        (owner_uid,),
    ).fetchone()
    return {
        "all": int(row["total"] or 0),
        "active": int(row["alive"] or 0),
        "gone": int(row["gone"] or 0),
    }


def fan_page(
    conn: sqlite3.Connection,
    owner_uid: int,
    *,
    status: str = "all",
    query: str = "",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[FanRow], int]:
    """分页取名单。status: all / active / gone；query 同时匹配昵称和 UID。"""
    where = ["owner_uid = ?"]
    params: list[object] = [owner_uid]
    if status == "active":
        where.append("gone_ts IS NULL")
    elif status == "gone":
        where.append("gone_ts IS NOT NULL")

    query = (query or "").strip()
    if query:
        where.append("(uname LIKE ? OR CAST(fan_uid AS TEXT) LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like])

    clause = " AND ".join(where)
    total = int(conn.execute(f"SELECT COUNT(*) AS n FROM fan WHERE {clause}", params).fetchone()["n"])

    if status == "gone":
        order = "gone_ts DESC, fan_uid"
    elif status == "active":
        order = "mtime DESC, fan_uid"
    else:
        # 还在关注的排前面，其次按关注时间从新到旧
        order = "(gone_ts IS NOT NULL) ASC, COALESCE(gone_ts, mtime) DESC, fan_uid"

    rows = conn.execute(
        f"""SELECT fan_uid, uname, mtime, first_seen_ts, last_seen_ts, gone_ts, attribute
            FROM fan WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?""",
        [*params, int(limit), int(offset)],
    ).fetchall()
    return [
        FanRow(
            fan_uid=r["fan_uid"],
            uname=r["uname"],
            mtime=r["mtime"],
            first_seen_ts=r["first_seen_ts"],
            last_seen_ts=r["last_seen_ts"],
            gone_ts=r["gone_ts"],
            attribute=r["attribute"],
        )
        for r in rows
    ], total


def recent_crawls(conn: sqlite3.Connection, owner_uid: int, limit: int = 5) -> list[FanCrawl]:
    rows = conn.execute(
        """SELECT * FROM fan_crawl WHERE owner_uid = ? AND finished_ts IS NOT NULL
           ORDER BY id DESC LIMIT ?""",
        (owner_uid, int(limit)),
    ).fetchall()
    return [
        FanCrawl(
            id=r["id"],
            owner_uid=r["owner_uid"],
            started_ts=r["started_ts"],
            finished_ts=r["finished_ts"],
            fetched=r["fetched"],
            total=r["total"],
            complete=bool(r["complete"]),
            note=r["note"],
        )
        for r in rows
    ]


# ---------- 逐次抓取的完整名单 ----------


def save_fan_snapshot(conn: sqlite3.Connection, crawl_id: int, owner_uid: int, fans: Iterable) -> int:
    """把这一次抓到的名单原样存一份。重复执行同一次抓取会覆盖。"""
    rows = [(int(crawl_id), owner_uid, f.mid, f.uname, f.mtime or None) for f in fans]
    if not rows:
        return 0
    conn.executemany(
        """INSERT INTO fan_snapshot (crawl_id, owner_uid, fan_uid, uname, mtime)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(crawl_id, fan_uid) DO UPDATE SET
             uname = excluded.uname, mtime = excluded.mtime""",
        rows,
    )
    conn.commit()
    return len(rows)


def snapshot_fans(
    conn: sqlite3.Connection,
    crawl_id: int,
    *,
    limit: int = 50,
    offset: int = 0,
    query: str = "",
) -> tuple[list[tuple[int, str, int | None]], int]:
    """读某一次抓取的名单：(fan_uid, uname, mtime) + 总数。"""
    where = ["crawl_id = ?"]
    params: list[object] = [int(crawl_id)]
    query = (query or "").strip()
    if query:
        where.append("(uname LIKE ? OR CAST(fan_uid AS TEXT) LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])
    clause = " AND ".join(where)

    total = int(conn.execute(f"SELECT COUNT(*) AS n FROM fan_snapshot WHERE {clause}", params).fetchone()["n"])
    rows = conn.execute(
        f"""SELECT fan_uid, uname, mtime FROM fan_snapshot WHERE {clause}
            ORDER BY mtime DESC, fan_uid LIMIT ? OFFSET ?""",
        [*params, int(limit), int(offset)],
    ).fetchall()
    return [(r["fan_uid"], r["uname"], r["mtime"]) for r in rows], total


def snapshot_counts(conn: sqlite3.Connection, owner_uid: int, limit: int = 20) -> list[tuple[int, int]]:
    """最近几次抓取各存了多少人：[(crawl_id, 人数), ...]。"""
    rows = conn.execute(
        """SELECT crawl_id, COUNT(*) AS n FROM fan_snapshot
           WHERE owner_uid = ? GROUP BY crawl_id ORDER BY crawl_id DESC LIMIT ?""",
        (owner_uid, int(limit)),
    ).fetchall()
    return [(r["crawl_id"], int(r["n"])) for r in rows]
