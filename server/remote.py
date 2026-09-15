"""Reaching Night Reader from somewhere other than this machine — a phone, mostly.

Why this module exists, and why it is careful
---------------------------------------------
The whole API is unauthenticated, and deliberately so: it acts on local files and is bound
to the loopback interface, which *is* the authentication. Nothing in ``server/app.py``
checks a header, and that has been fine because nothing outside this machine could reach it.

Posting from a phone breaks that arrangement. The run request already makes the feature
work — pressing Start writes a file and whichever Chrome is open picks it up — so "trigger
it from my phone" reduces entirely to reaching the app's own web interface. But the moment
the port is reachable from anything but this machine, "bound to loopback" stops being an
authentication story and the library is open to whatever else is on that network.

So: **loopback stays exactly as it was, and everything else needs a token.** The desktop
path is untouched, which matters because it is the path that works today; remote access is
opt-in, off until switched on, and refused without the token even if someone binds the port
wide by hand.

The token travels in a cookie
-----------------------------
A phone should need the link once, not a header on every request. So a valid ``?k=`` on any
request sets an ``nr_token`` cookie, and the browser sends it from then on — which means the
frontend needs no authentication code at all, and cannot break the desktop path by getting
it wrong. A header (``X-NR-Token``) is accepted too, for anything scripted.

The DNS-rebinding defence still matters
---------------------------------------
``TrustedHostMiddleware`` is not redundant here. A malicious page that resolves its own
hostname to 127.0.0.1 makes requests that arrive FROM loopback, and loopback is exempt from
the token — so the Host check is what stands between that page and this library. It stays
strict, and remote hostnames are added to it explicitly rather than by opening it to ``*``.
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

from translation_bot.atomic import atomic_write_text, quarantine_unreadable

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# A file of its own rather than a section in config.toml: that file is hand-edited and
# patched by regex elsewhere in this app, and a secret does not belong somewhere a comment
# rewrite could mangle it.
SETTINGS_NAME = "remote.json"

TOKEN_HEADER = "X-NR-Token"
TOKEN_COOKIE = "nr_token"
TOKEN_QUERY = "k"

# A year. The point is that a phone is set up once and then works; an expiry measured in
# hours would mean digging the link out again every morning.
COOKIE_MAX_AGE = 365 * 24 * 60 * 60

_DEFAULTS = {
    # Off until switched on. Reaching this app from elsewhere should be a decision, not
    # something that quietly became true because the code shipped.
    "enabled": False,
    "token": "",
    # Extra Host headers to trust, for a name this module could not work out on its own.
    "extra_hosts": [],
    # Where to send a "your run stopped" push. Left empty: it needs an account this app
    # has no business creating, and an empty value simply means no notifications.
    "notify_url": "",
}


def settings_path() -> Path:
    # A function, not a constant, so the test suite's tmp_path redirection is honoured —
    # the same reason series_root() and adapters_dir() are functions.
    return Path(os.environ.get("NR_REMOTE_SETTINGS") or (PROJECT_ROOT / SETTINGS_NAME))


def load() -> dict:
    path = settings_path()
    doc: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            doc = loaded if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            # Keep the bytes. This file holds the only copy of the token, and a transient
            # read failure that returned "no token" would otherwise let the next write
            # replace it with a fresh one, silently breaking every phone already set up.
            quarantine_unreadable(path)
            doc = {}
    out = {**_DEFAULTS, **doc}
    out["enabled"] = bool(out.get("enabled"))
    out["token"] = str(out.get("token") or "")
    hosts = out.get("extra_hosts")
    out["extra_hosts"] = [str(h).strip() for h in hosts if str(h).strip()] \
        if isinstance(hosts, list) else []
    out["notify_url"] = str(out.get("notify_url") or "").strip()
    return out


def save(doc: dict) -> dict:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=2))
    return doc


def new_token() -> str:
    """128 bits, hex. Long enough that guessing is not a threat model."""
    return secrets.token_hex(16)


def ensure_token() -> dict:
    doc = load()
    if not doc["token"]:
        doc["token"] = new_token()
        save(doc)
    return doc


# ---------------------------------------------------------------------------
# Who is asking
# ---------------------------------------------------------------------------


def is_loopback(host: str | None) -> bool:
    """Whether a request came from this machine.

    Unparseable counts as NOT loopback. Every uncertainty here has to fall on the side of
    asking for the token, because the loopback answer is the one that skips the check.
    """
    if not host:
        return False
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def token_from_request(headers, cookies, query) -> str:
    """The token this request carries, from any of the three places it may travel."""
    return str(
        (headers.get(TOKEN_HEADER) if headers else None)
        or (cookies.get(TOKEN_COOKIE) if cookies else None)
        or (query.get(TOKEN_QUERY) if query else None)
        or ""
    ).strip()


def token_matches(supplied: str, expected: str) -> bool:
    """Constant-time, and never true for an empty expected token.

    An unset token must not authenticate an empty header — that would turn "remote access
    is on but not configured" into "remote access is open".
    """
    if not supplied or not expected:
        return False
    return secrets.compare_digest(supplied, expected)


# ---------------------------------------------------------------------------
# Where this machine can be reached
# ---------------------------------------------------------------------------

# Tailscale hands out addresses from the carrier-grade NAT range, which is how one can be
# told apart from an ordinary LAN address without asking Tailscale anything.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

# An explicit whitelist rather than ipaddress.is_private, which was the first version of
# this and was wrong: Python counts the documentation, benchmarking and reserved ranges as
# private too, so an address out of any of them would have been offered to the user as
# somewhere to browse. These four are the only ones a phone could actually reach this
# machine on.
_REACHABLE = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    _CGNAT,
)

_TAILSCALE_CANDIDATES = (
    "tailscale",
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
    "/usr/bin/tailscale",
    "/usr/local/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)


def _tailscale_exe() -> str | None:
    for candidate in _TAILSCALE_CANDIDATES:
        found = shutil.which(candidate) if candidate == "tailscale" else (
            candidate if Path(candidate).exists() else None)
        if found:
            return found
    return None


def _tailscale_addresses() -> list[str]:
    exe = _tailscale_exe()
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True,
                             timeout=4, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in (out.stdout or "").splitlines() if line.strip()]


def _own_addresses() -> list[str]:
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in found:
                found.append(address)
    except (OSError, socket.gaierror):
        pass
    return found


def _kind(address: str) -> str | None:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return None
    if ip in _CGNAT:
        return "tailscale"
    if any(ip in net for net in _REACHABLE):
        return "local network"
    # Everything else: loopback (not somewhere a phone can reach), 169.254.x.x (what an
    # interface assigns itself when it never got a lease, so it answers nothing), and any
    # globally routable address - naming one of those as somewhere to browse would be
    # advice to put this library on the internet.
    return None


def addresses(port: int = 8000) -> list[dict]:
    """Addresses this machine plausibly answers on, best first.

    Tailscale addresses lead because a tailnet is private and authenticated by Tailscale
    itself, which makes it the one of these that is reasonable to use from outside the
    house. A LAN address is offered because it is useful on a home network, and labelled so
    the difference is visible.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for address in [*_tailscale_addresses(), *_own_addresses()]:
        if address in seen:
            continue
        seen.add(address)
        kind = _kind(address)
        if kind is None:
            continue
        out.append({"host": address, "kind": kind,
                    "url": f"http://{address}:{port}"})
    out.sort(key=lambda row: 0 if row["kind"] == "tailscale" else 1)
    return out


