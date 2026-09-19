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
    # Lands on the phone console, not the library root: opening this on a phone should
    # give the one page built for it rather than the desktop app scaled down.
    assert remote.phone_url(8000) == (
        f"http://100.101.102.103:8000/phone?{remote.TOKEN_QUERY}={token}")


def test_there_is_no_phone_link_before_it_is_set_up(monkeypatch):
    monkeypatch.setattr(remote, "_tailscale_addresses", lambda: ["100.101.102.103"])
    monkeypatch.setattr(remote, "_own_addresses", lambda: [])
    assert remote.phone_url() == ""


# ---- https, which is what makes the icon a real app -----------------------


def _serving(monkeypatch, web):
    """Fake the two Tailscale CLI reads serve_state does."""
    def fake(args):
        if args[0] == "status":
            return {"Self": {"DNSName": "box.tailnet.ts.net."}}
        return {"Web": web}
    monkeypatch.setattr(remote, "_tailscale_json", fake)


def test_the_tailnet_name_loses_its_trailing_dot(monkeypatch):
    # DNSName comes back fully qualified ("box.tailnet.ts.net."), and a trailing dot in a
    # URL is not something to hand to a person to type.
    _serving(monkeypatch, {})
    assert remote.tailnet_name() == "box.tailnet.ts.net"


def test_with_nothing_served_it_suggests_the_tidy_443_url(monkeypatch):
    _serving(monkeypatch, {})
    state = remote.serve_state(8000)
    assert state["https_url"] == ""
    assert state["command"] == "tailscale serve --bg http://127.0.0.1:8000"
    assert state["would_be"] == "https://box.tailnet.ts.net"


