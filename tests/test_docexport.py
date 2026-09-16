"""Tests for exporting a novel to a Google Doc.

This is the only thing in the app that writes to Google, so the tests exist mostly to pin
down what it will NOT do: it creates a document, and nothing is ever pointed at it. No
project is flipped, no source.json is rewritten, no hash is re-stamped. Asserted directly
by hashing the novel's files before and after.

The fake Docs service models one behaviour with care, because getting it wrong is what
made an earlier measurement of this feature wrong by a whole chapter: ``insertText``
inserts TEXT, not a paragraph, and Google starts a new paragraph at every newline. The
fake splits on newlines for that reason, so a round trip through it is a real test of the
round trip rather than a restatement of the code's own assumptions.

Runs under pytest (``pytest tests/``) and standalone.
"""

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import server.docexport as dx  # noqa: E402
import server.importing as importing  # noqa: E402
import server.projects as pj  # noqa: E402
import server.series as series_mod  # noqa: E402
from translation_bot import google_auth as ga  # noqa: E402

BODY = ("She lifted her brows at the sudden movement, as if burned by it. "
        "A hint of expectation shone there, and then it was gone again. " * 4)


@pytest.fixture(autouse=True)
def library(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    (tmp_path / "adapters").mkdir()
    monkeypatch.setattr(pj, "PROJECTS_DIR", root)
    series_mod._invalidate_index()
    return root


def _records(n=3):
    return [{"title": f"Chapter {i}",
             "html": f"<p>Opening of chapter {i}.</p><p>{BODY}</p>",
             "remote_id": f"id-{i}", "paid": False, "coins": 1, "state": "public"}
            for i in range(n, 0, -1)]


# ---- a Docs service that behaves like the real one ------------------------


class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeDocs:
    """Enough of the Docs service to drive an export, plus a record of what it was asked.

    ``insertText`` splits on newlines into separate paragraphs, exactly as Google does.
    """

    def __init__(self):
        self.made = {}
        self.created_titles = []
        self._n = 0

    def documents(self):
        return self

    def create(self, body):
        def go():
            self._n += 1
            doc_id = f"doc{self._n}"
            self.made[doc_id] = {"title": body["title"], "tabs": []}
            self.created_titles.append(body["title"])
            return {"documentId": doc_id, "title": body["title"]}
        return _Call(go)

    def batchUpdate(self, documentId, body):
        def go():
            doc = self.made[documentId]
            replies = []
            for req in body["requests"]:
                if "addDocumentTab" in req:
                    props = req["addDocumentTab"]["tabProperties"]
                    tab_id = f"{documentId}-t{len(doc['tabs']) + 1}"
                    doc["tabs"].append({"id": tab_id, "title": props["title"], "text": ""})
                    replies.append(
                        {"addDocumentTab": {"tabProperties": {"tabId": tab_id}}})
                elif "insertText" in req:
                    loc = req["insertText"]["location"]
                    tab = next(t for t in doc["tabs"] if t["id"] == loc["tabId"])
                    # Index 1 is the start of the tab body.
                    tab["text"] = req["insertText"]["text"] + tab["text"]
                    replies.append({})
            return {"replies": replies}
        return _Call(go)

    def get(self, documentId, includeTabsContent=False):
        def go():
            doc = self.made[documentId]
            return {"tabs": [{
                "tabProperties": {"title": t["title"]},
                "documentTab": {"body": {"content": [
                    # One paragraph per newline -- the behaviour that matters.
                    {"paragraph": {"elements": [{"textRun": {"content": line + "\n"}}]}}
                    for line in t["text"].split("\n")
                ]}},
            } for t in doc["tabs"]]}
        return _Call(go)


def _token(tmp_path, scopes):
    import json
    path = tmp_path / "token.json"
    path.write_text(json.dumps({
        "token": "t", "refresh_token": "r", "client_id": "i", "client_secret": "s",
        "token_uri": "https://oauth2.googleapis.com/token", "scopes": scopes,
        "expiry": "2099-01-01T00:00:00.000000Z",
    }), encoding="utf-8")
    return ga._load_token(path)


def _fingerprint(pid):
    """Every file of the novel, hashed. The export must not change any of them."""
    root = pj.PROJECTS_DIR / pid
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


# ---- planning, which writes nothing ---------------------------------------


def test_the_plan_counts_what_would_be_written():
    out = importing.import_novel("Novel", _records(4))
    plan = dx.plan(out["pid"])
    assert plan["chapters"] == 4
    assert plan["documents"] == 1
    assert plan["chars"] > 0
    assert plan["name"] == "Novel"


def test_a_novel_too_big_for_one_document_is_planned_as_several(monkeypatch):
    out = importing.import_novel("Novel", _records(6))
    monkeypatch.setattr(dx.docsmake, "DOC_CHAR_CAP", 500)
    assert dx.plan(out["pid"])["documents"] > 1


def test_the_plan_names_a_chapter_that_would_not_round_trip(monkeypatch):
    out = importing.import_novel("Novel", _records(2))
    real = pj.load_text_chapters

    def with_a_line_break(pid):
        chapters = real(pid)
        chapters[0].paragraphs[0] = "a line\nand another"
        return chapters
    monkeypatch.setattr(pj, "load_text_chapters", with_a_line_break)
    losses = dx.plan(out["pid"], content="source")["losses"]
    assert len(losses) == 1 and losses[0]["reads_back_as"] > losses[0]["paragraphs"]


def test_exporting_nothing_is_refused_rather_than_creating_an_empty_document():
    project = pj.create_text_project("Empty", [])
    with pytest.raises(ValueError):
        dx.plan(project["id"])


# ---- the export itself ----------------------------------------------------


def test_one_document_is_created_and_filled():
    out = importing.import_novel("Novel", _records(3))
    docs = FakeDocs()
    result = dx.export_novel(out["pid"], docs)
    assert len(result["documents"]) == 1
    doc = docs.made["doc1"]
    assert len(doc["tabs"]) == 3
    assert all(t["text"] for t in doc["tabs"])


def test_each_tab_is_named_by_its_resolved_chapter_number():
    # Not by the tab title. Every real tab in this library is called "Tab N", where N is
    # creation order rather than the chapter, so carrying those across would produce a
    # document whose contents page means nothing.
    out = importing.import_novel("Novel", _records(3))
    docs = FakeDocs()
    dx.export_novel(out["pid"], docs)
    assert [t["title"] for t in docs.made["doc1"]["tabs"]] \
        == ["Chapter 1", "Chapter 2", "Chapter 3"]


def test_the_document_is_named_for_the_novel_and_what_is_in_it():
    out = importing.import_novel("Novel", _records(2))
    docs = FakeDocs()
    dx.export_novel(out["pid"], docs, content="source")
    assert "Novel" in docs.created_titles[0] and "source" in docs.created_titles[0]


def test_a_long_novel_spans_several_documents_and_loses_nothing(monkeypatch):
    out = importing.import_novel("Novel", _records(6))
    monkeypatch.setattr(dx.docsmake, "DOC_CHAR_CAP", 500)
    docs = FakeDocs()
    result = dx.export_novel(out["pid"], docs)
    assert len(result["documents"]) > 1
    titles = [t["title"] for d in docs.made.values() for t in d["tabs"]]
    assert titles == [f"Chapter {i}" for i in range(1, 7)]
    # Each document says which part it is, so they are not indistinguishable in Drive.
    assert all("of" in name for name in docs.created_titles)


def test_the_text_survives_the_round_trip():
    # Through the fake's real newline behaviour, not around it.
    out = importing.import_novel("Novel", _records(3))
    docs = FakeDocs()
    result = dx.export_novel(out["pid"], docs)
    assert result["documents"][0]["check"]["ok"] is True


def test_a_paragraph_with_a_line_break_is_reported_after_the_fact(monkeypatch):
    out = importing.import_novel("Novel", _records(2))
    real = pj.load_text_chapters

    def with_a_line_break(pid):
        chapters = real(pid)
        chapters[0].paragraphs[0] = "a line\nand another"
        return chapters
    monkeypatch.setattr(pj, "load_text_chapters", with_a_line_break)
    docs = FakeDocs()
    result = dx.export_novel(out["pid"], docs, content="source")
    check = result["documents"][0]["check"]
    # Reported, but not a problem: nothing points at an export, so this costs a paragraph
    # mark in a document nobody reads back.
    assert check["differed"] and check["problems"] == []


# ---- what it must never do -----------------------------------------------


def test_the_export_does_not_touch_the_novel():
    # The whole safety argument in one assertion. project.json, source.json, state.json
    # and every chapter file byte-identical afterwards.
    out = importing.import_novel("Novel", _records(4))
    before = _fingerprint(out["pid"])
    dx.export_novel(out["pid"], FakeDocs())
    assert _fingerprint(out["pid"]) == before


def test_a_read_only_token_is_refused_before_anything_is_created(tmp_path):
    out = importing.import_novel("Novel", _records(3))
    docs = FakeDocs()
    creds = _token(tmp_path, ["https://www.googleapis.com/auth/documents.readonly"])
    with pytest.raises(ga.MissingScope):
        dx.export_novel(out["pid"], docs, creds=creds)
    # The point of checking up front: no half-built document left in the user's Drive.
    assert docs.made == {}


def test_a_token_that_can_write_is_allowed_through(tmp_path):
    out = importing.import_novel("Novel", _records(2))
    docs = FakeDocs()
    creds = _token(tmp_path, ["https://www.googleapis.com/auth/documents.readonly",
                              "https://www.googleapis.com/auth/drive.file"])
    result = dx.export_novel(out["pid"], docs, creds=creds)
    assert len(result["documents"]) == 1


def test_a_document_that_cannot_be_read_back_is_reported_not_raised():
    # An export that cannot be verified is still an export. Claiming it worked without
    # looking is the outcome worth avoiding.
    out = importing.import_novel("Novel", _records(2))

    class Broken(FakeDocs):
        def get(self, documentId, includeTabsContent=False):
            return _Call(lambda: (_ for _ in ()).throw(RuntimeError("no")))

    result = dx.export_novel(out["pid"], Broken())
    assert result["documents"][0]["check"]["ok"] is False
    assert result["documents"][0]["check"]["problems"]


# ---- the endpoints --------------------------------------------------------


@pytest.fixture
def client(monkeypatch, library):
    from fastapi.testclient import TestClient

    import server.app as A
    import server.pages as P
    from translation_bot.config import Config

    monkeypatch.setattr(P, "PROJECTS_DIR", pj.PROJECTS_DIR)
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()
    # The remote-access gate exempts loopback, and TestClient's default client host is
    # the literal string "testclient", which is not one.
    #
    # raise_server_exceptions=False because the app routes EVERY error through one
    # exception handler so the frontend always receives the same `detail` shape. A real
    # client gets a 403 with that payload; without this flag TestClient re-raises instead
    # and the response the browser would actually see is never tested.
    return TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000),
                      raise_server_exceptions=False)


