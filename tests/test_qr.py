"""QR 编码器测试。

金标准矩阵是用 B 站真实登录链接生成的，并且和第三方实现（qrcode 库）
逐位比对过、也用 macOS Vision 实际扫出来过。这里只负责防回归。
"""

from __future__ import annotations

import hashlib

import pytest

from bili_fans.qr import encode, to_svg

URL = (
    "https://account.bilibili.com/h5/account-h5/auth/scan-web?navhide=1"
    "&callback=close&qrcode_key=fbb46633f83d9b95859ad0f15136e1a6&from="
)
GOLDEN = "bb842caf8ab319351ba681774887c9db66bc75176434a39054a4556994618535"


def digest(modules) -> str:
    flat = "".join("".join(map(str, row)) for row in modules)
    return hashlib.sha256(flat.encode()).hexdigest()


def test_bilibili_login_url_matches_verified_matrix():
    qr = encode(URL)
    assert (qr.version, qr.size) == (8, 49)
    assert digest(qr.modules) == GOLDEN


def test_size_follows_version():
    for text, version in (("hi", 1), ("a" * 20, 2), ("a" * 100, 6), ("a" * 130, 8)):
        qr = encode(text)
        assert qr.version == version
        assert qr.size == version * 4 + 17


def test_finder_patterns_in_three_corners():
    qr = encode(URL)
    m, n = qr.modules, qr.size
    for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):
        assert m[r0][c0] == 1
        assert m[r0 + 1][c0 + 1] == 0
        assert m[r0 + 3][c0 + 3] == 1
        assert all(m[r0][c0 + i] == 1 for i in range(7))
        assert all(m[r0 + i][c0] == 1 for i in range(7))


def test_timing_pattern_alternates():
    qr = encode(URL)
    n = qr.size
    for i in range(8, n - 8):
        assert qr.modules[6][i] == (1 if i % 2 == 0 else 0)
        assert qr.modules[i][6] == (1 if i % 2 == 0 else 0)


def test_dark_module_is_set():
    qr = encode(URL)
    assert qr.modules[qr.size - 8][8] == 1


def test_too_long_text_raises():
    with pytest.raises(ValueError):
        encode("a" * 400)


def test_svg_is_self_contained_square():
    svg = to_svg(encode(URL), scale=6)
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    assert 'viewBox="0 0 342 342"' in svg
    assert svg.count("http://") == 1          # 只有 svg 命名空间那一个


def test_ascii_uses_half_blocks():
    art = encode(URL).ascii()
    lines = art.splitlines()
    assert len(lines) == (49 + 1) // 2 + 4      # 每行两个模块 + 上下留白
    assert set("".join(lines)) <= set("█▀▄ ")
