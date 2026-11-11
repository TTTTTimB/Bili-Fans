"""数据位置：默认存在系统应用数据目录，不跟仓库混在一起，重启后还在。"""

from __future__ import annotations

from pathlib import Path

from bili_fans import runtime
from bili_fans.cli import migrate_legacy_db
from bili_fans.config import Config
from bili_fans.db import connect, get_account, upsert_account


def test_default_db_lives_outside_the_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "data_dir", lambda: tmp_path / "appdata")
    path = Config().db_path()
    assert path == tmp_path / "appdata" / "bilifans.db"
    assert Path.cwd() not in path.parents


def test_explicit_db_wins(tmp_path):
    cfg = Config(db=str(tmp_path / "mine.db"))
    assert cfg.db_path() == tmp_path / "mine.db"


def test_migrate_legacy_db(monkeypatch, tmp_path):
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(runtime, "data_dir", lambda: tmp_path / "appdata")

    (work / "bilifans.db").write_bytes(b"legacy-data")
    (work / "bilifans.db-wal").write_bytes(b"legacy-wal")

    notes = []
    moved = migrate_legacy_db(Config(), say=notes.append)

    target = tmp_path / "appdata" / "bilifans.db"
    assert moved == str(target)
    assert target.read_bytes() == b"legacy-data"
    assert (tmp_path / "appdata" / "bilifans.db-wal").read_bytes() == b"legacy-wal"
    assert not (work / "bilifans.db").exists()      # 项目目录里不再留数据
    assert notes


def test_migrate_skips_when_path_configured(monkeypatch, tmp_path):
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    (work / "bilifans.db").write_bytes(b"legacy")
    assert migrate_legacy_db(Config(db="custom.db")) is None
    assert (work / "bilifans.db").exists()          # 用户指定了就别动


def test_data_survives_restart(tmp_path):
    """服务重启后数据还在（落盘，不是内存）。"""
    path = tmp_path / "bilifans.db"
    conn = connect(path)
    upsert_account(conn, 42, "演示账号")
    conn.close()

    again = connect(path)
    try:
        assert get_account(again, 42).name == "演示账号"
    finally:
        again.close()
