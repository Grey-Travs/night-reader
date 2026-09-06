"""Tests for the scanned-page manifest (projects/<pid>/pages.json).

An image novel's pages are ordered, re-orderable, individually re-readable, and
addressed by a server-minted id. The manifest is the only thing that maps a page id
to a file on disk, so it is also the security boundary for serving those images back.

What has to hold:
- An uploaded file's type is decided by its BYTES, never by the client's header or
  filename. A path is never built from a URL segment.
- A page's filename carries its sequence number, not its position, so reordering
  never renames a file (which would break image caching and race an in-flight OCR
  call holding that path).
- A corrupt manifest reads as empty rather than raising — one bad file must not take
  down the novel, the same rule State/Glossary/project.json already follow.
- A reorder that isn't a permutation is refused, because a partial list would
  silently drop pages.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_pages_store.py``).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import server.pages as P  # noqa: E402

PID = "abcdef012345"

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBP" + b"\x00" * 32
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32


@pytest.fixture
def project(monkeypatch, tmp_path):
    """Redirect the projects root at a temp dir and create one image project."""
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path)
    (tmp_path / PID / P.PAGES_DIRNAME).mkdir(parents=True)
    return tmp_path / PID


def _add(doc, *, ext="jpg", digest="d0", batch="b1", name=""):
    return P.add_page(doc, ext=ext, data_len=1024, digest=digest, batch=batch, name=name)


# ---- the bytes decide, not the client ----------------------------------------

def test_supported_formats_are_recognised_by_magic_bytes():
    assert P.sniff_image(JPEG) == "jpg"
    assert P.sniff_image(PNG) == "png"
    assert P.sniff_image(WEBP) == "webp"


def test_unsupported_formats_are_rejected():
    for blob in (HEIC, PDF, GIF, b"not an image at all"):
        assert P.sniff_image(blob) is None, "only real JPEG/PNG/WebP may be stored"


def test_heic_gets_the_iphone_explanation():
    """iPhones shoot HEIC by default; browsers can't display it, so the fix has to
    be actionable rather than a bare 'unsupported format'."""
    reason = P.unsupported_reason(HEIC)
    assert "iPhone" in reason and "Most Compatible" in reason


def test_pdf_and_gif_get_their_own_explanations():
    assert "PDF" in P.unsupported_reason(PDF)
    assert "GIF" in P.unsupported_reason(GIF)


def test_a_lying_content_type_cannot_smuggle_a_file_in():
    """The upload endpoint trusts sniff_image, not the header — so a PDF announced
    as image/jpeg still has no extension and cannot be stored."""
    assert P.sniff_image(PDF) is None


# ---- filenames and ordering --------------------------------------------------

def test_filenames_come_from_the_sequence_not_the_client():
    doc = P.new_doc()
    page = _add(doc, name="../../etc/passwd.jpg")
    assert page["file"] == "page-0001.jpg", "the on-disk name is always server-minted"
    assert "/" not in page["file"] and "\\" not in page["file"]


def test_client_filename_survives_only_as_a_display_label():
    doc = P.new_doc()
    page = _add(doc, name="../../secret/IMG_0042.jpg")
    assert "/" not in page["name"] and "\\" not in page["name"], \
        "a label must never contain path separators"
    assert "IMG_0042" in page["name"], "the useful part of the name is still shown"


def test_sequence_is_monotonic_across_deletes():
    """Reusing a freed number would collide with an image still cached in a browser."""
    doc = P.new_doc()
    first = _add(doc, digest="a")
    second = _add(doc, digest="b")
    P.delete_pages(doc, [second["id"]])
    third = _add(doc, digest="c")
    assert third["seq"] > second["seq"], "sequence numbers are never reused"
    assert third["file"] != second["file"]
    assert first["file"] == "page-0001.jpg"


def test_reorder_moves_records_without_renaming_files(project):
    doc = P.new_doc()
    a = _add(doc, digest="a")
    b = _add(doc, digest="b")
    files_before = {p["id"]: p["file"] for p in doc["pages"]}

    assert P.reorder(doc, [b["id"], a["id"]]) is True
    assert [p["id"] for p in doc["pages"]] == [b["id"], a["id"]], "order follows the request"
    assert {p["id"]: p["file"] for p in doc["pages"]} == files_before, \
        "reordering must never rename a file"


def test_reorder_refuses_a_partial_list():
    doc = P.new_doc()
    a = _add(doc, digest="a")
    _add(doc, digest="b")
    assert P.reorder(doc, [a["id"]]) is False, "a partial list would silently drop a page"
    assert len(doc["pages"]) == 2, "the manifest is untouched on refusal"


def test_reorder_refuses_unknown_ids():
    doc = P.new_doc()
    _add(doc, digest="a")
    assert P.reorder(doc, ["ffffffff"]) is False


# ---- duplicate uploads -------------------------------------------------------

def test_the_same_photo_is_recognised_by_hash():
    """Re-dropping a folder must be a no-op, not a doubled novel."""
    doc = P.new_doc()
    _add(doc, digest="samehash")
    assert P.find_by_hash(doc, "samehash") is not None
    assert P.find_by_hash(doc, "otherhash") is None


# ---- serving images back -----------------------------------------------------

def test_resolve_rejects_a_malformed_page_id(project):
    for bad in ("", "../../etc", "zzzz", "a" * 40, "../pages.json"):
        assert P.resolve_page_file(PID, bad) is None, f"{bad!r} must not resolve"


def test_resolve_returns_the_real_file(project):
    doc = P.new_doc()
    page = _add(doc)
    (project / P.PAGES_DIRNAME / page["file"]).write_bytes(JPEG)
    P.save_pages(PID, doc)

    resolved = P.resolve_page_file(PID, page["id"])
    assert resolved is not None and resolved.name == page["file"]


def test_resolve_refuses_a_manifest_pointing_outside_the_pages_folder(project):
    """Belt-and-braces against a hand-edited or tampered manifest."""
    doc = P.new_doc()
    page = _add(doc)
    page["file"] = "../project.json"
    (project / "project.json").write_text("{}", encoding="utf-8")
    P.save_pages(PID, doc)
    assert P.resolve_page_file(PID, page["id"]) is None


def test_resolve_returns_none_when_the_file_is_gone(project):
    doc = P.new_doc()
    page = _add(doc)
    P.save_pages(PID, doc)
    assert P.resolve_page_file(PID, page["id"]) is None, "a listed but missing file is not served"


# ---- durability --------------------------------------------------------------

def test_missing_manifest_reads_as_empty(project):
    doc = P.load_pages(PID)
    assert doc["pages"] == [] and doc["next_seq"] == 1


def test_corrupt_manifest_reads_as_empty_instead_of_raising(project):
    P.pages_file(PID).write_text("{not json at all", encoding="utf-8")
    doc = P.load_pages(PID)
    assert doc["pages"] == [], "a truncated manifest must not take down the novel"


def test_manifest_round_trips_korean_text(project):
    doc = P.new_doc()
    page = _add(doc)
    page["text"] = "그는 천천히 문을 열었다."
    P.save_pages(PID, doc)

    reloaded = P.load_pages(PID)
    assert P.find_page(reloaded, page["id"])["text"] == "그는 천천히 문을 열었다."
    raw = P.pages_file(PID).read_text(encoding="utf-8")
    assert "그는" in raw, "Korean is stored readably, not escaped"


def test_next_seq_is_recovered_from_the_pages_when_absent(project):
    """An older or hand-edited manifest must not restart numbering at 1."""
    P.pages_file(PID).write_text(
        json.dumps({"pages": [{"id": "aaaaaaaa", "seq": 7, "file": "page-0007.jpg"}]}),
        encoding="utf-8")
    assert P.load_pages(PID)["next_seq"] == 8


def test_mutate_pages_persists(project):
    with P.mutate_pages(PID) as doc:
        _add(doc, digest="x")
    assert len(P.load_pages(PID)["pages"]) == 1


# ---- payload shaping ---------------------------------------------------------

def test_summary_omits_page_text():
    """400 pages of Korean would be several MB on every tab switch."""
    doc = P.new_doc()
    page = _add(doc)
    page["text"] = "그는 천천히 문을 열었다."
    out = P.summary(doc)
    row = out["pages"][0]
    assert "text" not in row and "raw_text" not in row
    assert row["chars"] == len(page["text"]), "the length still reaches the UI"
    assert row["has_text"] is True


def test_counts_report_every_status():
    doc = P.new_doc()
    _add(doc, digest="a")
    _add(doc, digest="b")["status"] = P.STATUS_NEEDS_CHECK
    out = P.counts(doc)
    assert out["total"] == 2
    assert out[P.STATUS_NEW] == 1 and out[P.STATUS_NEEDS_CHECK] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
