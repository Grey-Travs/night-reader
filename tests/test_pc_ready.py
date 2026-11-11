"""The computer always ready for the phone: started with Windows, kept awake while there is
work, and never turning the phone away because it started before Tailscale.

Runs under pytest (``pytest tests/``). Nothing here touches the real registry or power
settings: both go through fakes.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import launch  # noqa: E402
import server.app as A  # noqa: E402
import server.remote as R  # noqa: E402
from server import autostart, keep_awake  # noqa: E402
from translation_bot.config import Config  # noqa: E402


# ---- starting with Windows -------------------------------------------------------------------

class FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeRegistry:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def OpenKey(self, root, path, reserved, access):  # noqa: N802 — winreg's names
        assert (root, path) == ("HKCU", autostart.RUN_KEY)
        return FakeKey()

    CreateKeyEx = OpenKey

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802
        self.values[name] = value

    def DeleteValue(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def test_starting_with_windows_runs_this_copy_quietly(tmp_path):
    reg = FakeRegistry()
    assert autostart.state(reg, root=tmp_path) == {"supported": True, "on": False, "current": False}
    state = autostart.set_on(True, reg, root=tmp_path)
    assert state == {"supported": True, "on": True, "current": True}
    command = reg.values["NightReader"]
    assert "pythonw.exe" in command and command.endswith('launch.py" --background')
    assert str(tmp_path) in command


def test_an_entry_for_a_moved_copy_is_said_to_be_out_of_date(tmp_path):
    reg = FakeRegistry()
    autostart.set_on(True, reg, root=tmp_path / "old")
    assert autostart.state(reg, root=tmp_path / "new")["current"] is False


def test_switching_it_off_removes_the_entry(tmp_path):
    reg = FakeRegistry()
    autostart.set_on(True, reg, root=tmp_path)
    assert autostart.set_on(False, reg, root=tmp_path)["on"] is False
    assert "NightReader" not in reg.values
    autostart.set_on(False, reg, root=tmp_path)   # already off: fine


# ---- keeping awake ------------------------------------------------------------------------------

class FakeKernel:
    def __init__(self):
        self.calls = []

    def SetThreadExecutionState(self, flags):  # noqa: N802
        self.calls.append(flags)


def test_it_holds_the_computer_awake_only_while_there_is_work():
    kernel = FakeKernel()
    busy = {"now": False}
    keeper = keep_awake.KeepAwake(lambda: busy["now"], kernel32=kernel)
    keeper.tick()
    assert kernel.calls == []
    busy["now"] = True
    keeper.tick()
    keeper.tick()
    assert kernel.calls == [keep_awake.ES_CONTINUOUS | keep_awake.ES_SYSTEM_REQUIRED]
    busy["now"] = False
    keeper.tick()
    assert kernel.calls[-1] == keep_awake.ES_CONTINUOUS
    assert keeper.holding is False


def test_a_failed_check_lets_go_rather_than_holding_forever():
    kernel = FakeKernel()
    state = {"fail": False}

    def busy():
        if state["fail"]:
            raise RuntimeError("config unreadable")
        return True

    keeper = keep_awake.KeepAwake(busy, kernel32=kernel)
    keeper.tick()
    state["fail"] = True
    keeper.tick()
    assert keeper.holding is False


def test_its_thread_lets_go_when_it_stops():
    kernel = FakeKernel()
    keeper = keep_awake.KeepAwake(lambda: True, kernel32=kernel, every=0.01)
    keeper.start()
    deadline = time.time() + 5
    while not kernel.calls and time.time() < deadline:
        time.sleep(0.01)
    keeper.stop()
    assert kernel.calls[0] & keep_awake.ES_SYSTEM_REQUIRED
    assert kernel.calls[-1] == keep_awake.ES_CONTINUOUS


def test_what_counts_as_work(monkeypatch):
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    monkeypatch.setattr(A, "_last_photo_at", 0.0)
    monkeypatch.setattr(A, "_jobs", {})
    monkeypatch.setattr(A, "_retry_timers", {})
    assert A._work_pending() is False
    job = A.Job("j", "p")
    monkeypatch.setitem(A._jobs, "j", job)
    assert A._work_pending() is True, "a job running, or waiting out the plan's limit"
    job.done = True
    assert A._work_pending() is False
    monkeypatch.setattr(A, "_last_photo_at", time.time() - 60)
    assert A._work_pending() is True, "photos arrived a minute ago; more may be coming"
    off = Config()
    off.phone.keep_awake = False
    monkeypatch.setattr(A, "load_global_config", lambda: off)
    assert A._work_pending() is False, "switched off in Settings"


# ---- the host list catches up -----------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    return TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000))


def test_a_name_learned_after_startup_is_let_in(client, monkeypatch):
    asked = []
    monkeypatch.setattr(R, "allowed_hosts",
                        lambda: asked.append(1) or ["localhost", "127.0.0.1", "pc.tail.ts.net"])
    monkeypatch.setitem(A._hosts, "list", ["localhost", "127.0.0.1"])
    monkeypatch.setitem(A._hosts, "looked", time.monotonic())
    headers = {"host": "pc.tail.ts.net"}
    assert client.get("/api/computer", headers=headers).status_code == 400, \
        "looked a moment ago: not asked again yet"
    assert asked == []
    monkeypatch.setitem(A._hosts, "looked", time.monotonic() - 60)
    assert client.get("/api/computer", headers=headers).status_code == 200
    assert client.get("/api/computer", headers=headers).status_code == 200
    assert asked == [1], "asked once, then known"
    assert client.get("/api/computer", headers={"host": "evil.example"}).status_code == 400


# ---- the settings ---------------------------------------------------------------------------------------

def test_the_settings_say_how_the_computer_is_set_up(client, monkeypatch):
    monkeypatch.setattr(autostart, "state", lambda *a, **k: {"supported": True, "on": True, "current": True})
    res = client.get("/api/computer").json()
    assert res == {"autostart": {"supported": True, "on": True, "current": True}, "keep_awake": True}


def test_only_the_computer_itself_can_change_them(monkeypatch):
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    phone = TestClient(A.app, base_url="http://localhost", client=("100.64.0.9", 50000))
    monkeypatch.setattr(R, "load", lambda: {**R._DEFAULTS, "enabled": True, "token": "k" * 32})
    res = phone.post("/api/computer", json={"autostart": True}, params={"k": "k" * 32})
    assert res.status_code == 403


def test_switching_autostart_where_it_isnt_supported_says_so(client, monkeypatch):
    monkeypatch.setattr(autostart, "supported", lambda: False)
    res = client.post("/api/computer", json={"autostart": True})
    assert res.status_code == 400


def test_keep_awake_is_saved_under_phone(client, monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[anthropic]\nmodel = \"claude-opus-5\"\n", encoding="utf-8")
    monkeypatch.setattr(A, "CONFIG_PATH", path)
    monkeypatch.setattr(autostart, "state", lambda *a, **k: {"supported": False, "on": False, "current": False})
    assert client.post("/api/computer", json={"keep_awake": False}).status_code == 200
    assert "keep_awake = false" in path.read_text(encoding="utf-8")


# ---- starting in the background ------------------------------------------------------------------------

def test_starting_in_the_background_writes_to_a_log_and_opens_no_browser(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)  # main() moves into ROOT; this puts the tests back after
    monkeypatch.setattr(launch, "app_is_running", lambda: True)
    monkeypatch.setattr(launch.webbrowser, "open", opened.append)
    monkeypatch.setattr(sys, "argv", ["launch.py", "--background"])
    stdout, stderr = sys.stdout, sys.stderr
    try:
        launch.main()
    finally:
        log = sys.stdout
        sys.stdout, sys.stderr = stdout, stderr
        if log is not stdout:
            log.close()
    assert opened == []
    text = (tmp_path / "logs" / "launch.log").read_text(encoding="utf-8")
    assert "started in the background" in text and "Already running" in text
