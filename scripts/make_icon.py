"""生成应用图标（只在开发时跑，产物提交到 assets/）。

想法：深色圆角底 + 一条中轴，绿色柱子向上、红色柱子向下 ——
正好是看板里「新增 / 掉粉」那张图的样子，16px 下也能认出来。

用法：
    python3 scripts/make_icon.py
产物：
    assets/icon.png    1024×1024，给网页 favicon 和文档用
    assets/icon.icns   macOS 应用图标
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SIZE = 1024
S = 4                      # 超采样倍数，边缘更干净
CANVAS = SIZE * S

BG_TOP = (36, 48, 74)
BG_BOTTOM = (13, 20, 34)
GREEN = (52, 211, 153, 255)
RED = (244, 63, 94, 255)
AXIS = (255, 255, 255, 105)

MARGIN = 96                # 圆角方块四周留白
RADIUS = 186               # macOS 图标圆角 ≈ 0.2237 × 832


def _px(v: float) -> int:
    return int(round(v * S))


def _gradient() -> Image.Image:
    column = Image.new("RGB", (1, CANVAS))
    for y in range(CANVAS):
        t = y / max(1, CANVAS - 1)
        column.putpixel(
            (0, y),
            tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)),
        )
    return column.resize((CANVAS, CANVAS))


def _squircle_mask() -> Image.Image:
    mask = Image.new("L", (CANVAS, CANVAS), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [_px(MARGIN), _px(MARGIN), _px(SIZE - MARGIN), _px(SIZE - MARGIN)],
        radius=_px(RADIUS),
        fill=255,
    )
    return mask


def _bar(
    canvas: Image.Image,
    *,
    x0: float,
    x1: float,
    axis_y: float,
    height: float,
    up: bool,
    color: tuple[int, int, int],
    radius: float,
) -> None:
    """一根柱子：远端圆角、靠中轴那头切平，颜色由轴向外渐亮。"""
    w, h = _px(x1 - x0), _px(height)
    r = _px(radius)

    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    if up:
        md.rectangle([0, h - r, w - 1, h], fill=255)      # 底边压平
    else:
        md.rectangle([0, 0, w - 1, r], fill=255)          # 顶边压平

    light = tuple(min(255, c + 34) for c in color)
    dark = color
    top_color, bottom_color = (light, dark) if up else (dark, light)
    grad = Image.linear_gradient("L").resize((w, h), Image.BILINEAR)
    bar = Image.composite(
        Image.new("RGBA", (w, h), (*top_color, 255)),
        Image.new("RGBA", (w, h), (*bottom_color, 255)),
        grad,
    )
    bar.putalpha(mask)
    canvas.alpha_composite(bar, (_px(x0), _px(axis_y - height if up else axis_y)))


def build_master() -> Image.Image:
    mask = _squircle_mask()
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    img.paste(_gradient(), (0, 0), mask)

    axis_y = 536          # 略低于正中，因为向上的柱子比向下的长

    # 中轴
    ImageDraw.Draw(img).rounded_rectangle(
        [_px(178), _px(axis_y - 5), _px(SIZE - 178), _px(axis_y + 5)],
        radius=_px(5),
        fill=AXIS,
    )
    # 三根柱子：两根涨、一根掉，像看板里那张图
    _bar(img, x0=262, x1=402, axis_y=axis_y, height=214, up=True, color=(34, 197, 94), radius=34)
    _bar(img, x0=442, x1=582, axis_y=axis_y, height=340, up=True, color=(34, 197, 94), radius=34)
    _bar(img, x0=622, x1=762, axis_y=axis_y, height=262, up=False, color=(225, 29, 72), radius=34)

    # 一圈很淡的内描边，浅色背景上也有轮廓
    ImageDraw.Draw(img).rounded_rectangle(
        [_px(MARGIN), _px(MARGIN), _px(SIZE - MARGIN), _px(SIZE - MARGIN)],
        radius=_px(RADIUS),
        outline=(255, 255, 255, 30),
        width=_px(3),
    )
    return img.resize((SIZE, SIZE), Image.LANCZOS)


ICONSET = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    master = build_master()
    master.save(ASSETS / "icon.png", "PNG")
    print(f"✓ {ASSETS / 'icon.png'}")

    iconset = ASSETS / "icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()
    for name, px in ICONSET:
        master.resize((px, px), Image.LANCZOS).save(iconset / name, "PNG")

    icns = ASSETS / "icon.icns"
    if shutil.which("iconutil"):
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
        print(f"✓ {icns}  ({icns.stat().st_size // 1024} KB)")
        shutil.rmtree(iconset)
    else:
        print("iconutil 不可用（非 macOS），只生成了 PNG", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
