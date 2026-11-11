"""接口解析测试：全部用假 Transport，不碰网络。"""

from __future__ import annotations

import pytest

from bili_fans.api import ApiError, BiliClient, normalize_cookie
from bili_fans.http import HttpError, Transport


class StubTransport:
    """按顺序吐出预置响应，同时记下请求过的 URL。"""

    cookie = ""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls: list[tuple[str, object]] = []

    def get_json(self, url, params=None, referer=""):
        self.calls.append((url, params))
        item = self.payloads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(payloads, cookie=""):
    stub = StubTransport(payloads)
    stub.cookie = cookie
    return BiliClient(stub, cookie=cookie), stub


def test_stat_reads_follower_field():
    client, stub = make_client([{"code": 0, "data": {"follower": 1437848, "following": 430}}])
    stat = client.stat(2)
    assert stat.fans == 1437848
    assert stat.following == 430
    assert stub.calls[0][0].endswith("/x/relation/stat")


def test_stat_accepts_fans_alias():
    client, _ = make_client([{"code": 0, "data": {"fans": "1,024"}}])
    assert client.stat(1).fans == 1024


def test_stat_raises_on_api_error():
    client, _ = make_client([{"code": -404, "message": "啥都木有"}])
    with pytest.raises(ApiError) as info:
        client.stat(999)
    assert info.value.code == -404


def test_profile_tolerates_missing_name():
    client, _ = make_client([{"code": 0, "data": {"card": {}}}])
    assert client.profile(1) is None


def test_profile_parses_name_and_sign():
    payload = {"code": 0, "data": {"card": {"name": "碧诗", "sign": "hi", "fans": 10}}}
    client, _ = make_client([payload])
    p = client.profile(2)
    assert p and (p.name, p.sign, p.fans) == ("碧诗", "hi", 10)


def test_creator_report_parses_two_series():
    payload = {
        "code": 0,
        "data": {
            "total_fans": 390,
            "incr_fans": 2,
            "fan_recent_thirty": {
                "follow": {"20261101": 3, "20261102": 0, "20261103": 12},
                "unfollow": {"20261101": 1, "20261103": 4},
            },
        },
    }
    client, _ = make_client([payload], cookie="SESSDATA=abc")
    report = client.creator_report()
    assert [d.day.isoformat() for d in report.days] == ["2026-11-01", "2026-11-02", "2026-11-03"]
    assert [(d.new_fans, d.lost_fans) for d in report.days] == [(3, 1), (0, 0), (12, 4)]
    assert report.total_fans == 390
    assert all(d.total_fans == 390 for d in report.days)


def test_creator_report_ignores_bad_date_keys():
    payload = {"code": 0, "data": {"fan_recent_thirty": {"follow": {"bad": 1}, "unfollow": {}}}}
    client, _ = make_client([payload], cookie="SESSDATA=abc")
    assert client.creator_report().days == []


def test_creator_report_requires_cookie():
    client, stub = make_client([])
    with pytest.raises(ApiError) as info:
        client.creator_report()
    assert info.value.code == -101
    assert stub.calls == []  # 没 Cookie 就别浪费一次请求


def test_creator_report_maps_login_error():
    client, _ = make_client([{"code": -101, "message": "账号未登录"}], cookie="SESSDATA=x")
    with pytest.raises(ApiError) as info:
        client.creator_report()
    assert info.value.code == -101
    assert "Cookie" in str(info.value)


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("", ""),
        ("  ", ""),
        ("abc123", "SESSDATA=abc123"),
        ('"SESSDATA=abc123"', "SESSDATA=abc123"),
        ("SESSDATA=abc; bili_jct=def", "SESSDATA=abc; bili_jct=def"),
        # 从 DevTools 的「请求标头」里直接复制整行
        ("cookie: SESSDATA=abc; bili_jct=def", "SESSDATA=abc; bili_jct=def"),
        ("Cookie: SESSDATA=abc", "SESSDATA=abc"),
        # 从 cURL 里抠出来的片段
        ("-H 'cookie: SESSDATA=abc; buvid3=xyz'", "SESSDATA=abc; buvid3=xyz"),
        # 带换行的粘贴
        ("SESSDATA=abc\nbili_jct=def", "SESSDATA=abc bili_jct=def"),
    ],
)
def test_normalize_cookie(raw, expect):
    assert normalize_cookie(raw) == expect


class FakeBackend:
    def __init__(self, results):
        self.results = list(results)
        self.seen = 0

    def __call__(self, req, timeout):
        self.seen += 1
        status, body = self.results.pop(0)
        return status, body.encode(), {}


def test_transport_retries_on_412_then_succeeds():
    backend = FakeBackend([(412, "blocked"), (200, '{"code":0}')])
    tr = Transport(backend=backend, retries=3, sleep=lambda _: None)
    assert tr.get_json("https://x/y") == {"code": 0}
    assert backend.seen == 2


def test_transport_gives_up_after_retries():
    backend = FakeBackend([(503, "busy")] * 3)
    tr = Transport(backend=backend, retries=3, sleep=lambda _: None)
    with pytest.raises(HttpError) as info:
        tr.get_json("https://x/y")
    assert info.value.status == 503
    assert backend.seen == 3


def test_transport_does_not_retry_404():
    backend = FakeBackend([(404, "nope")])
    tr = Transport(backend=backend, retries=3, sleep=lambda _: None)
    with pytest.raises(HttpError) as info:
        tr.get_json("https://x/y")
    assert info.value.status == 404
    assert backend.seen == 1