def test_the_plan_endpoint_writes_nothing(client):
    out = importing.import_novel("Novel", _records(3))
    before = _fingerprint(out["pid"])
    r = client.get(f"/api/projects/{out['pid']}/export/doc")
    assert r.status_code == 200
    assert r.json()["chapters"] == 3
    assert _fingerprint(out["pid"]) == before


def test_the_plan_endpoint_says_whether_it_could_actually_write(client):
    out = importing.import_novel("Novel", _records(2))
    # No token at all in the test config, so the honest answer is no. The UI needs this
    # to be separate from being signed in: every existing token is signed in and
    # read-only, so offering an export behind the same green dot promises something that
    # fails at the last step.
    assert client.get(f"/api/projects/{out['pid']}/export/doc").json()["can_write"] is False


def test_exporting_an_unknown_novel_is_a_clean_400(client):
    assert client.get("/api/projects/aaaaaaaaaaaa/export/doc").status_code == 400


def test_the_export_endpoint_refuses_a_read_only_token(client, monkeypatch, tmp_path):
    import server.app as A
    out = importing.import_novel("Novel", _records(2))
    creds = _token(tmp_path, ["https://www.googleapis.com/auth/documents.readonly"])
    monkeypatch.setattr(A, "load_saved_credentials", lambda _tf: creds)
    made = []
    monkeypatch.setattr(A, "build_docs_service", lambda _c: made.append(1))
    r = client.post(f"/api/projects/{out['pid']}/export/doc")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "google-scope"
    # Refused before a service was even built, let alone a document created.
    assert made == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