def test_an_app_already_on_443_is_routed_around_not_over(monkeypatch):
    # The real case this was written against: 443 already proxies a different app. Taking
    # it would break that app to make an icon slightly nicer, which is a bad trade — so
    # the suggestion moves to another port and says why.
    _serving(monkeypatch, {"box.tailnet.ts.net:443": {
        "Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}})
    state = remote.serve_state(8000)
    assert state["command"] == (
        f"tailscale serve --bg --https={remote.ALTERNATE_HTTPS_PORT} http://127.0.0.1:8000")
    assert state["would_be"] == f"https://box.tailnet.ts.net:{remote.ALTERNATE_HTTPS_PORT}"
    assert state["serving_other"] == {"https://box.tailnet.ts.net": "http://127.0.0.1:8787"}


def test_an_app_already_served_reports_its_https_url(monkeypatch):
    # Nothing to run: the phone can install properly today.
    _serving(monkeypatch, {"box.tailnet.ts.net:443": {
        "Handlers": {"/": {"Proxy": "http://127.0.0.1:8000"}}}})
    state = remote.serve_state(8000)
    assert state["https_url"] == "https://box.tailnet.ts.net"
    assert state["serving_other"] == {}


def test_a_non_standard_https_port_keeps_its_port_in_the_url(monkeypatch):
    _serving(monkeypatch, {"box.tailnet.ts.net:8443": {
        "Handlers": {"/": {"Proxy": "http://127.0.0.1:8000"}}}})
    assert remote.serve_state(8000)["https_url"] == "https://box.tailnet.ts.net:8443"


def test_without_tailscale_there_is_nothing_to_suggest(monkeypatch):
    monkeypatch.setattr(remote, "_tailscale_exe", lambda: None)
    state = remote.serve_state(8000)
    assert state == {"tailnet_name": "", "https_url": "", "serving_other": {},
                     "command": "", "would_be": ""}


def test_a_broken_tailscale_cli_is_not_an_error(monkeypatch):
    # It is consulted on every Settings load. A crash here must not take the page with it.
    monkeypatch.setattr(remote, "_tailscale_exe", lambda: "tailscale")
    def boom(*a, **k):
        raise OSError("no")
    monkeypatch.setattr(remote.subprocess, "run", boom)
    assert remote.serve_state(8000)["command"] == ""


def test_the_tailnet_name_is_trusted_only_while_remote_access_is_on(monkeypatch):
    # A request proxied by `tailscale serve` arrives FROM loopback and carries the tailnet
    # NAME in its Host header — so TrustedHostMiddleware is the only thing in its way, and
    # it has to close back down when the feature is switched off.
    monkeypatch.setattr(remote, "tailnet_name", lambda: "box.tailnet.ts.net")
    monkeypatch.setattr(remote, "addresses", lambda port=8000: [])
    assert "box.tailnet.ts.net" not in remote.allowed_hosts()
    _enable()
    assert "box.tailnet.ts.net" in remote.allowed_hosts()


# ---- every link handed to a phone carries the key -------------------------


def test_the_https_link_offered_for_installing_carries_the_key(here, monkeypatch):
    """The bug this exists for, found by actually opening the link.

    A request proxied by `tailscale serve` does NOT arrive from loopback -- measured, it
    is refused like any other device -- so an https link without the key opens straight
    onto a 403. The Settings panel built that link by hand, in the one branch whose whole
    job is to say "ready to install".
    """
    monkeypatch.setattr(remote, "_tailscale_json", lambda args: (
        {"Self": {"DNSName": "box.tailnet.ts.net."}} if args[0] == "status"
        else {"Web": {"box.tailnet.ts.net:8443": {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:8000"}}}}}))
    token = _enable()
    serve = here.get("/api/remote").json()["serve"]
    assert serve["https_url"] == "https://box.tailnet.ts.net:8443"
    assert serve["phone_url"] == (
        f"https://box.tailnet.ts.net:8443/phone?{remote.TOKEN_QUERY}={token}")


def test_there_is_no_https_link_until_something_is_served(here, monkeypatch):
    monkeypatch.setattr(remote, "_tailscale_json", lambda args: (
        {"Self": {"DNSName": "box.tailnet.ts.net."}} if args[0] == "status" else {}))
    _enable()
    serve = here.get("/api/remote").json()["serve"]
    assert serve["https_url"] == ""
    # Empty rather than a keyless URL: the UI shows the setup command in this case, and a
    # half-built link would be worse than none.
    assert serve["phone_url"] == ""


def test_a_link_is_never_built_without_a_key(monkeypatch):
    # phone_link is the single place both links come from, so this covers the http one too.
    assert remote.phone_link("https://box.ts.net:8443") == ""   # remote access still off
    _enable()
    assert remote.phone_link("") == ""


# ---- the home-screen icon -------------------------------------------------


def test_the_manifest_never_hands_the_key_to_a_stranger(away):
    # The whole reason the manifest is generated rather than a static file is that its
    # start_url can carry the key -- which makes it exactly the kind of file that must
    # stay behind the gate. An unauthenticated fetch gets the same 403 as anything else.
    token = _enable()
    r = away.get("/manifest.webmanifest")
    assert r.status_code == 403
    assert token not in r.text


def test_an_authenticated_phone_gets_a_start_url_that_carries_the_key(away):
    # Why it carries the key at all: a launcher icon is opened months after it was made,
    # by which time the cookie planted on the first visit may be long evicted. An app
    # icon that opens onto "that link is missing its access key" is worse than no icon.
    token = _enable()
    doc = away.get(f"/manifest.webmanifest?{remote.TOKEN_QUERY}={token}").json()
    assert doc["start_url"] == f"/phone?{remote.TOKEN_QUERY}={token}"
    assert doc["display"] == "standalone"


def test_this_computer_gets_a_plain_start_url(here):
    # No key exists on the desktop path and none is needed, so none is invented.
    assert here.get("/manifest.webmanifest").json()["start_url"] == "/phone"


def test_the_manifest_is_not_cached(here):
    # It can carry a secret and it changes when the key is rotated.
    r = here.get("/manifest.webmanifest")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"].startswith("application/manifest+json")


def test_every_icon_the_manifest_promises_actually_exists(here):
    # A manifest listing a 404 is not an install error the browser explains -- it just
    # silently declines to offer the install.
    public = remote.PROJECT_ROOT / "web" / "public"
    for icon in here.get("/manifest.webmanifest").json()["icons"]:
        assert (public / icon["src"].lstrip("/")).exists(), icon["src"]


def test_the_scope_does_not_claim_the_whole_hostname(here):
    """Narrow on purpose, and the reason is off-web.

    Installing this mints an Android WebAPK whose intent filter carries scheme, host and
    path prefix but NO port -- and Android treats an absent port as any port. A scope of
    "/" would therefore register Night Reader as a handler for every URL on this hostname,
    including another app's, which is exactly how the other app captured ours.
    """
    doc = here.get("/manifest.webmanifest").json()
    assert doc["scope"] == "/phone"
    # start_url and id must both sit inside the scope or the install is rejected.
    assert doc["start_url"].startswith("/phone")
    assert doc["id"].startswith("/phone")


def test_the_key_cookie_is_secure_over_https(monkeypatch):
    # The key is the only authentication this app has, and it is set on a hostname that
    # may be shared with another app. Secure at least keeps it off plain http.
    from fastapi.testclient import TestClient
    token = _enable()
    tls = TestClient(A.app, base_url="https://localhost", client=ELSEWHERE)
    r = tls.get(f"/api/status?{remote.TOKEN_QUERY}={token}")
    assert r.status_code == 200
    assert "secure" in r.headers["set-cookie"].lower()


def test_the_key_cookie_is_still_set_over_plain_http(away):
    """Secure is conditional, and this is why.

    The plain-http tailnet link is still a supported way in. An unconditional Secure flag
    is silently dropped over http, so the phone would go back to needing ?k= on every
    request -- the one thing the cookie exists to prevent.
    """
    token = _enable()
    r = away.get(f"/api/status?{remote.TOKEN_QUERY}={token}")
    assert r.status_code == 200
    assert "secure" not in r.headers["set-cookie"].lower()
    assert r.cookies.get(remote.TOKEN_COOKIE) == token


def test_the_maskable_icon_is_listed_separately(here):
    # Listing one file as both "any" and "maskable" is the classic mistake: a launcher
    # crops a maskable icon to its own shape, so the rounded artwork loses its corners.
    icons = here.get("/manifest.webmanifest").json()["icons"]
    purposes = {i["src"]: i["purpose"] for i in icons}
    assert "maskable" in purposes.values()
    assert not any(p == "any maskable" for p in purposes.values())


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