def allowed_hosts() -> list[str]:
    """Host headers to accept, for TrustedHostMiddleware.

    Loopback always. The rest only while remote access is on, so switching it off closes
    the Host check back down rather than leaving it open from a previous session.
    """
    hosts = ["localhost", "127.0.0.1"]
    doc = load()
    if not doc["enabled"]:
        return hosts
    name = socket.gethostname()
    for host in [name, f"{name}.local", *(row["host"] for row in addresses()),
                 *doc["extra_hosts"]]:
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def phone_url(port: int = 8000) -> str:
    """The one link to open on the phone, token and all, or empty if not set up."""
    doc = load()
    rows = addresses(port)
    if not (doc["enabled"] and doc["token"] and rows):
        return ""
    return f'{rows[0]["url"]}/?{TOKEN_QUERY}={doc["token"]}'


# ---------------------------------------------------------------------------
# Telling the phone something went wrong
# ---------------------------------------------------------------------------


def notify(title: str, body: str = "") -> bool:
    """Push a short message to whatever the user has pointed this at.

    The point of starting a run remotely is that nobody is at the desk when it stops, so a
    failure that only appears in the app is a failure nobody sees for hours.

    Fire-and-forget on a daemon thread: this is called from the endpoint the extension
    reports to, and a slow or unreachable push service must not hold up recording that a
    chapter went out. Returns whether it was attempted, not whether it arrived.
    """
    url = load()["notify_url"]
    if not url:
        return False

    def _send() -> None:
        try:
            payload = (f"{title}\n\n{body}" if body else title).encode("utf-8")
            request = urllib.request.Request(
                url, data=payload, method="POST",
                # ntfy reads these; anything else ignores them harmlessly.
                headers={"Title": "Night Reader", "Content-Type": "text/plain"})
            urllib.request.urlopen(request, timeout=8).close()
        except (urllib.error.URLError, OSError, ValueError):
            # Nothing to do about it and nobody to tell — the run itself is unaffected.
            pass

    threading.Thread(target=_send, daemon=True).start()
    return True
