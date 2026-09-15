"""Tests for reaching this app from another device.

This is the one place in Night Reader where getting it wrong has a cost outside the app.
The whole API is unauthenticated because loopback binding IS its authentication; letting a
phone in retires that, and what replaces it is an access key. So these check both halves,
and the half that matters most is the refusals.

What each group is really guarding:

* **Loopback is untouched.** The desktop app, the Vite dev proxy and the extension's
  service worker all speak to localhost, and none of them carries a key. If the gate ever
  starts asking them for one, the app stops working entirely — so that is asserted first.
* **Nothing else gets in without the key**, including when remote access is switched on but
  half-configured, which is the state a half-finished setup leaves behind.
* **The key is never handed to a device that did not already have it**, because a page open
  on a phone is a page that can be read over a shoulder.

Runs under pytest (``pytest tests/``) and standalone (``python tests/test_remote.py``).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.app as A  # noqa: E402
import server.remote as remote  # noqa: E402

HERE = ("127.0.0.1", 50000)
ELSEWHERE = ("203.0.113.5", 41234)   # TEST-NET-3, reserved for documentation


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    """A remote.json of its own, so a test never reads or writes the real one."""
    path = tmp_path / "remote.json"
    monkeypatch.setenv("NR_REMOTE_SETTINGS", str(path))
    return path


@pytest.fixture
def here():
    return TestClient(A.app, base_url="http://localhost", client=HERE)


@pytest.fixture
def away():
    return TestClient(A.app, base_url="http://localhost", client=ELSEWHERE)


def _enable() -> str:
    doc = remote.load()
    doc["enabled"] = True
    doc["token"] = remote.new_token()
    remote.save(doc)
    return doc["token"]


# ---- loopback is exactly as it was ----------------------------------------


def test_this_computer_needs_no_key(here):
    # The path that works today: the desktop app, the dev proxy and the extension's service
    # worker all speak to localhost and none of them knows a key exists.
    assert here.get("/api/status").status_code == 200


def test_this_computer_still_needs_no_key_once_remote_access_is_on(here):
    _enable()
    assert here.get("/api/status").status_code == 200


# ---- nothing else gets in -------------------------------------------------


def test_another_device_is_refused_while_it_is_switched_off(away):
    r = away.get("/api/status")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "remote-disabled"


def test_another_device_is_refused_without_the_key(away):
    _enable()
    r = away.get("/api/status")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "remote-token"


def test_a_wrong_key_is_refused(away):
    _enable()
    r = away.get("/api/status", headers={remote.TOKEN_HEADER: remote.new_token()})
    assert r.status_code == 403


def test_an_empty_key_never_authenticates_an_unset_one(away):
    # The nastiest version of this bug: remote access on, no key generated yet, and a
    # request carrying nothing at all matching nothing at all.
    doc = remote.load()
    doc["enabled"] = True
    doc["token"] = ""
    remote.save(doc)
    assert away.get("/api/status").status_code == 403
    assert away.get("/api/status", headers={remote.TOKEN_HEADER: ""}).status_code == 403
    assert remote.token_matches("", "") is False


def test_the_right_key_in_a_header_is_let_through(away):
    token = _enable()
    assert away.get("/api/status",
                    headers={remote.TOKEN_HEADER: token}).status_code == 200


def test_the_key_in_the_link_is_let_through_and_remembered(away):
    # This is the phone flow: open the link once, and the cookie carries it afterwards.
    # It is also why no frontend code knows about any of this.
    token = _enable()
    r = away.get(f"/api/status?{remote.TOKEN_QUERY}={token}")
    assert r.status_code == 200
    assert r.cookies.get(remote.TOKEN_COOKIE) == token
    # The cookie alone, with the link long forgotten.
    assert away.get("/api/status").status_code == 200


def test_a_key_that_was_replaced_stops_working(away):
    token = _enable()
    assert away.get("/api/status", headers={remote.TOKEN_HEADER: token}).status_code == 200
    doc = remote.load()
    doc["token"] = remote.new_token()
    remote.save(doc)
    assert away.get("/api/status", headers={remote.TOKEN_HEADER: token}).status_code == 403


def test_switching_it_off_shuts_the_door_again(away):
    token = _enable()
    assert away.get("/api/status", headers={remote.TOKEN_HEADER: token}).status_code == 200
    doc = remote.load()
    doc["enabled"] = False
    remote.save(doc)
    assert away.get("/api/status", headers={remote.TOKEN_HEADER: token}).status_code == 403


# ---- the key is not handed out --------------------------------------------


def test_the_key_is_shown_to_this_computer_and_to_nothing_else(here, away):
    token = _enable()
    mine = here.get("/api/remote").json()
    assert mine["token"] == token and mine["local"] is True
    theirs = away.get("/api/remote", headers={remote.TOKEN_HEADER: token}).json()
    assert "token" not in theirs and "phone_url" not in theirs
    assert theirs["local"] is False and theirs["has_token"] is True


def test_a_phone_cannot_replace_the_key_and_cut_itself_off(away):
    token = _enable()
    r = away.post("/api/remote", json={"rotate": True},
                  headers={remote.TOKEN_HEADER: token})
    assert r.status_code == 403
    assert remote.load()["token"] == token


def test_this_computer_can_replace_the_key(here):
    token = _enable()
    after = here.post("/api/remote", json={"rotate": True}).json()
    assert after["token"] != token and len(after["token"]) == 32


# ---- switching it on ------------------------------------------------------


def test_switching_it_on_creates_a_key_in_the_same_breath(here, settings):
    # Enabled with no key would mean "reachable, and nothing to prove", so there must be no
    # window in which that is the stored state.
    assert here.get("/api/remote").json()["has_token"] is False
    after = here.post("/api/remote", json={"enabled": True}).json()
    assert after["enabled"] is True and len(after["token"]) == 32
    assert json.loads(settings.read_text(encoding="utf-8"))["token"] == after["token"]


def test_it_is_off_until_it_is_switched_on(here):
    state = here.get("/api/remote").json()
    assert state["enabled"] is False


def test_a_notification_address_has_to_look_like_one(here):
    assert here.post("/api/remote",
                     json={"notify_url": "ntfy.sh/topic"}).status_code == 400
    ok = here.post("/api/remote",
                   json={"notify_url": "https://ntfy.sh/topic"}).json()
    assert ok["notify_url"] == "https://ntfy.sh/topic"


def test_an_unknown_setting_is_refused_rather_than_ignored(here):
    assert here.post("/api/remote", json={"enabld": True}).status_code == 422


def test_a_test_notification_needs_an_address_first(here):
    assert here.post("/api/remote/test-notification").status_code == 400


# ---- the pieces underneath ------------------------------------------------


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("127.0.0.53", True), ("::1", True), ("localhost", True),
    ("203.0.113.5", False), ("192.168.1.10", False), ("100.101.102.103", False),
    ("testclient", False), ("", False), (None, False),
])
def test_who_counts_as_this_machine(host, expected):
    # "testclient" is the name the test harness uses by default, and it must NOT count:
    # every uncertainty here has to fall on the side of asking for the key.
    assert remote.is_loopback(host) is expected


def test_the_host_check_stays_shut_until_remote_access_is_on():
    # TrustedHostMiddleware is not redundant: a DNS-rebinding page's requests arrive FROM
    # loopback and so skip the key entirely, which makes the Host check the only thing
    # standing in their way.
    assert remote.allowed_hosts() == ["localhost", "127.0.0.1"]
    _enable()
    assert len(remote.allowed_hosts()) > 2


def test_an_address_no_one_can_reach_is_not_offered(monkeypatch):
    # 169.254.x.x is what an interface assigns itself when it never got a lease. It counts
    # as private, so it would otherwise be offered as somewhere to browse.
    monkeypatch.setattr(remote, "_tailscale_addresses", lambda: [])
    monkeypatch.setattr(remote, "_own_addresses",
                        lambda: ["169.254.83.107", "192.168.68.116", "127.0.0.1"])
    assert [a["host"] for a in remote.addresses()] == ["192.168.68.116"]


def test_a_tailnet_address_is_offered_first(monkeypatch):
    # A tailnet is private and authenticated by Tailscale itself, which makes it the one of
    # these that is reasonable to use away from home.
    monkeypatch.setattr(remote, "_tailscale_addresses", lambda: ["100.101.102.103"])
    monkeypatch.setattr(remote, "_own_addresses", lambda: ["192.168.68.116"])
    rows = remote.addresses(8000)
    assert rows[0] == {"host": "100.101.102.103", "kind": "tailscale",
                       "url": "http://100.101.102.103:8000"}
    assert rows[1]["kind"] == "local network"


def test_only_an_address_a_phone_could_reach_is_offered(monkeypatch):
    # This caught a real bug. The first version asked ipaddress.is_private, and Python
    # counts the documentation (203.0.113.x), benchmarking (198.18.x) and reserved
    # (192.0.0.x) ranges as private too - so any of those would have been presented to the
    # user as somewhere to browse. Only the three RFC1918 blocks and the tailnet range are.
    monkeypatch.setattr(remote, "_tailscale_addresses", lambda: [])
    monkeypatch.setattr(remote, "_own_addresses", lambda: [
        "203.0.113.5", "8.8.8.8", "192.0.0.1", "198.18.0.1", "172.32.5.5", "0.0.0.0"])
    assert remote.addresses() == []


@pytest.mark.parametrize("address,kind", [
    ("100.101.102.103", "tailscale"),
    ("10.1.2.3", "local network"),
    ("172.16.5.5", "local network"),
    ("192.168.68.116", "local network"),
    ("172.32.5.5", None),            # just outside the /12
    ("169.254.83.107", None),        # never got a lease; answers nothing
    ("127.0.0.1", None),             # not somewhere a phone can reach
    ("8.8.8.8", None),
    ("nonsense", None),
])
def test_how_an_address_is_classified(address, kind):
    assert remote._kind(address) == kind


def test_the_phone_link_carries_the_key(monkeypatch):
    monkeypatch.setattr(remote, "_tailscale_addresses", lambda: ["100.101.102.103"])
    monkeypatch.setattr(remote, "_own_addresses", lambda: [])
    token = _enable()
    assert remote.phone_url(8000) == (
        f"http://100.101.102.103:8000/?{remote.TOKEN_QUERY}={token}")


def test_there_is_no_phone_link_before_it_is_set_up(monkeypatch):
    monkeypatch.setattr(remote, "_tailscale_addresses", lambda: ["100.101.102.103"])
    monkeypatch.setattr(remote, "_own_addresses", lambda: [])
    assert remote.phone_url() == ""


def test_notifying_nowhere_does_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(remote.threading, "Thread",
                        lambda **kw: called.append(kw) or _NoThread())
    assert remote.notify("hello") is False
    assert called == []


def test_notifying_somewhere_is_attempted_off_the_request(monkeypatch):
    # Off the request thread on purpose: this fires from the endpoint the extension reports
    # to, and a slow push service must not hold up recording that a chapter went out.
    doc = remote.load()
    doc["notify_url"] = "https://ntfy.sh/topic"
    remote.save(doc)
    started = []
    monkeypatch.setattr(remote.threading, "Thread",
                        lambda **kw: started.append(kw) or _NoThread())
    assert remote.notify("stopped", "why") is True
    assert started and started[0]["daemon"] is True


class _NoThread:
    def start(self) -> None:
        pass


def test_an_unreadable_settings_file_is_kept_aside(settings):
    # It holds the only copy of the key. A read failure that returned "no key" would let
    # the next write mint a fresh one, silently breaking every phone already set up.
    settings.write_text("{ not json", encoding="utf-8")
    assert remote.load()["token"] == ""
    assert list(settings.parent.glob("remote.json.unreadable-*"))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
