"""Every glossary writer has to hold the glossary lock.

``glossary.json`` and ``glossary_pending.json`` are load → modify → save from two
directions at once: the user approving/editing terms in the browser, and the
translation worker calling ``queue_new_terms`` on every finished chapter, on a
threadpool thread. With nothing ordering them, whichever saved last silently
discarded the other's work — a chapter's newly found names vanished because a term
was approved at the same moment. Nothing raises; the terms are simply gone, and the
novel's spellings drift from the next chapter onward.

Two different properties are covered here, because two different bugs were possible:

1. **The save happens inside the lock.** Checked by trying to take the same lock from
   another thread at the moment the endpoint writes. Driven over every writing
   endpoint, so a new one added later without a lock fails here.

2. **An endpoint that calls the model re-loads afterwards.** ``learn``,
   ``detect-pronouns`` and ``bulk-add`` read the glossary, call Claude for minutes,
   then save. Holding the lock across the call would stall the translation worker, so
   these must instead apply their result to a FRESHLY loaded glossary. Saving the
   pre-call snapshot would delete every term the worker queued while they waited —
   the lock alone does not prevent that.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_glossary_race.py``).
"""

import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.app as A  # noqa: E402
import server.pages as P  # noqa: E402
import server.projects as pj  # noqa: E402
import server.variants as V  # noqa: E402
from translation_bot.config import Config  # noqa: E402
from translation_bot.glossary import Glossary, GlossaryEntry, glossary_lock  # noqa: E402
from translation_bot.translator import Translator  # noqa: E402

KOREAN = "그는 천천히 문을 열었다.\n\n밖에는 아무도 없었다."
ENGLISH = "He opened the door slowly.\n\nThere was no one outside."


