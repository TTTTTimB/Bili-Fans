"""生成「一键启动」的快捷方式：macOS .app + 全局命令。

生成出来的东西只是壳，真正的逻辑还在 bilifans 里：

- `Bili掉粉分析.app`：双击或放进 Dock，启动 serve 并自动打开浏览器
- `~/.local/bin/bilifans`：软链到项目里的启动器，敲 `bilifans serve` 就行

壳里写死了当前解释器的绝对路径。因为从 Finder 启动的 app 只拿到
`/usr/bin:/bin:/usr/sbin:/sbin`，找不到 conda/homebrew 里的 python3。
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "Bili掉粉分析"
BUNDLE_ID = "local.bilifans.dashboard"

INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>{name}</string>
    <key>CFBundleDisplayName</key><string>{name}</string>
    <key>CFBundleIdentifier</key><string>{bundle_id}</string>
    <key>CFBundleVersion</key><string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>launch</string>
    <key>CFBundleIconFile</key><string>icon.icns</string>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
</dict>
</plist>
"""

LAUNCHER = """#!/bin/sh
# 由 `bilifans install` 生成；删掉这个 app 不影响任何数据
cd "{project}" || exit 1
export PYTHONPATH="{project}/src"
LOG="$HOME/.bilifans/app.log"
mkdir -p "$HOME/.bilifans"

"{python}" -m bili_fans serve --port {port} --restart >>"$LOG" 2>&1
code=$?

# 双击启动失败时给个弹窗，不然 Finder 里会「闪一下就没了」。
# 130/143 是 Ctrl+C 和被 kill 掉，属于正常收工，别当成错误吓人。
if [ $code -ne 0 ] && [ $code -ne 130 ] && [ $code -ne 143 ]; then
    osascript -e 'display dialog "看板启动失败，详情见 ~/.bilifans/app.log" buttons {{"好"}} default button 1 with title "Bili掉粉分析"' >/dev/null 2>&1
fi
exit $code
"""


@dataclass
class InstallResult:
    app_path: Path | None = None
    cli_path: Path | None = None
    on_path: bool = False
    path_hint: str = ""
    notes: list[str] = field(default_factory=list)


def project_root() -> Path:
    """src/bili_fans/shortcut.py -> 项目根目录"""
    return Path(__file__).resolve().parents[2]


def build_app(
    dest: Path,
    *,
    project: Path | None = None,
    name: str = APP_NAME,
    port: int = 8787,
    python: str | None = None,
) -> Path:
    """在 dest 下生成 <name>.app，返回 bundle 路径。"""
    project = Path(project or project_root()).resolve()
    python = python or sys.executable or "python3"

    bundle = Path(dest).expanduser() / f"{name}.app"
    macos = bundle / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)

    (bundle / "Contents" / "Info.plist").write_text(
        INFO_PLIST.format(name=name, bundle_id=BUNDLE_ID), encoding="utf-8"
    )
    icon = project / "assets" / "icon.icns"
    if icon.exists():
        resources = bundle / "Contents" / "Resources"
        resources.mkdir(parents=True, exist_ok=True)
        shutil.copy2(icon, resources / "icon.icns")

    launcher = macos / "launch"
    launcher.write_text(
        LAUNCHER.format(project=project, python=python, port=int(port)), encoding="utf-8"
    )
    os.chmod(launcher, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    return bundle


def cli_cache_dir() -> Path:
    return Path.home() / ".local" / "bin"


def install_cli(project: Path | None = None, *, dest_dir: Path | None = None) -> tuple[Path, bool]:
    """把启动器软链到 ~/.local/bin/bilifans，返回 (路径, 是否已在 PATH 上)。"""
    project = Path(project or project_root()).resolve()
    target_dir = Path(dest_dir) if dest_dir else cli_cache_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    link = target_dir / "bilifans"
    source = project / "bin" / "bilifans"

    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(source)
    os.chmod(source, 0o755)

    paths = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    on_path = str(target_dir.resolve()) in {str(Path(p).expanduser().resolve()) for p in paths}
    return link, on_path


def install(
    *,
    app_dir: Path | None = None,
    with_app: bool = True,
    with_cli: bool = True,
    project: Path | None = None,
    port: int = 8787,
    cli_dir: Path | None = None,
) -> InstallResult:
    project = Path(project or project_root()).resolve()
    result = InstallResult()

    if with_app:
        dest = Path(app_dir).expanduser() if app_dir else Path.home() / "Applications"
        dest.mkdir(parents=True, exist_ok=True)
        result.app_path = build_app(dest, project=project, port=port)

    if with_cli:
        link, on_path = install_cli(project, dest_dir=cli_dir)
        result.cli_path = link
        result.on_path = on_path
        if not on_path:
            result.path_hint = 'export PATH="$HOME/.local/bin:$PATH"'

    return result
