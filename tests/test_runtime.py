"""App 每次打开都要重启服务：PID 文件的读写、旧进程的识别与停止。"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from bili_fans import runtime


@pytest.fixture()
def pid_file(tmp_path, monkeypatch):
    path = tmp_path / "serve.pid"
    monkeypatch.setattr(runtime, "PID_FILE", path)
    monkeypatch.setattr(runtime, "APP_DIR", tmp_path)
    return path


def test_pid_file_roundtrip(pid_file):
    runtime.write_pid_file(8787)
    info = runtime.read_pid_file()
    assert info["pid"] == os.getpid()
    assert info["port"] == 8787
    runtime.clear_pid_file()
    assert runtime.read_pid_file() is None


def test_read_pid_file_tolerates_garbage(pid_file):
    pid_file.write_text("not json")
    assert runtime.read_pid_file() is None
    pid_file.unlink()


def test_pid_alive():
    assert runtime.pid_alive(os.getpid()) is True
    assert runtime.pid_alive(0) is False
    assert runtime.pid_alive(999_999) is False


def test_stop_running_ignores_foreign_process(pid_file):
    """别人的进程绝对不能碰。"""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)", "not_ours"])
    try:
        pid_file.write_text(f'{{"pid": {proc.pid}, "port": 1}}')
        notes = runtime.stop_running(1, timeout=0.5)
        assert any("跳过" in n for n in notes)
        assert proc.poll() is None          # 还活着
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_stop_running_kills_own_server(pid_file):
    """命令行里带 bili_fans 的，才认定是我们自己的服务。"""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "bili_fans_fake_serve"]
    )
    try:
        time.sleep(0.3)
        pid_file.write_text(f'{{"pid": {proc.pid}, "port": 1}}')
        notes = runtime.stop_running(1, timeout=5)
        assert any("已停止旧服务" in n for n in notes)
        proc.wait(timeout=5)                # 真的退出了
        assert runtime.read_pid_file() is None
    finally:
        if proc.poll() is None:
            proc.kill()


def test_wait_port_free(pid_file):
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        return calls["n"] < 3              # 前两次还活着

    assert runtime.wait_port_free(1, probe=probe, timeout=5) is True


def test_app_launcher_restarts(monkeypatch, tmp_path):
    """生成的 .app 启动脚本必须带 --restart。"""
    from bili_fans.shortcut import build_app

    project = tmp_path / "proj"
    (project / "bin").mkdir(parents=True)
    (project / "bin" / "bilifans").write_text("#!/bin/sh\n")
    bundle = build_app(tmp_path / "apps", project=project, python="/usr/bin/python3", port=9123)
    script = (bundle / "Contents" / "MacOS" / "launch").read_text()
    assert "serve --port 9123 --restart" in script