@pytest.fixture
def client(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    for module in (pj, P, V):
        monkeypatch.setattr(module, "PROJECTS_DIR", root)
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()
    A._offline_projects.clear()
    A._jobs.clear()
    A._active_job_by_project.clear()
    return TestClient(A.app, base_url="http://localhost",
                      client=("127.0.0.1", 50000))


@pytest.fixture
def novel(client):
    pid = client.post("/api/projects/text", json={
        "name": "Test novel", "text": KOREAN, "split_mode": "single"}).json()["id"]
    cfg = pj.project_config(Config(), pj.get_project(pid))
    g = Glossary([])
    g.add(GlossaryEntry(korean="고원", english="Go Won", type="name"))
    g.add(GlossaryEntry(korean="승연", english="Seung Yeon", type="name"))
    g.save(cfg.paths.glossary_json, cfg.paths.glossary_md)
    return pid, cfg


def _terms(cfg) -> set[str]:
    return {e.english for e in Glossary.load(cfg.paths.glossary_json).entries()}


def _worker_queues_a_term(cfg, english="Worker Found Me"):
    """What the translation worker does mid-chapter: add a term and save.

    Deliberately a plain load → add → save on the file, because that is the shape the
    endpoint has to survive — not a call into code the endpoint also uses.
    """
    g = Glossary.load(cfg.paths.glossary_json)
    g.add(GlossaryEntry(korean="", english=english, type="name"))
    g.save(cfg.paths.glossary_json, cfg.paths.glossary_md)


# ---- 1. the save is inside the lock ------------------------------------------

def _held_by_someone(lock) -> bool:
    """Whether `lock` is currently held.

    Probed from a DIFFERENT thread on purpose: it is an RLock, so a non-blocking
    acquire on the thread that already holds it succeeds and proves nothing.
    """
    got = []

    def probe():
        acquired = lock.acquire(blocking=False)
        got.append(acquired)
        if acquired:
            lock.release()

    t = threading.Thread(target=probe)
    t.start()
    t.join(timeout=5)
    assert got, "the probe thread never ran"
    return not got[0]


class _LockWatcher:
    """Records which glossary locks were held at the moment the glossary was saved.

    The LAST save, deliberately. Some of these tests simulate the worker saving
    mid-request, and that save is correctly unlocked — watching the first one would
    measure the stand-in instead of the endpoint.
    """

    def __init__(self, cfg, *also):
        self.locks = [glossary_lock(c.paths.glossary_json) for c in (cfg, *also)]
        self.held_at_last_save = None

    def watch(self, monkeypatch):
        real_save = Glossary.save
        watcher = self

        def spy(self, json_path, md_path=None):
            watcher.held_at_last_save = [_held_by_someone(l) for l in watcher.locks]
            return real_save(self, json_path, md_path)

        monkeypatch.setattr(Glossary, "save", spy)


WRITERS = [
    ("term", lambda pid: (f"/api/projects/{pid}/glossary/term",
                          {"korean": "새말", "english": "New Word", "type": "term"})),
    ("term/delete", lambda pid: (f"/api/projects/{pid}/glossary/term/delete",
                                 {"korean": "고원"})),
    ("term/delete-bulk", lambda pid: (f"/api/projects/{pid}/glossary/term/delete-bulk",
                                      {"terms": [{"korean": "고원", "english": ""}]})),
    ("import", lambda pid: (f"/api/projects/{pid}/glossary/import",
                            {"entries": [{"korean": "수입", "english": "Imported",
                                          "type": "term"}], "mode": "merge"})),
    ("review", lambda pid: (f"/api/projects/{pid}/glossary/review",
                            {"approve": [{"korean": "승인", "english": "Approved",
                                          "type": "term"}], "reject": []})),
]


@pytest.mark.parametrize("name,build", WRITERS, ids=[w[0] for w in WRITERS])
def test_every_glossary_write_happens_under_the_lock(client, novel, monkeypatch,
                                                     name, build):
    pid, cfg = novel
    watcher = _LockWatcher(cfg)
    watcher.watch(monkeypatch)

    url, body = build(pid)
    assert client.post(url, json=body).status_code == 200, f"{name} did not succeed"
    assert watcher.held_at_last_save == [True], \
        f"{name} saved the glossary without holding the lock"


def test_copy_locks_the_destination_but_not_the_source(client, novel, monkeypatch):
    """Copying reads another novel's glossary. That read needs no lock — and taking two
    glossary locks at once would be the one place in this app a deadlock could be
    built."""
    pid, cfg = novel
    src = client.post("/api/projects/text", json={
        "name": "Source novel", "text": KOREAN, "split_mode": "single"}).json()["id"]
    src_cfg = pj.project_config(Config(), pj.get_project(src))
    g = Glossary([])
    g.add(GlossaryEntry(korean="원본", english="From Source", type="term"))
    g.save(src_cfg.paths.glossary_json, src_cfg.paths.glossary_md)

    # Watches BOTH locks, so the assertion below is about what the endpoint actually
    # holds rather than about what it was written to hold.
    watcher = _LockWatcher(cfg, src_cfg)
    watcher.watch(monkeypatch)

    r = client.post(f"/api/projects/{pid}/glossary/copy",
                    json={"source_pid": src, "mode": "merge"})

    assert r.status_code == 200
    assert "From Source" in _terms(cfg)
    assert watcher.held_at_last_save == [True, False], \
        "the destination write must be locked, and the source lock must not be held too"


# ---- 2. a model call means re-loading afterwards ------------------------------

def test_bulk_add_keeps_terms_queued_while_the_paste_was_classified(client, novel,
                                                                    monkeypatch):
    pid, cfg = novel

    def _classify(self, terms):
        # The worker finishes a chapter while the model is classifying this paste.
        _worker_queues_a_term(cfg)
        return {t.lower(): "name" for t in terms}

    monkeypatch.setattr(Translator, "classify_terms", _classify)

    r = client.post(f"/api/projects/{pid}/glossary/bulk-add",
                    json={"entries": [{"english": "Needs A Type"}]})

    assert r.status_code == 200
    after = _terms(cfg)
    assert "Needs A Type" in after, "the pasted term must be saved"
    assert "Worker Found Me" in after, \
        "a term queued during the model call was discarded by the endpoint's save"
    assert {"Go Won", "Seung Yeon"} <= after, "existing terms must survive"


def test_learn_keeps_terms_queued_while_the_model_read_the_chapters(client, novel,
                                                                    monkeypatch):
    pid, cfg = novel
    cfg.paths.output_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(A, "classify", lambda ch, cfg: "english")

    def _extract(self, sample):
        _worker_queues_a_term(cfg)
        return [{"english": "Learned Name", "type": "name"}]

    monkeypatch.setattr(Translator, "extract_glossary", _extract)
    watcher = _LockWatcher(cfg)
    watcher.watch(monkeypatch)

    r = client.post(f"/api/projects/{pid}/glossary/learn")

    assert r.status_code == 200, r.text
    after = _terms(cfg)
    assert "Learned Name" in after
    assert "Worker Found Me" in after, \
        "a term queued during the model call was discarded by the endpoint's save"
    # learn already loaded after the model call, so the lock is the whole fix here —
    # without this the endpoint would have no coverage of the change at all.
    assert watcher.held_at_last_save == [True], "learn saved without holding the lock"


def test_detect_pronouns_keeps_terms_queued_during_detection(client, novel, monkeypatch):
    pid, cfg = novel
    cfg.paths.output_dir.mkdir(parents=True, exist_ok=True)
    (cfg.paths.output_dir / "chapter-01.md").write_text(ENGLISH, encoding="utf-8")

    def _detect(self, names, sample):
        _worker_queues_a_term(cfg)
        return {n.lower(): "he" for n in names}

    monkeypatch.setattr(Translator, "detect_pronouns", _detect)

    r = client.post(f"/api/projects/{pid}/glossary/detect-pronouns")

    assert r.status_code == 200, r.text
    after = _terms(cfg)
    assert "Worker Found Me" in after, \
        "a term queued during the model call was discarded by the endpoint's save"
    by_english = {e.english: e for e in Glossary.load(cfg.paths.glossary_json).entries()}
    assert by_english["Go Won"].pronoun == "he", "the detected pronoun must still land"


def test_detect_pronouns_does_not_overwrite_one_set_during_the_call(client, novel,
                                                                   monkeypatch):
    """The endpoint promises a hand-set pronoun is never overwritten. That has to hold
    ACROSS the model call, not just before it — the user is looking at the glossary
    table while this runs, which is exactly why they pressed the button."""
    pid, cfg = novel
    cfg.paths.output_dir.mkdir(parents=True, exist_ok=True)
    (cfg.paths.output_dir / "chapter-01.md").write_text(ENGLISH, encoding="utf-8")

    def _detect(self, names, sample):
        # The user types "she" for Go Won while the detection is running.
        g = Glossary.load(cfg.paths.glossary_json)
        for e in g.entries():
            if e.english == "Go Won":
                e.pronoun = "she"
        g.save(cfg.paths.glossary_json, cfg.paths.glossary_md)
        return {n.lower(): "he" for n in names}

    monkeypatch.setattr(Translator, "detect_pronouns", _detect)

    r = client.post(f"/api/projects/{pid}/glossary/detect-pronouns")

    assert r.status_code == 200, r.text
    by_english = {e.english: e for e in Glossary.load(cfg.paths.glossary_json).entries()}
    assert by_english["Go Won"].pronoun == "she", \
        "a pronoun the user set by hand must win over a detected one"
    assert not any(row["english"] == "Go Won" for row in r.json()["filled"]), \
        "and it must not be reported as filled by the detector"


def test_a_failed_classify_still_saves_the_typed_rows(client, novel, monkeypatch):
    """Typed rows never needed the model, so a classify failure must not lose them —
    the behaviour the endpoint documents, re-checked now the save moved under a lock."""
    from translation_bot.translator import TranslatorError
    pid, cfg = novel

    def _boom(self, terms):
        raise TranslatorError("no")

    monkeypatch.setattr(Translator, "classify_terms", _boom)

    r = client.post(f"/api/projects/{pid}/glossary/bulk-add", json={"entries": [
        {"english": "Typed Row", "type": "name"},
        {"english": "Untyped Row"},
    ]})

    assert r.status_code == 200, r.text
    assert "Typed Row" in _terms(cfg)
    assert r.json()["classify_error"]
    assert r.json()["unclassified"] == ["Untyped Row"]


def test_a_paste_with_nothing_to_write_leaves_the_file_alone(client, novel, monkeypatch):
    pid, cfg = novel
    before = cfg.paths.glossary_json.read_text(encoding="utf-8")
    saves = []
    real_save = Glossary.save
    monkeypatch.setattr(Glossary, "save",
                        lambda self, j, m=None: (saves.append(1), real_save(self, j, m))[1])

    r = client.post(f"/api/projects/{pid}/glossary/bulk-add",
                    json={"entries": [{"english": "Go Won", "type": "name"}]})

    assert r.status_code == 200, r.text
    assert json.loads(cfg.paths.glossary_json.read_text(encoding="utf-8")) == \
        json.loads(before), "an all-duplicate paste must not rewrite the glossary"
    assert saves == [], "and must not save at all"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
