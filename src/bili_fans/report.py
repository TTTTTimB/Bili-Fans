"""输出层：终端对齐表格 + 单文件 HTML 报告（内联 CSS / SVG，不依赖任何外部资源）。"""

from __future__ import annotations

import html
import math
import unicodedata
from datetime import datetime
from typing import Sequence

from .stats import DayRow, Summary, SOURCE_CREATOR, SOURCE_NONE, SOURCE_TICK
from .timeutil import get_tz

UP = "#16a34a"
DOWN = "#e11d48"
INK = "#0f172a"
MUTED = "#64748b"
LINE = "#e2e8f0"

SOURCE_LABEL = {SOURCE_CREATOR: "创作中心", SOURCE_TICK: "采样估算", SOURCE_NONE: "无数据"}


# ---------------------------------------------------------------- 文本工具


def disp_width(text: str) -> int:
    """中日韩字符占两格，用来在终端里对齐。"""
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text: str, width: int, align: str = "left", fill: str = " ") -> str:
    gap = max(0, width - disp_width(text))
    if align == "right":
        return fill * gap + text
    if align == "center":
        left = gap // 2
        return fill * left + text + fill * (gap - left)
    return text + fill * gap


def fmt_int(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def fmt_signed(value: int | None) -> str:
    if value is None:
        return "—"
    if value > 0:
        return f"+{value:,}"
    return f"{value:,}"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"


# ---------------------------------------------------------------- 终端表格


def render_text(account_label: str, rows: Sequence[DayRow], summary: Summary, tz_name: str) -> str:
    lines: list[str] = []
    span = f"{rows[0].day} ~ {rows[-1].day}" if rows else "无数据"
    title = f"{account_label}  ·  {span}  ·  {len(rows)} 天"
    width = 78
    lines.append("─" * width)
    lines.append(" " + title)
    lines.append("─" * width)

    if not rows or summary.exact_days + summary.est_days == 0:
        lines.append("")
        lines.append("  还没有任何数据。先执行：")
        lines.append("    bilifans poll --all                  # 采一次粉丝数快照")
        lines.append("    bilifans backfill --all              # 用 Cookie 拉近 30 天精确涨掉粉")
        lines.append("")
        lines.append("─" * width)
        return "\n".join(lines)

    header = (
        pad("日期", 12)
        + pad("总粉丝", 10, "right")
        + pad("净增", 9, "right")
        + pad("新增", 8, "right")
        + pad("掉粉", 8, "right")
        + pad("采样", 6, "right")
        + "  "
        + pad("来源", 8)
    )
    lines.append(header)
    lines.append("─" * width)

    for row in rows:
        est = "" if row.has_exact else "≈"
        mark_new = est if row.display_new is not None else ""
        mark_lost = est if row.display_lost is not None else ""
        cells = (
            pad(str(row.day), 12)
            + pad(fmt_int(row.close_fans), 10, "right")
            + pad(fmt_signed(row.net), 9, "right")
            + pad(mark_new + fmt_int(row.display_new), 8, "right")
            + pad(mark_lost + fmt_int(row.display_lost), 8, "right")
            + pad(str(row.samples) if row.samples else "—", 6, "right")
            + "  "
            + pad(SOURCE_LABEL.get(row.source, row.source), 8)
        )
        lines.append(cells)

    lines.append("─" * width)

    active = summary.exact_days + summary.est_days
    if active and (summary.new_total or summary.lost_total or summary.net_total):
        lines.append(
            pad("合计", 12)
            + pad("", 10, "right")
            + pad(fmt_signed(summary.net_total), 9, "right")
            + pad(fmt_int(summary.new_total), 8, "right")
            + pad(fmt_int(summary.lost_total), 8, "right")
        )
        lines.append(
            pad("日均", 12)
            + pad("", 10, "right")
            + pad(f"{summary.net_total / active:+.1f}", 9, "right")
            + pad(f"{summary.avg_new:.1f}", 8, "right")
            + pad(f"{summary.avg_lost:.1f}", 8, "right")
        )
    lines.append("")

    if summary.exact_days == 0 and sum(r.samples for r in rows) < 3:
        lines.append("  提示：采样点还太少（少于 3 个），净增和估算暂时没有意义，等定时任务多跑几轮。")

    if summary.best_new is not None and (summary.best_new.display_new or 0) > 0:
        lines.append(f"  单日涨粉最多   {summary.best_new.day}   +{summary.best_new.display_new:,}")
    if summary.worst_lost is not None and (summary.worst_lost.display_lost or 0) > 0:
        lines.append(f"  单日掉粉最多   {summary.worst_lost.day}   -{summary.worst_lost.display_lost:,}")
    if summary.last_fans is not None:
        lines.append(f"  当前总粉丝     {summary.last_fans:,}")
    if summary.churn_rate:
        lines.append(f"  日均掉粉率     {fmt_pct(summary.churn_rate)}（掉粉 / 粉丝规模）")
    lines.append(
        f"  数据完整度     {summary.exact_days}/{len(rows)} 天来自创作中心（精确），"
        f"{summary.est_days} 天为采样估算"
    )

    odd = [r for r in rows if r.note and r.note != "无数据"]
    if odd:
        lines.append("")
        lines.append("  备注：")
        for row in odd[:6]:
            lines.append(f"    {row.day}  {row.note}")
        if len(odd) > 6:
            lines.append(f"    …… 另有 {len(odd) - 6} 条")

    lines.append("─" * width)
    lines.append("  ≈ 表示由总粉丝数采样推算的下限，真实新增/掉粉只会更高；无 Cookie 时无法做到精确。")
    lines.append(f"  时区 {tz_name}，生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    return "\n".join(line.rstrip() for line in lines)


def _clip(text: str, width: int) -> str:
    """按显示宽度截断，超长补省略号。"""
    if disp_width(text) <= width:
        return text
    out, used = "", 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > width - 1:
            break
        out += ch
        used += w
    return out + "…"


# ---------------------------------------------------------------- 取关名单


def render_churn(
    account_label: str,
    rows: Sequence,
    summary: dict,
    *,
    start_day,
    end_day,
    tz_name: str,
    coverage_note: str = "",
) -> str:
    width = 78
    lines = ["─" * width]
    lines.append(f" {account_label} 的取关名单  ·  {start_day} ~ {end_day}")
    lines.append("─" * width)

    if not rows:
        lines.append("")
        lines.append("  这段时间没有检测到取关。")
        lines.append("  如果是刚开始用，需要至少两次「bilifans fans」抓取才能比对出结果。")
    else:
        lines.append(
            pad("昵称", 22)
            + pad("UID", 12, "right")
            + "  "
            + pad("关注时间", 12)
            + pad("存续", 8, "right")
            + "  "
            + pad("发现取关", 12)
        )
        lines.append("─" * width)
        for row in rows:
            tz = get_tz(tz_name)
            mtime = datetime.fromtimestamp(row.mtime, tz=tz).strftime("%Y-%m-%d") if row.mtime else "—"
            days = None
            if row.mtime and row.mtime > 0 and row.mtime <= row.gone_ts:
                days = max(0, (row.gone_ts - row.mtime) // 86400)
            gone = datetime.fromtimestamp(row.gone_ts, tz=tz).strftime("%Y-%m-%d")
            lines.append(
                pad(_clip(row.uname or "（已注销）", 22), 22)
                + pad(str(row.fan_uid), 12, "right")
                + "  "
                + pad(mtime, 12)
                + pad(f"{days} 天" if days is not None else "—", 8, "right")
                + "  "
                + pad(gone, 12)
            )
        lines.append("─" * width)
        lines.append(f"  共 {summary.get('count', len(rows))} 人在此期间取关")
        if summary.get("avg_survival") is not None:
            lines.append(
                f"  平均存续 {summary['avg_survival']:.1f} 天，"
                f"其中 {summary.get('quick_count', 0)} 人关注不到一周就走了"
            )

    if coverage_note:
        lines.append("")
        lines.append(f"  ⚠ {coverage_note}")
    lines.append("")
    lines.append("  说明：B 站不提供「谁取关了我」的接口，这里是靠两次粉丝名单快照比对得出的。")
    lines.append(f"  时区 {tz_name}，生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("─" * width)
    return "\n".join(line.rstrip() for line in lines)


# ---------------------------------------------------------------- 粉丝名单


def render_fan_list(
    account_label: str,
    rows: Sequence,
    totals: dict,
    *,
    total: int,
    page: int = 1,
    size: int = 50,
    tz_name: str = "Asia/Shanghai",
) -> str:
    width = 78
    tz = get_tz(tz_name)
    pages = max(1, (total + size - 1) // size)
    scope = f"全部 {totals.get('all', 0):,} 人（在关注 {totals.get('active', 0):,} · 已取关 {totals.get('gone', 0):,}）"
    lines = ["─" * width, f" {account_label} 的粉丝名单  ·  {scope}", "─" * width]

    if not rows:
        lines.append("")
        lines.append("  没有记录。先执行 bilifans fans（需要 Cookie）抓一次名单。")
        lines.append("─" * width)
        return "\n".join(lines)

    lines.append(
        pad("昵称", 22) + pad("UID", 12, "right") + "  " + pad("关注时间", 12)
        + pad("状态", 8) + "  " + pad("取关 / 最近", 12)
    )
    lines.append("─" * width)
    for row in rows:
        status = "互粉" if (row.active and row.attribute == 6) else ("在关注" if row.active else "已取关")
        when = row.gone_ts if row.gone_ts else row.last_seen_ts
        lines.append(
            pad(_clip(row.uname or "（已注销）", 22), 22)
            + pad(str(row.fan_uid), 12, "right")
            + "  "
            + pad(datetime.fromtimestamp(row.mtime, tz=tz).strftime("%Y-%m-%d") if row.mtime else "—", 12)
            + pad(status, 8)
            + "  "
            + pad(datetime.fromtimestamp(when, tz=tz).strftime("%Y-%m-%d"), 12)
        )
    lines.append("─" * width)
    lines.append(
        f"  第 {page}/{pages} 页，本页 {len(rows)} 条，共 {total:,} 条"
        + ("（用 --page 翻页，或看网页看板）" if pages > 1 else "")
    )
    lines.append("  核对：总条数和 B 站粉丝数对得上，就说明抓取成功。")
    lines.append("─" * width)
    return "\n".join(line.rstrip() for line in lines)


# ---------------------------------------------------------------- HTML 报告

CSS = """
:root{
  --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; --bg:#f7f8fa; --card:#fff;
  --up:#16a34a; --down:#e11d48; --accent:#2563eb;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1040px;margin:0 auto;padding:32px 20px 56px}
header{margin-bottom:20px}
h1{font-size:22px;margin:0 0 6px;letter-spacing:.01em}
.sub{color:var(--muted);font-size:13px;display:flex;flex-wrap:wrap;gap:6px 14px}
.sub b{color:var(--ink);font-weight:600}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:20px 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.card .k{color:var(--muted);font-size:12px;margin-bottom:6px}
.card .v{font-size:24px;font-weight:650;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.card .d{color:var(--muted);font-size:11px;margin-top:4px}
.up{color:var(--up)} .down{color:var(--down)}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:16px}
h2{font-size:14px;margin:0 0 2px;font-weight:650}
.hint{color:var(--muted);font-size:12px;margin:0 0 14px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:13px}
th{text-align:right;color:var(--muted);font-weight:500;font-size:12px;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
td{padding:7px 10px;text-align:right;border-bottom:1px solid #f1f5f9;white-space:nowrap}
tbody tr:hover{background:#fafbfc}
.tag{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;line-height:18px}
.tag.exact{background:#ecfdf5;color:#047857}
.tag.est{background:#fff7ed;color:#c2410c}
.tag.none{background:#f1f5f9;color:#64748b}
.est-num{color:#c2410c}
.note-row{color:var(--muted);font-size:12px;white-space:normal;text-align:left}
svg{display:block;width:100%;height:auto;overflow:visible}
.tick{fill:var(--muted);font-size:11px}
.grid{stroke:#eef2f6;stroke-width:1}
.axis{stroke:#cbd5e1;stroke-width:1}
.empty{padding:40px;text-align:center;color:var(--muted)}
footer{color:var(--muted);font-size:12px;line-height:1.9;padding:4px 4px 0}
footer b{color:var(--ink);font-weight:600}
@media print{body{background:#fff}section,.card{break-inside:avoid}}
"""


def _nice_max(value: float) -> float:
    if value <= 0:
        return 1.0
    exp = math.floor(math.log10(value))
    base = 10.0**exp
    for mult in (1, 2, 2.5, 5, 10):
        if value <= mult * base:
            return mult * base
    return 10 * base


def _nice_ticks(lo: float, hi: float, target: int = 4) -> list[float]:
    """给坐标轴生成整齐的刻度，比如 127500 / 128000 / 128500。"""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / target
    mag = 10.0 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    step = mag
    for mult in (1, 2, 2.5, 5, 10):
        step = mult * mag
        if raw <= step:
            break
    start = math.floor(lo / step) * step
    ticks: list[float] = []
    value = start
    while value <= hi + step * 0.5:
        ticks.append(round(value, 6))
        value += step
    return ticks or [lo, hi]


def _bar_chart(rows: Sequence[DayRow]) -> str:
    w, h = 960, 320
    left, right, top, bottom = 52, 14, 12, 54
    pw, ph = w - left - right, h - top - bottom
    y0 = top + ph / 2
    half = ph / 2 - 10

    peak = max(
        [max(r.display_new or 0, r.display_lost or 0) for r in rows] or [1]
    )
    top_val = _nice_max(peak)
    scale = half / top_val
    step = pw / max(1, len(rows))
    bw = max(2.5, min(20.0, step * 0.56))

    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="每日新增与掉粉">']
    for frac in (1, 0.5, 0, -0.5, -1):
        y = y0 - frac * half
        cls = "axis" if frac == 0 else "grid"
        out.append(f'<line class="{cls}" x1="{left}" y1="{y:.1f}" x2="{w - right}" y2="{y:.1f}"/>')
        out.append(
            f'<text class="tick" x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{top_val * frac:,.0f}</text>'
        )

    label_every = max(1, math.ceil(len(rows) / 9))
    for i, row in enumerate(rows):
        cx = left + step * (i + 0.5)
        new = row.display_new or 0
        lost = row.display_lost or 0
        est = "" if row.has_exact else "≈"
        opacity = "1" if row.has_exact else ".45"
        tip = f"{row.day}  新增 {est}{new:,} / 掉粉 {est}{lost:,}"
        out.append(f'<g><title>{html.escape(tip)}</title>')
        if new:
            bh = new * scale
            out.append(
                f'<rect x="{cx - bw / 2:.1f}" y="{y0 - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" '
                f'rx="2" fill="{UP}" fill-opacity="{opacity}"/>'
            )
        if lost:
            bh = lost * scale
            out.append(
                f'<rect x="{cx - bw / 2:.1f}" y="{y0:.1f}" width="{bw:.1f}" height="{bh:.1f}" '
                f'rx="2" fill="{DOWN}" fill-opacity="{opacity}"/>'
            )
        out.append("</g>")
        if i % label_every == 0:
            out.append(
                f'<text class="tick" x="{cx:.1f}" y="{h - bottom + 18}" text-anchor="middle">'
                f"{row.day.month}/{row.day.day}</text>"
            )

    out.append(
        f'<text class="tick" x="{left}" y="{h - 8}">实心 = 创作中心精确数据，半透明 = 采样估算（下限）</text>'
    )
    out.append("</svg>")
    return "".join(out)


def _line_chart(rows: Sequence[DayRow]) -> str:
    pts = [(i, r.close_fans) for i, r in enumerate(rows) if r.close_fans]
    if len(pts) < 2:
        return '<div class="empty">采样点不足，画不出粉丝趋势。多跑几次 poll 就有了。</div>'

    w, h = 960, 260
    left, right, top, bottom = 64, 18, 16, 36
    pw, ph = w - left - right, h - top - bottom
    values = [v for _, v in pts]
    if min(values) == max(values):
        ticks = [values[0] - 1, values[0], values[0] + 1]
    else:
        ticks = _nice_ticks(min(values) * 0.9995, max(values) * 1.0005, target=4)
        if len(ticks) < 3:  # 至少给上下各留一格，别让线贴着边框
            ticks = _nice_ticks(ticks[0] - 1, ticks[-1] + 1, target=4)
    lo, hi = ticks[0], ticks[-1]
    step = pw / max(1, len(rows) - 1)

    def x_at(index: int) -> float:
        return left + index * step

    def y_at(value: float) -> float:
        return top + ph - (value - lo) / (hi - lo) * ph

    coords = [(x_at(i), y_at(v)) for i, v in pts]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"{coords[0][0]:.1f},{top + ph:.1f} " + line + f" {coords[-1][0]:.1f},{top + ph:.1f}"

    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="粉丝总数趋势">']
    out.append(
        '<defs><linearGradient id="g1" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%" stop-color="#2563eb" stop-opacity=".22"/>'
        '<stop offset="100%" stop-color="#2563eb" stop-opacity="0"/></linearGradient></defs>'
    )
    for tick in ticks:
        y = y_at(tick)
        out.append(f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{w - right}" y2="{y:.1f}"/>')
        out.append(
            f'<text class="tick" x="{left - 10}" y="{y + 4:.1f}" text-anchor="end">{tick:,.0f}</text>'
        )
    out.append(f'<polygon points="{area}" fill="url(#g1)"/>')
    out.append(f'<polyline points="{line}" fill="none" stroke="#2563eb" stroke-width="2.2" '
               'stroke-linejoin="round" stroke-linecap="round"/>')
    lx, ly = coords[-1]
    out.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4" fill="#2563eb" stroke="#fff" stroke-width="2"/>')
    out.append(
        f'<text class="tick" x="{lx - 8:.1f}" y="{ly - 12:.1f}" text-anchor="end" '
        f'fill="{INK}" font-weight="600">{values[-1]:,}</text>'
    )
    label_every = max(1, math.ceil(len(rows) / 9))
    for i in range(0, len(rows), label_every):
        out.append(
            f'<text class="tick" x="{x_at(i):.1f}" y="{h - bottom + 18}" text-anchor="middle">'
            f"{rows[i].day.month}/{rows[i].day.day}</text>"
        )
    out.append("</svg>")
    return "".join(out)


def bar_chart_svg(rows: Sequence[DayRow]) -> str:
    """对外暴露的图表入口（网页看板复用同一套绘制代码）。"""
    return _bar_chart(rows)


def line_chart_svg(rows: Sequence[DayRow]) -> str:
    return _line_chart(rows)


def _table(rows: Sequence[DayRow]) -> str:
    body = []
    for row in rows:
        if row.source == SOURCE_NONE:
            body.append(
                f'<tr><td>{row.day}</td><td>—</td><td>—</td><td>—</td><td>—</td>'
                f'<td>0</td><td><span class="tag none">无数据</span></td></tr>'
            )
            continue
        cls = "tag exact" if row.has_exact else "tag est"
        num_cls = "" if row.has_exact else ' class="est-num"'
        mark_new = "" if (row.has_exact or row.display_new is None) else "≈"
        mark_lost = "" if (row.has_exact or row.display_lost is None) else "≈"
        net = row.net or 0
        net_cls = "up" if net > 0 else "down" if net < 0 else ""
        body.append(
            f"<tr><td>{row.day}</td>"
            f"<td>{fmt_int(row.close_fans)}</td>"
            f'<td class="{net_cls}">{fmt_signed(row.net)}</td>'
            f"<td{num_cls}>{mark_new}{fmt_int(row.display_new)}</td>"
            f"<td{num_cls}>{mark_lost}{fmt_int(row.display_lost)}</td>"
            f"<td>{row.samples}</td>"
            f'<td><span class="{cls}">{SOURCE_LABEL[row.source]}</span></td></tr>'
        )
    return (
        "<table><thead><tr>"
        "<th>日期</th><th>总粉丝</th><th>净增</th><th>新增</th><th>掉粉</th><th>采样点</th><th>数据源</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


def render_html(
    account_label: str,
    rows: Sequence[DayRow],
    summary: Summary,
    *,
    tz_name: str,
    cookie_ok: bool,
) -> str:
    active = summary.exact_days + summary.est_days
    span = f"{rows[0].day} ~ {rows[-1].day}" if rows else "无数据"
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    cards = [
        ("新增粉丝", f"{summary.new_total:,}", "up", f"日均 {summary.avg_new:.1f}"),
        ("流失粉丝", f"{summary.lost_total:,}", "down", f"日均 {summary.avg_lost:.1f}"),
        ("净增", fmt_signed(summary.net_total), "up" if summary.net_total >= 0 else "down", f"{active} 天累计"),
        ("日均掉粉率", fmt_pct(summary.churn_rate), "", "掉粉 / 粉丝规模"),
    ]
    card_html = "".join(
        f'<div class="card"><div class="k">{k}</div>'
        f'<div class="v {cls}">{v}</div><div class="d">{d}</div></div>'
        for k, v, cls, d in cards
    )

    if active == 0:
        chart = (
            '<div class="empty">还没有数据。先运行 <code>bilifans poll --all</code> 采集快照，'
            "或用 <code>bilifans backfill --all</code> 拉取近 30 天精确数据。</div>"
        )
    else:
        chart = (
            '<section><h2>每日新增 / 流失</h2>'
            '<p class="hint">上方绿色为新增，下方红色为流失；颜色越浅表示该天数据来自采样估算。</p>'
            f"{_bar_chart(rows)}</section>"
            '<section><h2>粉丝总数趋势</h2>'
            '<p class="hint">按每天最后一个采样点绘制，断档处直接连线。</p>'
            f"{_line_chart(rows)}</section>"
            '<section><h2>逐日明细</h2><p class="hint">净增 = 新增 − 流失；≈ 为采样推算的下限。</p>'
            f"{_table(rows)}</section>"
        )

    source_note = (
        "本次报告包含创作中心（官方口径）数据，新增 / 流失为精确值。"
        if cookie_ok
        else "未配置 Cookie，只能用采样估算，新增 / 流失均为下限。配置 BILI_COOKIE 后可获得精确值。"
    )

    parts = [
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{html.escape(account_label)} · 粉丝变化报告</title>",
        f"<style>{CSS}</style></head><body><div class=\"wrap\">",
        "<header><h1>粉丝变化报告</h1><div class=\"sub\">",
        f"<span><b>{html.escape(account_label)}</b></span>",
        f"<span>区间 <b>{span}</b></span>",
        f"<span>{len(rows)} 天</span>",
        f"<span>时区 {tz_name}</span>",
        f"<span>生成于 {generated}</span>",
        "</div></header>",
        f'<div class="cards">{card_html}</div>',
        chart,
        "<footer>",
        "<div><b>口径</b> 净增 = 当天最后采样 − 前一天最后采样；新增 / 流失以创作中心为准，"
        "无该数据时用相邻采样差值累计出的下限代替（≈）。</div>",
        f"<div><b>说明</b> {html.escape(source_note)}</div>",
        "<div>数据仅供个人复盘使用，请控制采集频率，勿用于绕过平台风控。</div>",
        "</footer></div></body></html>",
    ]
    return "".join(parts)


def write_html(path: str, content: str) -> str:
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return str(p)
