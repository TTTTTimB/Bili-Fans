"""配置加载：toml 文件 → Config，命令行参数优先于文件。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .api import CREATOR_STAT_URL
from .timeutil import DEFAULT_TZ

CONFIG_NAME = "bilifans.toml"
USER_CONFIG = Path.home() / ".config" / "bilifans" / "config.toml"


@dataclass
class Config:
    # 空字符串 = 用系统应用数据目录里的默认库（不放项目目录里）
    db: str = ""
    timezone: str = DEFAULT_TZ
    timeout: float = 15.0
    retries: int = 3
    interval_minutes: int = 5
    creator_url: str = CREATOR_STAT_URL
    cookie_env: str = "BILI_COOKIE"
    default_days: int = 30
    source_path: Path | None = field(default=None, repr=False)

    def cookie_from_env(self) -> str:
        return os.environ.get(self.cookie_env, "").strip()

    def db_path(self) -> Path:
        if not self.db:
            from .runtime import default_db_path

            return default_db_path()
        p = Path(self.db).expanduser()
        if not p.is_absolute():
            base = self.source_path.parent if self.source_path else Path.cwd()
            p = base / p
        return p


def find_config(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"找不到配置文件：{path}")
        return path
    for candidate in (Path.cwd() / CONFIG_NAME, USER_CONFIG):
        if candidate.exists():
            return candidate
    return None


def load_config(path: str | None = None) -> Config:
    found = find_config(path)
    cfg = Config()
    if not found:
        return cfg

    with found.open("rb") as fh:
        raw = tomllib.load(fh)
    if not isinstance(raw, dict):
        raise ValueError("配置文件格式错误：根对象必须是表")

    cfg.source_path = found
    cfg.db = str(raw.get("db", cfg.db))
    cfg.timezone = str(raw.get("timezone", cfg.timezone))

    req = raw.get("request") or {}
    cfg.timeout = float(req.get("timeout", cfg.timeout))
    cfg.retries = int(req.get("retries", cfg.retries))
    cfg.interval_minutes = max(1, int(req.get("interval_minutes", cfg.interval_minutes)))

    creator = raw.get("creator") or {}
    cfg.creator_url = str(creator.get("url", cfg.creator_url))
    cfg.cookie_env = str(creator.get("cookie_env", cfg.cookie_env))

    report = raw.get("report") or {}
    cfg.default_days = max(1, int(report.get("default_days", cfg.default_days)))
    return cfg
