"""扫码登录流程测试：用假客户端走完申请 → 轮询 → 成功/超时。"""

from __future__ import annotations


from bili_fans import login as qrlogin
from bili_fans.api import ApiError, QRLoginState, QRSession


class FakeClient:
    def __init__(self, states):
        self.states = list(states)
        self.polls = 0

    def qr_generate(self):
        return QRSession(key="k123", url="https://example.com/scan?qrcode_key=k123")

    def qr_poll(self, key):
        self.polls += 1
        item = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if isinstance(item, Exception):
            raise item
        return item


def test_begin_returns_session():
    session = qrlogin.begin(FakeClient([QRLoginState(status="waiting")]))
    assert session.key == "k123"
    assert "qrcode_key=k123" in session.url


def test_wait_until_confirmed():
    client = FakeClient([
        QRLoginState(status="waiting"),
        QRLoginState(status="scanned"),
        QRLoginState(status="confirmed", cookie="SESSDATA=abc", values={"SESSDATA": "abc"}),
    ])
    session = qrlogin.begin(client)
    ticks = []
    state = qrlogin.wait(client, session, timeout=30, interval=0, on_tick=lambda left, s: ticks.append(s))
    assert state.status == "confirmed"
    assert state.cookie == "SESSDATA=abc"
    assert ticks[:2] == ["waiting", "scanned"]


def test_wait_stops_on_expired():
    client = FakeClient([QRLoginState(status="expired")])
    state = qrlogin.wait(client, qrlogin.begin(client), timeout=30, interval=0)
    assert state.status == "expired"


def test_wait_times_out_as_waiting():
    client = FakeClient([QRLoginState(status="waiting")])
    state = qrlogin.wait(client, qrlogin.begin(client), timeout=0.05, interval=0.01)
    assert state.status == "waiting"


def test_wait_survives_api_errors():
    client = FakeClient([ApiError(-1, "网络抖动"), QRLoginState(status="confirmed", cookie="x")])
    state = qrlogin.wait(client, qrlogin.begin(client), timeout=5, interval=0)
    assert state.status == "confirmed"


def test_describe_labels():
    assert qrlogin.describe("scanned") == "已扫码，请在手机上确认"
    assert qrlogin.describe("confirmed") == "登录成功"


def test_finalize_adds_buvid():
    class WithFingerprint:
        def fingerprint(self):
            return "B3", "B4"

    state = QRLoginState(status="confirmed", values={"SESSDATA": "s"}, cookie="SESSDATA=s")
    cookie, values = qrlogin.finalize(WithFingerprint(), state)

    assert values["buvid3"] == "B3" and values["buvid4"] == "B4"
    assert "SESSDATA=s" in cookie and "buvid3=B3" in cookie


def test_finalize_survives_fingerprint_failure():
    class Broken:
        def fingerprint(self):
            raise ApiError(-352, "风控")

    state = QRLoginState(status="confirmed", values={"SESSDATA": "s"}, cookie="SESSDATA=s")
    cookie, values = qrlogin.finalize(Broken(), state)
    assert cookie == "SESSDATA=s"
    assert "buvid3" not in values
