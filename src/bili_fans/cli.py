"""命令行入口：bilifans <命令>。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Sequence

from . import __version__
from .api import ApiError, BiliClient
from .config import Config, load_config
from .db import (
    Account,
    churn_rows,
    clear_account_data,
    connect,
    delete_account,
    fan_counts,
    fan_page,
    fan_totals,
    get_account,
    last_snapshot,
    latest_fan_crawl,
    list_accounts,
    snapshot_count,
    upsert_account,
)
from .demo import seed_demo, seed_fans
from .fans import crawl, summarize_churn, survival_days
from .http import HttpError, Transport
from .report import render_churn, render_fan_list, render_html, render_text, write_html
from .service import backfill_account, collect, poll_account, resolve_cookie
from .timeutil import day_start_ts, fmt_ts, get_tz, local_day, now_ts, to_local

PROG = "bilifans"


# ---------------------------------------------------------------- 解析与上下文


def build_parser() -> argparse.ArgumentParser:
    # 公共参数同时挂在主命令和子命令上，所以要 SUPPRESS 默认值：
    # 否则子解析器会用 None 覆盖掉写在前面的 --db，导致参数静默失效。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, help="数据库路径（默认 bilifans.db）")
    common.add_argument("--config", default=argparse.SUPPRESS, help="配置文件路径（默认 ./bilifans.toml）")
    common.add_argument("--tz", default=argparse.SUPPRESS, help="统计时区（默认 Asia/Shanghai）")
    common.add_argument("-q", "--quiet", default=argparse.SUPPRESS, action="store_true", help="只输出错误")

    p = argparse.ArgumentParser(
        prog=PROG,
        parents=[common],
        description="统计 B 站账号每天的掉粉与新增粉丝（纯标准库实现）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常见用法：\n"
            "  bilifans add 123456 --name 我的号\n"
            "  bilifans login               # 扫码一次，自动取回并保存 Cookie\n"
            "  bilifans sync --all          # 采快照 + 拉近 30 天精确数据\n"
            "  bilifans report --all        # 终端看表\n"
            "  bilifans export --all        # 生成 HTML 报告\n"
            "  bilifans fans --all          # 抓粉丝名单，比对出谁取关了（需 Cookie）\n"
            "  bilifans churn --days 7      # 看具体是谁取关了你\n"
            "  bilifans serve               # 打开网页看板（图形界面）\n"
        ),
    )
    p.add_argument("-V", "--version", action="version", version=f"{PROG} {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="命令")

    def target_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--uid", type=int, action="append", help="指定 UID，可重复")
        sp.add_argument("-a", "--all", action="store_true", help="对所有已添加账号执行")

    sp = sub.add_parser("init", parents=[common], help="初始化数据库")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("add", parents=[common], help="添加要监听的账号")
    sp.add_argument("uid", type=int)
    sp.add_argument("--name", default="", help="备注名，留空则自动抓取昵称")
    sp.add_argument("--note", default="", help="备注")
    sp.set_defaults(func=cmd_add)

    for name in ("rm", "remove"):
        sp = sub.add_parser(name, parents=[common], help="删除账号及其历史数据")
        sp.add_argument("uid", type=int)
        sp.set_defaults(func=cmd_rm)

    sp = sub.add_parser("list", parents=[common], help="列出账号")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("poll", parents=[common], help="采集一次总粉丝数快照")
    target_args(sp)
    sp.set_defaults(func=cmd_poll)

    sp = sub.add_parser("backfill", parents=[common], help="用 Cookie 拉取创作中心近 30 天精确涨掉粉")
    target_args(sp)
    sp.add_argument("--cookie", default="", help="SESSDATA 或完整 Cookie；留空读环境变量")
    sp.set_defaults(func=cmd_backfill)

    sp = sub.add_parser("sync", parents=[common], help="poll + backfill，日常定时任务跑这个")
    target_args(sp)
    sp.add_argument("--cookie", default="", help="同上")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("report", parents=[common], help="终端查看逐日新增/掉粉")
    target_args(sp)
    sp.add_argument("--days", type=int, default=0, help="统计最近多少天（默认取配置，30）")
    sp.add_argument("--json", action="store_true", help="输出 JSON")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("export", parents=[common], help="生成 HTML 报告")
    target_args(sp)
    sp.add_argument("--days", type=int, default=0)
    sp.add_argument("-o", "--out", default="", help="输出路径，默认 reports/bilifans-<uid>-<日期>.html")
    sp.add_argument("--cookie", default="", help="用于判断报告里是否标注为精确数据")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("doctor", parents=[common], help="逐项体检：Cookie、接口、风控，定位到底哪一步不通")
    sp.add_argument("--cookie", default="")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("install", parents=[common], help="生成一键启动的 App 和全局命令")
    sp.add_argument("--app-dir", default="", help="App 放哪，默认 ~/Applications")
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--no-app", action="store_true", help="只装命令，不生成 App")
    sp.add_argument("--no-cli", action="store_true", help="只生成 App，不装命令")
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("login", parents=[common], help="打开浏览器扫码登录，自动取回并保存 Cookie")
    sp.add_argument("--timeout", type=int, default=180, help="等待扫码的秒数，默认 180")
    sp.add_argument("--headless", action="store_true", help="配合 --browser：不弹窗口")
    sp.add_argument("--profile", default="", help="浏览器 profile 目录，默认 ~/.bilifans/chrome-profile")
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("serve", parents=[common], help="启动本地网页看板（图形界面）")
    sp.add_argument("--port", type=int, default=8787, help="监听端口，默认 8787")
    sp.add_argument("--host", default="127.0.0.1", help="监听地址，默认只监听本机")
    sp.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    sp.add_argument("--cookie", default="")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("whoami", parents=[common], help="检查 Cookie 是否有效、登录的是哪个号")
    sp.add_argument("--cookie", default="")
    sp.set_defaults(func=cmd_whoami)

    sp = sub.add_parser("fans", parents=[common], help="抓一次粉丝名单，用于比对出谁取关了你")
    target_args(sp)
    sp.add_argument("--limit", type=int, default=1000, help="最多抓多少人（B 站上限 1000）")
    sp.add_argument("--ps", type=int, default=50, help="每页人数")
    sp.add_argument("--cookie", default="")
    sp.add_argument("--list", action="store_true", help="不抓取，只列出已存的名单")
    sp.add_argument("--status", choices=["all", "active", "gone"], default="all", help="配合 --list 过滤")
    sp.add_argument("--search", default="", help="配合 --list，按昵称或 UID 搜索")
    sp.add_argument("--page", type=int, default=1, help="配合 --list 翻页")
    sp.add_argument("--page-size", type=int, default=50, help="配合 --list，每页条数")
    sp.set_defaults(func=cmd_fans)

    sp = sub.add_parser("churn", parents=[common], help="查看具体是谁取关了你")
    target_args(sp)
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_churn)

    sp = sub.add_parser("probe", parents=[common], help="打印接口原始返回，排查字段变化")
    sp.add_argument("--cookie", default="")
    sp.add_argument("--uid", type=int, default=0, help="顺便查一下这个 UID 的公开粉丝数")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("daemon", parents=[common], help="常驻定时采集（没有 cron 时用这个）")
    target_args(sp)
    sp.add_argument("--interval", type=int, default=0, help="采样间隔分钟数，默认取配置")
    sp.add_argument("--cookie", default="")
    sp.set_defaults(func=cmd_daemon)

    sp = sub.add_parser("seed-demo", parents=[common], help="灌入演示数据，先看看效果")
    sp.add_argument("--uid", type=int, default=42)
    sp.add_argument("--name", default="演示账号")
    sp.add_argument("--days", type=int, default=32)
    sp.set_defaults(func=cmd_seed_demo)

    return p


class Ctx:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.cfg: Config = load_config(getattr(args, "config", None))
        db = getattr(args, "db", None)
        if db:
            self.cfg.db = db
        tz = getattr(args, "tz", None)
        if tz:
            self.cfg.timezone = tz
        self.tz = get_tz(self.cfg.timezone)
        self.conn = connect(self.cfg.db_path())
        self.cookie, self.cookie_source = resolve_cookie(self.cfg, getattr(args, "cookie", ""))
        self.quiet = bool(getattr(args, "quiet", False))

    def say(self, text: str = "") -> None:
        if not self.quiet:
            print(text)

    def close(self) -> None:
        self.conn.close()

    def client(self) -> BiliClient:
        transport = Transport(timeout=self.cfg.timeout, retries=self.cfg.retries, cookie=self.cookie)
        return BiliClient(transport, cookie=self.cookie)

    def targets(self) -> list[Account]:
        args = self.args
        accounts = list_accounts(self.conn)
        if getattr(args, "all", False) or not getattr(args, "uid", None):
            if not accounts:
                raise ValueError("还没有添加任何账号，先执行：bilifans add <UID>")
            return accounts
        wanted = []
        for uid in dict.fromkeys(args.uid):
            acc = get_account(self.conn, uid)
            if acc is None:
                acc = upsert_account(self.conn, uid, "", "")
            wanted.append(acc)
        return wanted

    def days(self) -> int:
        value = getattr(self.args, "days", 0) or self.cfg.default_days
        return max(1, int(value))


# ---------------------------------------------------------------- 命令实现


def cmd_init(ctx: Ctx) -> int:
    path = ctx.cfg.db_path()
    ctx.say(f"数据库已就绪：{path}")
    ctx.say(f"  账号 {len(list_accounts(ctx.conn))} 个")
    return 0


def cmd_add(ctx: Ctx) -> int:
    uid = ctx.args.uid
    name = ctx.args.name.strip()
    if not name:
        try:
            profile = ctx.client().profile(uid)
            if profile:
                name = profile.name
        except (ApiError, HttpError):
            pass
    acc = upsert_account(ctx.conn, uid, name, ctx.args.note)
    ctx.say(f"已添加：{acc.label}")
    if not name:
        ctx.say("  （没抓到昵称，可以后面用 bilifans add 同 UID 加 --name 改名）")
    return 0


def cmd_rm(ctx: Ctx) -> int:
    n = delete_account(ctx.conn, ctx.args.uid)
    ctx.say(f"已删除 UID {ctx.args.uid}，连带 {n} 条记录")
    return 0


def cmd_list(ctx: Ctx) -> int:
    accounts = list_accounts(ctx.conn)
    if not accounts:
        ctx.say("还没有账号。执行：bilifans add <UID>")
        return 0
    for acc in accounts:
        last = last_snapshot(ctx.conn, acc.uid)
        tail = ""
        if last:
            tail = f"  最近采样 {last.fans:,} 粉丝 @ {fmt_ts(last.ts, ctx.tz)[5:]}"
        ctx.say(f"  {acc.uid:<12} {acc.name or '(未知昵称)':<16}{tail}")
        ctx.say(f"  {'':12} 快照 {snapshot_count(ctx.conn, acc.uid)} 条")
    return 0


def cmd_poll(ctx: Ctx) -> int:
    accounts = ctx.targets()
    client = ctx.client()
    ok = 0
    for i, acc in enumerate(accounts):
        success, msg = poll_account(ctx.conn, client, acc, ctx.tz)
        ctx.say(("  ✓ " if success else "  ✗ ") + msg)
        ok += int(success)
        if i + 1 < len(accounts):
            time.sleep(0.4)  # 多账号之间留一点间隔
    return 0 if ok == len(accounts) else 1


def cmd_backfill(ctx: Ctx) -> int:
    accounts = ctx.targets()
    client = ctx.client()
    ok = 0
    for i, acc in enumerate(accounts):
        success, msg = backfill_account(
            ctx.conn, client, acc, cookie=ctx.cookie, creator_url=ctx.cfg.creator_url
        )
        ctx.say(("  ✓ " if success else "  ✗ ") + msg)
        ok += int(success)
        if i + 1 < len(accounts):
            time.sleep(0.6)
    return 0 if ok == len(accounts) else 1


def cmd_sync(ctx: Ctx) -> int:
    rc_poll = cmd_poll(ctx)
    ctx.say("")
    rc_bf = cmd_backfill(ctx)
    return 0 if rc_poll == 0 and rc_bf == 0 else 1


def _collect(ctx: Ctx, acc: Account, days: int):
    return collect(ctx.conn, acc.uid, days, ctx.tz)


def cmd_report(ctx: Ctx) -> int:
    accounts = ctx.targets()
    for i, acc in enumerate(accounts):
        rows, summary = _collect(ctx, acc, ctx.days())
        if ctx.args.json:
            payload = {
                "uid": acc.uid,
                "name": acc.name,
                "days": ctx.days(),
                "rows": [
                    {
                        "day": row.day.isoformat(),
                        "net": row.net,
                        "new": row.display_new,
                        "lost": row.display_lost,
                        "exact": row.has_exact,
                        "close_fans": row.close_fans,
                        "samples": row.samples,
                        "source": row.source,
                        "note": row.note,
                    }
                    for row in rows
                ],
                "summary": {
                    "new_total": summary.new_total,
                    "lost_total": summary.lost_total,
                    "net_total": summary.net_total,
                    "avg_new": round(summary.avg_new, 2),
                    "avg_lost": round(summary.avg_lost, 2),
                    "exact_days": summary.exact_days,
                    "est_days": summary.est_days,
                    "churn_rate": summary.churn_rate,
                    "last_fans": summary.last_fans,
                },
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            if i:
                print()
            print(render_text(acc.label, rows, summary, ctx.cfg.timezone))
    return 0


def cmd_export(ctx: Ctx) -> int:
    accounts = ctx.targets()
    out_path = ctx.args.out.strip()
    rc = 0
    for i, acc in enumerate(accounts):
        rows, summary = _collect(ctx, acc, ctx.days())
        target = Path(out_path)
        if not out_path:
            today = local_day(now_ts(), ctx.tz).isoformat()
            target = Path("reports") / f"bilifans-{acc.uid}-{today}.html"
        elif len(accounts) > 1:
            target = target.with_name(f"{target.stem}-{acc.uid}{target.suffix}")

        content = render_html(
            acc.label,
            rows,
            summary,
            tz_name=ctx.cfg.timezone,
            cookie_ok=bool(ctx.cookie),
        )
        written = write_html(str(target), content)
        ctx.say(f"已生成：{written}")
        if summary.exact_days + summary.est_days == 0:
            ctx.say("  ⚠ 报告里还没有数据，先跑 bilifans sync --all")
            rc = 1
    return rc


HINTS = {
    -101: "Cookie 失效了：重新 bilifans login（或在看板点「登录 B 站」）",
    -352: "被风控拦了：换个网络（家用宽带/手机热点）再试，别在机房 IP 上跑",
    -412: "被 B 站拦截：等几分钟，或换个网络",
    -404: "账号不存在，检查 UID",
}


def _explain(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    hint = HINTS.get(code)
    return f"{exc}" + (f"\n      → {hint}" if hint else "")


def cmd_doctor(ctx: Ctx) -> int:
    """把「为什么不通」拆成一条条可确认的结论。"""
    from .browser import COOKIE_FILE
    from .server import DEFAULT_PORT, running_health

    ok = True
    say = ctx.say

    say("【配置】")
    say(f"  ✓ 数据库   {ctx.cfg.db_path()}")
    accounts = list_accounts(ctx.conn)
    if accounts:
        names = "、".join(a.label for a in accounts[:3])
        say(f"  ✓ 监听账号 {len(accounts)} 个：{names}")
    else:
        say("  ⚠ 还没有添加账号：bilifans add <UID>")
        ok = False

    if ctx.cookie:
        fields = {p.split("=", 1)[0].strip() for p in ctx.cookie.split(";") if "=" in p}
        say(f"  ✓ Cookie   {ctx.cookie_source}")
        have = "、".join(sorted(f for f in ("SESSDATA", "bili_jct", "DedeUserID", "buvid3") if f in fields))
        say(f"             含 {have or '（空）'}")
        if "buvid3" not in fields:
            say("             ⚠ 没有 buvid3，容易吃 -352；重新 login 会自动补上")
    else:
        say(f"  ✗ Cookie   没有（{COOKIE_FILE} 不存在）")
        say("      → 运行 bilifans login，或打开看板点「登录 B 站」")
        ok = False

    uid = accounts[0].uid if accounts else None
    client = ctx.client()

    say("")
    say("【接口】")
    if uid:
        try:
            stat = client.stat(uid)
            say(f"  ✓ 公开接口 通，当前 {stat.fans:,} 粉丝")
        except (ApiError, HttpError) as exc:
            say(f"  ✗ 公开接口失败：{_explain(exc)}")
            ok = False

    if not ctx.cookie:
        say("  · 登录相关检查已跳过（没有 Cookie）")
    else:
        try:
            mid, uname = client.me()
            say(f"  ✓ 登录态   有效：{uname}（UID {mid}）")
        except (ApiError, HttpError) as exc:
            say(f"  ✗ 登录态   失败：{_explain(exc)}")
            mid, uname = None, ""
            ok = False

        try:
            report = client.creator_report(ctx.cfg.creator_url)
            if report.days:
                say(f"  ✓ 创作中心 可用，近 30 天有 {len(report.days)} 天数据")
            else:
                say("  ⚠ 创作中心 接口通了但没返回数据")
        except (ApiError, HttpError) as exc:
            say(f"  ✗ 创作中心 失败：{_explain(exc)}")
            ok = False

        target = uid or mid
        if target:
            try:
                page = client.followers(target, pn=1, ps=5)
                extra = f"，共 {page.total:,} 个粉丝" if page.total else ""
                say(f"  ✓ 粉丝列表 可用{extra}")
            except (ApiError, HttpError) as exc:
                say(f"  ✗ 粉丝列表 失败：{_explain(exc)}")

    say("")
    say("【看板服务】")
    health = running_health(DEFAULT_PORT)
    if health:
        state = "（旧代码，重启后新功能才生效）" if health.get("stale") else ""
        say(f"  ✓ 正在 http://127.0.0.1:{DEFAULT_PORT}/ 运行{state}")
    else:
        say(f"  · 没在 {DEFAULT_PORT} 端口运行（想开就 bilifans serve）")

    say("")
    if ok:
        say("结论：都通。Safari / Chrome 用哪个都一样，逻辑全在服务端。")
        return 0
    say("结论：上面标 ✗ 的就是卡住的地方，按「→」后面的提示处理。")
    return 1


def cmd_install(ctx: Ctx) -> int:
    from .shortcut import install

    result = install(
        app_dir=Path(ctx.args.app_dir).expanduser() if ctx.args.app_dir else None,
        with_app=not ctx.args.no_app,
        with_cli=not ctx.args.no_cli,
        port=ctx.args.port,
    )

    if result.app_path:
        ctx.say(f"✓ 已生成 App：{result.app_path}")
        ctx.say("   双击它，或者拖进 Dock 点一下，就会启动看板并打开浏览器。")
        ctx.say("   看板已经在运行时再点一次，只会打开页面，不会重复启动。")
    if result.cli_path:
        if result.app_path:
            ctx.say("")
        ctx.say(f"✓ 已安装命令：{result.cli_path}")
        if result.on_path:
            ctx.say("   在任意目录敲 `bilifans serve` 就能启动。")
        else:
            ctx.say("   ⚠ 这个目录不在 PATH 里，把下面这行加到 ~/.zshrc：")
            ctx.say(f"      {result.path_hint}")
            ctx.say("   重新打开终端后，就能在任意目录敲 `bilifans serve`。")
    ctx.say("")
    ctx.say("两个入口都只是壳，数据仍在项目目录的 bilifans.db 里。")
    return 0


def cmd_login(ctx: Ctx) -> int:
    """打开浏览器窗口扫码登录，拿到 Cookie 后自动登记这个号。"""
    from .browser import COOKIE_FILE, PROFILE_DIR, BrowserError, capture_cookie, save_cookie

    profile = Path(ctx.args.profile).expanduser() if ctx.args.profile else PROFILE_DIR
    ctx.say("正在启动浏览器…")
    ctx.say("  首次使用：在弹出的窗口里扫码登录 B 站（这个窗口的登录状态会被记住，下次不用再扫）")
    ctx.say("")

    last_shown = [0.0, ""]

    def on_progress(text: str) -> None:
        if text != last_shown[1]:
            last_shown[1] = text
            ctx.say(f"  {text}")

    def on_wait(remaining: float) -> None:
        now = time.time()
        if now - last_shown[0] < 5:
            return
        last_shown[0] = now
        ctx.say(f"  等待扫码…还剩 {int(remaining)} 秒")

    try:
        result = capture_cookie(
            profile,
            timeout=max(3, ctx.args.timeout),
            headless=ctx.args.headless,
            on_wait=on_wait,
            on_progress=on_progress,
        )
    except BrowserError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        print("  也可以按 docs/get-cookie.md 手动复制 Cookie", file=sys.stderr)
        return 1

    path = save_cookie(COOKIE_FILE, result.cookie)
    ctx.say("")
    ctx.say(f"✓ 已取回 Cookie（{len(result.names)} 个字段，用时 {result.seconds:.1f} 秒）")
    ctx.say(f"  {result.masked}")
    ctx.say(f"  已保存到 {path}（权限 600，只有你自己能读）")

    # 顺手把登录的这个号登记进来，省得再去 add
    uid, uname = _whoami_with_cookie(result.cookie)
    if uid:
        acc = upsert_account(ctx.conn, uid, uname, "")
        ctx.say(f"✓ 已登记账号：{acc.label}")

    ctx.say("")
    ctx.say("  之后 sync / backfill / fans / serve 都会自动用它。")
    ctx.say("  Cookie 过期时（报 -101）重新执行一次 bilifans login 即可。")
    return 0


def _whoami_with_cookie(cookie: str) -> tuple[int, str]:
    """用刚拿到的 Cookie 反查 UID 和昵称；失败就返回 (0, "")。"""
    try:
        client = BiliClient(Transport(cookie=cookie), cookie=cookie)
        return client.me()
    except (ApiError, HttpError):
        return 0, ""


def cmd_serve(ctx: Ctx) -> int:
    from .server import serve

    return serve(
        ctx.cfg,
        host=ctx.args.host,
        port=ctx.args.port,
        cookie=ctx.cookie,
        open_browser=not ctx.args.no_open,
    )


def cmd_whoami(ctx: Ctx) -> int:
    """一条命令回答「我这个 Cookie 到底行不行」。"""
    if not ctx.cookie:
        raise ValueError(
            "还没有配置 Cookie。\n"
            "    获取方法见 docs/get-cookie.md\n"
            "    然后：export BILI_COOKIE='SESSDATA=...'"
        )

    client = ctx.client()
    try:
        uid, uname = client.me()
    except (ApiError, HttpError) as exc:
        ctx.say(f"✗ Cookie 校验失败（来源：{ctx.cookie_source or '无'}）：{exc}")
        ctx.say("  获取方法见 docs/get-cookie.md；取值时确认浏览器已登录 www.bilibili.com")
        return 1

    ctx.say(f"✓ Cookie 有效（来源：{ctx.cookie_source}）")
    ctx.say(f"  登录账号  {uname}（UID {uid}）")

    try:
        report = client.creator_report(ctx.cfg.creator_url)
    except (ApiError, HttpError) as exc:
        ctx.say(f"  ✗ 创作中心不可用：{exc}")
    else:
        if report.days:
            first, last = report.days[0].day, report.days[-1].day
            ctx.say(f"  创作中心  可用，近 30 天里有 {len(report.days)} 天数据（{first} ~ {last}）")
        else:
            ctx.say("  创作中心  接口通了，但没返回数据（可能这个号还没有粉丝数据）")

    acc = get_account(ctx.conn, uid)
    if acc:
        ctx.say(f"  监听列表  已添加：{acc.label}")
    else:
        ctx.say(f"  监听列表  未添加，执行：bilifans add {uid} --name {uname}")

    others = [a for a in list_accounts(ctx.conn) if a.uid != uid]
    if others:
        names = "、".join(f"{a.name or a.uid}({a.uid})" for a in others)
        ctx.say(f"  ⚠ 监听列表里还有别的号：{names}；创作中心只返回登录账号自己的数据")
    return 0


def cmd_fans(ctx: Ctx) -> int:
    if ctx.args.list:
        return cmd_fan_list(ctx)

    accounts = ctx.targets()
    if not ctx.cookie:
        raise ValueError(
            "抓粉丝名单必须登录：B 站要求 Cookie 才给看粉丝列表。\n"
            "    export BILI_COOKIE='SESSDATA=...'  然后重试"
        )
    client = ctx.client()
    owner = client.nav()
    rc = 0
    for i, acc in enumerate(accounts):
        if i:
            ctx.say("")
        ctx.say(f"  · {acc.label}")
        if owner and owner[0] != acc.uid:
            # B 站只给看「自己」前 1000 名粉丝，别人的号只能看前 100 名，
            # 拿这种残缺名单做比对会得出完全错误的取关结论。
            ctx.say(
                f"    ✗ Cookie 登录的是 UID {owner[0]}（{owner[1]}），不是 {acc.uid}；"
                "粉丝名单只能抓自己账号的，已跳过"
            )
            rc = 1
            continue
        try:
            out = crawl(
                client,
                ctx.conn,
                acc.uid,
                limit=max(1, ctx.args.limit),
                ps=max(1, min(50, ctx.args.ps)),
            )
        except (ApiError, HttpError) as exc:
            ctx.say(f"    ✗ 抓取失败：{exc}")
            rc = 1
            continue

        alive, gone_total = fan_counts(ctx.conn, acc.uid)
        ctx.say(f"    名单 {out.fetched} 人 · {out.coverage}")
        ctx.say(f"    库内共 {alive} 人在关注、{gone_total} 人已确认取关")
        if out.baseline:
            ctx.say("    首次抓取，已建立基线；下次抓取才能比对出取关")
        else:
            ctx.say(f"    新关注 {len(out.new_fans)} 人 · 确认取关 {len(out.gone)} 人")
            if out.gone:
                names = "、".join(f"{r.uname or r.fan_uid}({r.fan_uid})" for r in out.gone[:10])
                more = f" 等 {len(out.gone)} 人" if len(out.gone) > 10 else ""
                ctx.say(f"      取关：{names}{more}")
                ctx.say("      完整名单：bilifans churn --days 7")
        if out.guard_skipped:
            ctx.say(f"    ⚠ {out.note}")
        elif out.note and not out.complete:
            ctx.say(f"    ⚠ {out.note}")
    return rc


def cmd_fan_list(ctx: Ctx) -> int:
    accounts = ctx.targets()
    size = max(1, min(500, ctx.args.page_size))
    page = max(1, ctx.args.page)
    rc = 0
    for i, acc in enumerate(accounts):
        totals = fan_totals(ctx.conn, acc.uid)
        rows, total = fan_page(
            ctx.conn,
            acc.uid,
            status=ctx.args.status,
            query=ctx.args.search,
            limit=size,
            offset=(page - 1) * size,
        )
        if i:
            print()
        print(
            render_fan_list(
                acc.label,
                rows,
                totals,
                total=total,
                page=page,
                size=size,
                tz_name=ctx.cfg.timezone,
            )
        )
        if totals.get("all", 0) == 0:
            rc = 1
    return rc


def cmd_churn(ctx: Ctx) -> int:
    accounts = ctx.targets()
    days = max(1, ctx.args.days)
    end = local_day(now_ts(), ctx.tz)
    start = end - timedelta(days=days - 1)
    since = day_start_ts(start, ctx.tz)
    rc = 0

    for i, acc in enumerate(accounts):
        rows = churn_rows(ctx.conn, acc.uid, since)
        summary = summarize_churn(rows)
        last = latest_fan_crawl(ctx.conn, acc.uid)
        if last is None or last.fetched == 0:
            note = "还没有成功抓过粉丝名单。先运行 bilifans fans --all（需要 Cookie），抓两次之后才有结果。"
        elif last.complete:
            note = ""
        else:
            total = f"，总粉丝 {last.total:,}" if last.total else ""
            note = (
                f"上次只覆盖最近 {last.fetched} 名粉丝{total}，更早的粉丝取关检测不到；"
                "这是 B 站「登录后最多看前 1000 名粉丝」的限制。"
            )

        if ctx.args.json:
            payload = {
                "uid": acc.uid,
                "name": acc.name,
                "days": days,
                "range": [start.isoformat(), end.isoformat()],
                "coverage_complete": bool(last and last.complete),
                "coverage_note": note,
                "churned": [
                    {
                        "fan_uid": r.fan_uid,
                        "uname": r.uname,
                        "followed_at": r.mtime,
                        "gone_at": r.gone_ts,
                        "survival_days": survival_days(r.mtime, r.gone_ts),
                    }
                    for r in rows
                ],
                "summary": summary,
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            if i:
                print()
            print(
                render_churn(
                    acc.label,
                    rows,
                    summary,
                    start_day=start,
                    end_day=end,
                    tz_name=ctx.cfg.timezone,
                    coverage_note=note,
                )
            )
            if last is None or last.fetched == 0:
                rc = 1
    return rc


def cmd_probe(ctx: Ctx) -> int:
    client = ctx.client()
    if ctx.args.uid:
        try:
            print(json.dumps({"stat": asdict(client.stat(ctx.args.uid))}, ensure_ascii=False, indent=2))
        except (ApiError, HttpError) as exc:
            print(f"公开接口失败：{exc}", file=sys.stderr)

    print(f"GET {ctx.cfg.creator_url}")
    print(f"Cookie：{'已配置，长度 %d' % len(ctx.cookie) if ctx.cookie else '未配置'}")
    try:
        payload = client.transport.get_json(ctx.cfg.creator_url, None, referer="https://member.bilibili.com/")
    except (ApiError, HttpError) as exc:
        print(f"请求失败：{exc}", file=sys.stderr)
        return 1
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text[:3000] + ("\n……（已截断）" if len(text) > 3000 else ""))
    return 0


def cmd_daemon(ctx: Ctx) -> int:
    interval = max(1, ctx.args.interval or ctx.cfg.interval_minutes)
    accounts = ctx.targets()
    ctx.say(f"开始常驻采集：{len(accounts)} 个账号，每 {interval} 分钟一次，Ctrl+C 停止")
    last_backfill_day: date | None = None
    try:
        while True:
            ctx.say(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]")
            cmd_poll(ctx)
            today = local_day(now_ts(), ctx.tz)
            if ctx.cookie and last_backfill_day != today and to_local(now_ts(), ctx.tz).hour >= 1:
                ctx.say("  · 尝试回填创作中心数据")
                cmd_backfill(ctx)
                last_backfill_day = today
            time.sleep(interval * 60)
    except KeyboardInterrupt:
        ctx.say("\n已停止")
    return 0


def cmd_seed_demo(ctx: Ctx) -> int:
    acc = upsert_account(ctx.conn, ctx.args.uid, ctx.args.name, "演示数据")
    clear_account_data(ctx.conn, acc.uid)  # 重复执行时先清掉上次的演示数据
    info = seed_demo(
        ctx.conn,
        acc.uid,
        days=ctx.args.days,
        today=local_day(now_ts(), ctx.tz),
        tz=ctx.tz,
    )
    fan_info = seed_fans(ctx.conn, acc.uid, now=now_ts())
    ctx.say(f"已写入演示数据：UID {acc.uid}，{info['days']} 天，{info['rows']} 天精确明细")
    ctx.say(f"  另造了 {fan_info['fans']} 个粉丝，其中 {fan_info['gone']} 人在近 7 天取关，可以试 churn")
    ctx.say(f"  取关名单：  bilifans churn --uid {acc.uid}")
    ctx.say(f"  现在可以看：bilifans report --uid {acc.uid}")
    ctx.say(f"  生成网页：  bilifans export --uid {acc.uid}")
    return 0


# ---------------------------------------------------------------- 入口


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0

    ctx: Ctx | None = None
    try:
        ctx = Ctx(args)
        return int(args.func(ctx) or 0)
    except ApiError as exc:
        print(f"接口错误：{exc}", file=sys.stderr)
        if exc.code == -101:
            print("  提示：运行 bilifans login 可以自动重新获取 Cookie（会打开浏览器）", file=sys.stderr)
        return 1
    except HttpError as exc:
        print(f"接口错误：{exc}", file=sys.stderr)
        return 1
    except (ValueError, FileNotFoundError, sqlite3.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    finally:
        if ctx is not None:
            ctx.close()


if __name__ == "__main__":
    raise SystemExit(main())
