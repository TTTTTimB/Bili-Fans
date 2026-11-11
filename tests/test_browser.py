"""自动取 Cookie 的测试：不启动真浏览器，用假 CDP 客户端验证逻辑。"""

from __future__ import annotations

import stat

import pytest

from bili_fans import browser as br


def test_cookie_header_filters_domains_and_orders_keys():
    cookies = [
        {"name": "buvid3", "value": "b", "domain": ".bilibili.com"},
        {"name": "SESSDATA", "value": "s", "domain": ".bilibili.com"},
        {"name": "evil", "value": "x", "domain": ".evil.com"},
        {"name": "", "value": "y", "domain": ".bilibili.com"},
        {"name": "DedeUserID", "value": "42", "domain": "www.bilibili.com"},
    ]
    header, values = br._cookie_header(cookies)
    assert header.startswith("SESSDATA=s;")      # 关键字段排最前
    assert "evil" not in header
    assert "" not in values
    assert values["DedeUserID"] == "42"


def test_masked_never_leaks_full_session():
    result = br.CookieResult(
        cookie="SESSDATA=abcdef1234567890; bili_jct=zzzz; buvid3=q; DedeUserID=42",
        values={"SESSDATA": "abcdef1234567890", "bili_jct": "zzzz", "buvid3": "q", "DedeUserID": "42"},
        logged_in=True,
    )
    masked = result.masked
    assert "abcdef1234567890" not in masked
    assert "abcdef…7890" in masked
    assert "另有 1 个字段" in masked
    assert result.uid == "42"
    assert "DedeUserID" in result.names


def test_save_cookie_writes_0600(tmp_path):
    path = tmp_path / "cookie.txt"
    br.save_cookie(path, "SESSDATA=abc; bili_jct=def\n")
    assert br.load_cookie(path) == "SESSDATA=abc; bili_jct=def"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_load_cookie_missing_or_broken(tmp_path):
    assert br.load_cookie(tmp_path / "nope") == ""
    broken = tmp_path / "bin"
    broken.write_bytes(b"\xff\xfe\x00")
    assert br.load_cookie(broken) == ""


def test_find_chrome_prefers_env_override(monkeypatch, tmp_path):
    fake = tmp_path / "chrome"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("BILIFANS_CHROME", str(fake))
    assert br.find_chrome() == str(fake)

    monkeypatch.setenv("BILIFANS_CHROME", str(tmp_path / "missing"))
    assert br.find_chrome() is None


class FakeCDP:
    """依次吐出：未登录 → 未登录 → 已登录。"""

    instances: list["FakeCDP"] = []

    def __init__(self, chrome, profile_dir, *, url="", headless=False, timeout=30):
        self.polls = 0
        self.closed = False
        FakeCDP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def attach_page(self):
        return "session"

    def navigate(self, url):
        self.url = url

    def wait_ready(self, timeout=25.0):
        return True

    def cookies(self):
        self.polls += 1
        base = [{"name": "buvid3", "value": "b3", "domain": ".bilibili.com"}]
        if self.polls < 3:
            return base
        return base + [{"name": "SESSDATA", "value": "secret-session", "domain": ".bilibili.com"}]


def test_capture_waits_until_login_appears():
    FakeCDP.instances = []
    result = br.capture_cookie(
        "/tmp/fake-profile",
        chrome="/fake/chrome",
        timeout=5,
        poll=0,
        client_factory=FakeCDP,
    )
    assert result.logged_in is True
    assert result.values["SESSDATA"] == "secret-session"
    assert result.values["buvid3"] == "b3"
    assert FakeCDP.instances[0].polls == 3
    assert FakeCDP.instances[0].closed is True       # 用完要关掉浏览器
    assert FakeCDP.instances[0].url == br.DEFAULT_URL
    assert "passport.bilibili.com" in FakeCDP.instances[0].url


class NeverLogin(FakeCDP):
    def cookies(self):
        self.polls += 1
        return [{"name": "buvid3", "value": "b3", "domain": ".bilibili.com"}]


def test_capture_times_out_with_hint():
    with pytest.raises(br.BrowserError) as info:
        br.capture_cookie(
            "/tmp/fake-profile", chrome="/fake/chrome", timeout=0.05, poll=0.01, client_factory=NeverLogin
        )
    assert "还没登录成功" in str(info.value)


def test_capture_reports_missing_browser(monkeypatch):
    monkeypatch.setattr(br, "find_chrome", lambda: None)
    with pytest.raises(br.BrowserError) as info:
        br.capture_cookie("/tmp/fake-profile", client_factory=FakeCDP)
    assert "没找到" in str(info.value)


def test_on_wait_callback_is_invoked():
    seen = []
    br.capture_cookie(
        "/tmp/fake-profile",
        chrome="/fake/chrome",
        timeout=5,
        poll=0,
        client_factory=FakeCDP,
        on_wait=lambda left: seen.append(left),
    )
    assert seen and seen[0] > 0


def test_resolve_cookie_priority(tmp_path, monkeypatch):
    from bili_fans import browser as b
    from bili_fans.config import Config
    from bili_fans.service import resolve_cookie

    cookie_file = tmp_path / "cookie"
    monkeypatch.setattr(b, "COOKIE_FILE", cookie_file)
    monkeypatch.delenv("BILI_COOKIE", raising=False)
    cfg = Config()

    assert resolve_cookie(cfg) == ("", "")

    b.save_cookie(cookie_file, "SESSDATA=fromfile")
    assert resolve_cookie(cfg)[0] == "SESSDATA=fromfile"

    monkeypatch.setenv("BILI_COOKIE", "SESSDATA=fromenv")
    assert resolve_cookie(cfg) == ("SESSDATA=fromenv", "环境变量 BILI_COOKIE")

    assert resolve_cookie(cfg, "SESSDATA=fromcli") == ("SESSDATA=fromcli", "--cookie")
