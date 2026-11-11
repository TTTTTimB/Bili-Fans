"""一键启动的「壳」测试：生成 .app、装软链。不启动真服务。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bili_fans.shortcut import APP_NAME, build_app, install, install_cli


def make_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "bin").mkdir(parents=True)
    launcher = project / "bin" / "bilifans"
    launcher.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(launcher, 0o755)
    return project


def test_build_app_creates_valid_bundle(tmp_path):
    project = make_project(tmp_path)
    bundle = build_app(tmp_path / "apps", project=project, python="/usr/bin/python3", port=9123)

    assert bundle.name == f"{APP_NAME}.app"
    plist = bundle / "Contents" / "Info.plist"
    launcher = bundle / "Contents" / "MacOS" / "launch"
    assert plist.exists() and launcher.exists()

    text = launcher.read_text()
    assert str(project) in text                     # 绝对路径，Finder 里也能 cd 对
    assert "/usr/bin/python3" in text               # 写死解释器，绕开 Finder 的极简 PATH
    assert "--port 9123" in text
    assert "display dialog" in text                 # 失败时弹窗，不是静默失败
    assert os.access(launcher, os.X_OK)

    plist_text = plist.read_text()
    assert "<key>CFBundleExecutable</key><string>launch</string>" in plist_text
    assert "local.bilifans.dashboard" in plist_text


@pytest.mark.skipif(not shutil.which("plutil"), reason="plutil 只在 macOS 上有")
def test_info_plist_is_valid(tmp_path):
    bundle = build_app(tmp_path, project=make_project(tmp_path), python="/usr/bin/python3")
    result = subprocess.run(
        ["plutil", "-lint", str(bundle / "Contents" / "Info.plist")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_install_cli_creates_symlink(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    link, on_path = install_cli(project, dest_dir=tmp_path / "localbin")

    assert link.is_symlink()
    assert link.resolve() == (project / "bin" / "bilifans").resolve()
    assert on_path is False


def test_install_cli_detects_path(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    target = tmp_path / "localbin"
    monkeypatch.setenv("PATH", f"{target}:/usr/bin:/bin")
    _, on_path = install_cli(project, dest_dir=target)
    assert on_path is True


def test_install_can_skip_pieces(tmp_path):
    project = make_project(tmp_path)
    only_app = install(app_dir=tmp_path / "apps", with_app=True, with_cli=False, project=project)
    assert only_app.app_path is not None and only_app.cli_path is None

    only_cli = install(with_app=False, with_cli=True, project=project, cli_dir=tmp_path / "lb")
    assert only_cli.app_path is None
    assert only_cli.cli_path is not None
    assert only_cli.path_hint  # 没进 PATH 时要给出提示


def test_build_app_embeds_icon(tmp_path):
    project = make_project(tmp_path)
    (project / "assets").mkdir()
    fake = b"fake-icns-bytes"
    (project / "assets" / "icon.icns").write_bytes(fake)

    bundle = build_app(tmp_path / "apps", project=project, python="/usr/bin/python3")
    assert (bundle / "Contents" / "Resources" / "icon.icns").read_bytes() == fake
    assert "CFBundleIconFile" in (bundle / "Contents" / "Info.plist").read_text()
    # 图标必须在 Info.plist 指向的位置
    assert '<key>CFBundleIconFile</key><string>icon.icns</string>' in (
        bundle / "Contents" / "Info.plist"
    ).read_text()


def test_build_app_without_icon_still_works(tmp_path):
    project = make_project(tmp_path)     # 没有 assets/
    bundle = build_app(tmp_path / "apps", project=project, python="/usr/bin/python3")
    assert bundle.exists()
    assert not (bundle / "Contents" / "Resources" / "icon.icns").exists()


def test_launcher_does_not_popup_on_sigterm(tmp_path):
    """用户 kill 掉服务时不该弹「启动失败」。"""
    project = make_project(tmp_path)
    bundle = build_app(tmp_path / "apps", project=project, python="/usr/bin/python3")
    script = (bundle / "Contents" / "MacOS" / "launch").read_text()
    assert "[ $code -ne 130 ]" in script and "[ $code -ne 143 ]" in script
