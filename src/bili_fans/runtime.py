"""运行期杂项：PID 文件、找到并停掉正在跑的看板服务。

App 每次打开都会带上 `--restart`，先在端口/进程标识上把旧服务停掉，
再启动新的。这样升级代码之后不会出现「窗口开了还是旧逻辑」。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path.home() / ".bilifans"


def data_dir() -> Path:
    """数据目录：放在系统规定的地方，不跟代码混在一起。

    macOS  -> ~/Library/Application Support/bilifans
    Linux  -> $XDG_DATA_HOME/bilifans（默认 ~/.local/share/bilifans）
    其他   -> ~/.bilifans
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / "bilifans"


def default_db_path() -> Path:
    return data_dir() / "bilifans.db"
PID_FILE = APP_DIR / "serve.pid"

# 只杀「看起来是我们自己的」进程：命令行里必须出现这个标记
MARKER = "bili_fans"


def read_pid_file() -> dict | None:
    try:
        data = json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_pid_file(port: int) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(
        json.dumps({"pid": os.getpid(), "port": int(port), "started": time.time()}),
        encoding="utf-8",
    )


def clear_pid_file() -> None:
    try:
        PID_FILE.unlink()
    except OSError:
        pass


def _is_zombie(pid: int) -> bool:
    """僵尸进程：已经被杀但父进程还没 wait，kill(0) 仍然成功。"""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip().upper().startswith("Z")
    except (OSError, subprocess.SubprocessError):
        return False


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:      # 存在但不属于我们
        return True
    return not _is_zombie(pid)


def cmdline(pid: int) -> str:
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def listener_pid(port: int) -> int | None:
    """谁在监听这个端口（拿不到 lsof 就返回 None）。"""
    try:
        out = subprocess.run(
            ["lsof", "-nP", "-ti", f"tcp:{int(port)}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for token in out.stdout.split():
        try:
            return int(token)
        except ValueError:
            continue
    return None


def _terminate(pid: int, timeout: float = 6.0) -> bool:
    """先 SIGTERM，等不到就 SIGKILL。返回是否真的结束了。"""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.15)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    time.sleep(0.2)
    return not pid_alive(pid)


def stop_running(port: int, *, timeout: float = 6.0) -> list[str]:
    """停掉正在跑的看板。返回给人看的说明，没东西可停就返回空列表。"""
    notes: list[str] = []
    targets: list[int] = []

    info = read_pid_file()
    if info and isinstance(info.get("pid"), int):
        targets.append(info["pid"])

    by_port = listener_pid(port)
    if by_port and by_port not in targets:
        targets.append(by_port)

    for pid in targets:
        if pid == os.getpid():
            continue
        if not pid_alive(pid):
            continue
        line = cmdline(pid)
        if MARKER not in line:
            notes.append(f"跳过 pid {pid}：它不是本程序的进程（{line[:60] or '未知'}）")
            continue
        if _terminate(pid, timeout):
            notes.append(f"已停止旧服务 pid {pid}")
        else:
            notes.append(f"停不掉 pid {pid}，请手动结束")

    clear_pid_file()
    return notes


def wait_port_free(port: int, *, probe, timeout: float = 6.0) -> bool:
    """等端口上的旧服务真正消失。probe() 返回 True 表示还活着。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not probe():
            return True
        time.sleep(0.2)
    return not probe()
