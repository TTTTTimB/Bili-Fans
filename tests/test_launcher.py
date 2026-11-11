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
