"""演示数据生成器：不联网也能看到一个完整的报告长什么样。"""

from __future__ import annotations

import random
import time
from datetime import date, timedelta

from .api import CreatorDay
from .db import add_snapshots_bulk, upsert_creator_days
from .timeutil import day_start_ts

# 一天 24 小时按 10 分钟切 144 段，涨粉在晚上更集中，掉粉相对均匀
_BUCKETS = 144
_WEIGHTS = []
for _b in range(_BUCKETS):
    _hour = _b / 6.0
    # 双峰：午休 + 晚间黄金档
    _w = 0.25 + 1.6 * pow(2.718, -((_hour - 12.5) ** 2) / 6.0) + 2.2 * pow(2.718, -((_hour - 21.0) ** 2) / 5.0)
    _WEIGHTS.append(_w)
_TOTAL_W = sum(_WEIGHTS)


def _spread(total: int, rng: random.Random) -> list[int]:
    """把 total 个事件按权重随机撒进 144 个时间桶。"""
    counts = [0] * _BUCKETS
    for _ in range(total):
        r = rng.random() * _TOTAL_W
        acc = 0.0
        for idx, w in enumerate(_WEIGHTS):
            acc += w
            if r <= acc:
                counts[idx] += 1
                break
        else:
            counts[-1] += 1
    return counts


def seed_demo(conn, uid: int, *, days: int = 32, start_fans: int = 128_000, today: date, tz) -> dict:
    """生成 days 天数据；除今天外都有创作中心口径的精确值。"""
    rng = random.Random(20261112)
    first_day = today - timedelta(days=days - 1)
    fans = start_fans
    creator_rows: list[CreatorDay] = []
    cutoff = int(time.time())

    for offset in range(days):
        day = first_day + timedelta(days=offset)
        base = day_start_ts(day, tz)
        growth = 40 + 22 * (1 if day.weekday() >= 4 else 0)  # 周末略高
        new_today = max(0, int(rng.gauss(growth, 18)))
        lost_today = max(0, int(rng.gauss(14, 8)))
        if offset == days - 2:  # 造一个爆款日
            new_today += 380
        if offset == days - 5:  # 造一个翻车日
            lost_today += 190

        gains = _spread(new_today, rng)
        losses = _spread(lost_today, rng)
        elapsed = _BUCKETS
        if base + (_BUCKETS - 1) * 600 > cutoff:
            # 今天只过了一部分：把当天的事件按已过去的时段重新撒一遍，
            # 否则报告里「今天」会是一片 0，看不出真实形态
            elapsed = max(1, (cutoff - base) // 600 + 1)
            gains = _spread(int(new_today * elapsed / _BUCKETS), rng)
            losses = _spread(int(lost_today * elapsed / _BUCKETS), rng)

        if offset == days - 1:
            # 演示：假设今天凌晨发了个视频，头几个小时就有一波明显涨粉
            burst = 55
            for b in range(elapsed):
                gains[b] += burst // elapsed + (1 if b < burst % elapsed else 0)

        batch: list[tuple[int, int, int, int | None, str]] = []
        for bucket in range(elapsed):
            fans += gains[bucket] - losses[bucket]
            ts = base + bucket * 600
            batch.append((uid, ts, fans, None, "demo"))
        add_snapshots_bulk(conn, batch)

        if offset < days - 1:  # 今天还没到 T+1，创作中心不会给数
            creator_rows.append(CreatorDay(day=day, new_fans=new_today, lost_fans=lost_today, total_fans=fans))

    upsert_creator_days(conn, uid, creator_rows)
    return {"days": days, "final_fans": fans, "rows": len(creator_rows)}


_NICKS = [
    "路过的风", "小破站常驻", "咕咕咕", "奶盖不加冰", "加班狗", "今天也要早睡",
    "爱看番的猫", "一只咸鱼", "键盘侠本侠", "深夜刷视频", "考古队队长", "云端漫步",
    "不吃香菜", "momo", "阿巴阿巴", "某科学的超电磁炮", "三点水", "夜航星",
]


def seed_fans(conn, owner_uid: int, *, now: int, count: int = 140, churn: int = 11) -> dict:
    """造一批粉丝 + 若干取关记录，让 churn 命令有东西可看。"""
    rng = random.Random(20261113)
    day = 86400
    rows, events, crawls = [], [], []

    for i in range(count):
        mid = 100000 + i * 37
        followed = now - rng.randint(1 * day, 70 * day)
        uname = _NICKS[i % len(_NICKS)] + ("" if i < len(_NICKS) else str(i // len(_NICKS) + 1))
        gone = None
        if i < churn:
            # 一半是「刚关注没几天就跑了」，一半是老粉流失
            if i % 2 == 0:
                gone = min(now, followed + rng.randint(1, 6) * day)
            else:
                gone = now - rng.randint(0, 7 * day)
                if gone <= followed:
                    gone = followed + day
            gone = min(gone, now)
            events.append((owner_uid, mid, "unfollow", gone, uname, None))
        rows.append(
            (owner_uid, mid, uname, followed, "", "", rng.choice([0, 0, 0, 2, 6]),
             followed, gone or now, gone)
        )

    conn.executemany(
        """INSERT INTO fan (owner_uid, fan_uid, uname, mtime, face, sign, attribute,
                            first_seen_ts, last_seen_ts, gone_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(owner_uid, fan_uid) DO UPDATE SET
             uname = excluded.uname, mtime = excluded.mtime, gone_ts = excluded.gone_ts""",
        rows,
    )
    if events:
        conn.executemany(
            "INSERT INTO fan_event (owner_uid, fan_uid, kind, ts, uname, crawl_id) VALUES (?, ?, ?, ?, ?, ?)",
            events,
        )
    # 两次抓取：8 天前还没人取关（151 人），昨天抓的时候剩 140 人
    crawls.append((owner_uid, now - 8 * day, now - 8 * day, count + churn, count + churn, 1, "演示数据"))
    crawls.append((owner_uid, now - day, now - day, count, count, 1, "演示数据"))
    conn.executemany(
        """INSERT INTO fan_crawl (owner_uid, started_ts, finished_ts, fetched, total, complete, note)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        crawls,
    )
    conn.commit()
    return {"fans": count, "gone": len(events)}
