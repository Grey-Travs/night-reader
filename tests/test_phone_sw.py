"""The phone app's offline copy: its worker is served as a script, never as the app's
page; it can be switched off from the computer; and a change to it rebuilds the app.

Runs under pytest (``pytest tests/``).
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import launch  # noqa: E402
import server.app as A  # noqa: E402


@pytest.fixture
def client():
    return TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000))


def test_the_worker_is_served_as_a_script_that_is_always_checked(client, monkeypatch, tmp_path):
    (tmp_path / "phone-sw.js").write_text("self.addEventListener('fetch', () => {})", encoding="utf-8")
    monkeypatch.setattr(A, "DIST_DIR", tmp_path)
    res = client.get("/phone-sw.js")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/javascript")
    assert res.headers["cache-control"] == "no-cache"
    assert "addEventListener" in res.text


def test_a_missing_worker_is_a_404_not_the_app_page(client, monkeypatch, tmp_path):
    monkeypatch.setattr(A, "DIST_DIR", tmp_path)
    assert client.get("/phone-sw.js").status_code == 404


def test_the_worker_can_be_switched_off_from_the_computer(client, monkeypatch, tmp_path):
    (tmp_path / "phone-sw.js").write_text("// the real one", encoding="utf-8")
    monkeypatch.setattr(A, "DIST_DIR", tmp_path)
    monkeypatch.setenv("NR_PHONE_SW", "off")
    res = client.get("/phone-sw.js")
    assert res.status_code == 200 and "unregister()" in res.text
    assert "the real one" not in res.text


def test_a_change_to_the_worker_rebuilds_the_app(monkeypatch, tmp_path):
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "public").mkdir()
    dist = web / "dist"
    dist.mkdir()
    (web / "src" / "main.jsx").write_text("x", encoding="utf-8")
    worker = web / "public" / "phone-sw.js"
    worker.write_text("v1", encoding="utf-8")
    built = dist / "index.html"
    built.write_text("<html>", encoding="utf-8")
    later = time.time() + 5
    os.utime(built, (later, later))
    monkeypatch.setattr(launch, "WEB", web)
    monkeypatch.setattr(launch, "DIST", dist)
    assert launch._build_is_stale() is False
    os.utime(worker, (later + 5, later + 5))
    assert launch._build_is_stale() is True
