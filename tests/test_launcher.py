"""启动器脚本本身的行为：尤其是被软链到 PATH 上以后还能不能找到项目。"""

from __future__ import annotations

import subprocess
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT / "bin" / "bilifans"


def run(cmd):
    return subprocess.run([str(cmd), "--version"], capture_output=True, text=True, timeout=60)


def test_launcher_runs_from_project():
    result = run(LAUNCHER)
    assert result.returncode == 0, result.stderr
    assert "bilifans" in result.stdout


def test_launcher_survives_symlink(tmp_path):
    """`bilifans install` 会把它软链到 ~/.local/bin，此时 $0 不是真实路径。"""
    link = tmp_path / "bilifans"
    link.symlink_to(LAUNCHER)
    result = run(link)
    assert result.returncode == 0, result.stderr
    assert "bilifans" in result.stdout


def _launcher_env(tmp_path, broken_python3=True):
    """构造一个 PATH，头里放一个「存在但跑不了」的 python3。"""
    import os

    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir(exist_ok=True)
    if broken_python3:
        stub = fake_bin / "python3"
        stub.write_text("#!/bin/sh\nexit 1\n")
        stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env.pop("PYTHON", None)
    return env


def test_launcher_skips_unusable_python3(tmp_path):
    """系统 python3 是 3.9（没有 tomllib）时，要自动换一个能用的解释器。"""
    result = subprocess.run(
        [str(LAUNCHER), "--version"], capture_output=True, text=True,
        timeout=60, env=_launcher_env(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert "bilifans" in result.stdout


def test_launcher_explains_when_no_usable_python(tmp_path):
    """一个能用的都没有时，给一句人话，而不是抛 traceback。"""
    result = subprocess.run(
        [str(LAUNCHER), "--version"], capture_output=True, text=True,
        timeout=60, env=_launcher_env(tmp_path, broken_python3=True) | {"PATH": str(tmp_path / "fakebin")},
    )
    assert result.returncode == 1
    assert "3.11" in result.stderr
    assert "Traceback" not in result.stderr
