"""输出层测试：中文宽度对齐、HTML 自包含、估算标记。"""

from __future__ import annotations

from datetime import date

from bili_fans.db import Snapshot
from bili_fans.report import (
    disp_width,
    fmt_signed,
    pad,
    render_html,
    render_text,
)
from bili_fans.stats import build_rows, summarize
from bili_fans.timeutil import day_start_ts, get_tz

TZ = get_tz("Asia/Shanghai")


def sample_rows():
    d1, d2, d3 = date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)
    snaps = [
        Snapshot(day_start_ts(d1, TZ) - 3600, 10_000),
        Snapshot(day_start_ts(d1, TZ) + 3600, 10_020),
        Snapshot(day_start_ts(d2, TZ) + 3600, 10_050),
        Snapshot(day_start_ts(d3, TZ) + 3600, 10_041),
    ]
    creator = {"2026-11-01": (25, 5), "2026-11-02": (35, 5)}
    return build_rows(snaps, creator, d1, d3, TZ)


def test_disp_width_counts_cjk_as_two():
    assert disp_width("ab") == 2
    assert disp_width("中文") == 4
    assert disp_width("a中") == 3


def test_pad_aligns_visually():
    assert disp_width(pad("中", 6)) == 6
    assert disp_width(pad("中", 6, "right")) == 6
    assert pad("汉", 4) == "汉  "
    assert pad("5", 3, "right") == "  5"


def test_fmt_signed():
    assert fmt_signed(0) == "0"
    assert fmt_signed(1234) == "+1,234"
    assert fmt_signed(-12) == "-12"
    assert fmt_signed(None) == "—"


def test_text_report_marks_estimates_and_sources():
    rows = sample_rows()
    text = render_text("测试号（UID 1）", rows, summarize(rows), "Asia/Shanghai")
    assert "创作中心" in text
    assert "采样估算" in text
    assert "≈" in text            # 11-03 只有采样
    assert "+30" in text          # 11-02 新增 35 掉 5 = 净增 30
    assert "数据完整度" in text
    # 对齐：每一行数据的列宽必须完全一致（表头去掉尾部空格后会略窄）
    data_lines = [line for line in text.splitlines() if line.startswith("2026-")]
    assert len(data_lines) == 3
    assert {disp_width(line) for line in data_lines} == {63}


def test_text_report_handles_no_data():
    rows = build_rows([], {}, date(2026, 11, 1), date(2026, 11, 2), TZ)
    text = render_text("空号（UID 2）", rows, summarize(rows), "Asia/Shanghai")
    assert "还没有任何数据" in text
    assert "bilifans poll" in text


def test_html_report_is_self_contained():
    rows = sample_rows()
    html = render_html("测试号（UID 1）", rows, summarize(rows), tz_name="Asia/Shanghai", cookie_ok=True)
    assert html.startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")
    assert "<svg" in html
    assert "http://" not in html and "https://" not in html  # 不引用外部资源
    assert "nan" not in html.lower()
    assert "创作中心" in html and "采样估算" in html


def test_html_report_empty_state():
    rows = build_rows([], {}, date(2026, 11, 1), date(2026, 11, 2), TZ)
    html = render_html("空号（UID 2）", rows, summarize(rows), tz_name="Asia/Shanghai", cookie_ok=False)
    assert "还没有数据" in html
    assert "未配置 Cookie" in html
