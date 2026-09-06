"""Tests for what travels inside a portable novel bundle.

A photographed novel carries hundreds of page images. Putting them in every backup
would make an ordinary library export run to gigabytes, so images are opt-in — but
the EXTRACTED TEXT must always travel, because that is what makes a novel readable
and translatable on the other device.

What has to hold:
- ``pages.json`` (the transcribed Korean) is always in the bundle.
- Page images are excluded unless explicitly requested.
- ``variants/`` (per-paragraph edit history) travels, so moving a novel doesn't throw
  away the user's rewrites.
- The bundle is written to a FILE, not assembled in memory — an image novel would
  otherwise need the whole archive resident in RAM before the download starts.
- A bundle still round-trips through import.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_bundle_images.py``).
"""

import json
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import server.projects as pj  # noqa: E402

PID = "abcdef012345"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 512


@pytest.fixture
def library(monkeypatch, tmp_path):
    """A temp projects root holding one image novel with a page and a transcription."""
    root = tmp_path / "projects"
    monkeypatch.setattr(pj, "PROJECTS_DIR", root)
    pdir = root / PID
    (pdir / "pages").mkdir(parents=True)
    (pdir / "chapters").mkdir()
    (pdir / "variants").mkdir()

    (pdir / "project.json").write_text(
        json.dumps({"id": PID, "name": "Scanned novel", "source_type": "images"}),
        encoding="utf-8")
    (pdir / "pages.json").write_text(
        json.dumps({"version": 1, "pages": [
            {"id": "aaaaaaaa", "seq": 1, "file": "page-0001.jpg",
             "text": "그는 천천히 문을 열었다."}]}, ensure_ascii=False),
        encoding="utf-8")
    (pdir / "pages" / "page-0001.jpg").write_bytes(JPEG)
    (pdir / "variants" / "chapter-01.json").write_text(
        json.dumps({"version": 1, "chapter": 1, "groups": []}), encoding="utf-8")
    return root


def _names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as z:
        return z.namelist()


# ---- what travels ------------------------------------------------------------

def test_extracted_text_always_travels(library):
    names = _names(pj.export_bundle([PID]))
    assert f"{PID}/pages.json" in names, \
        "the transcribed text must always travel, or the novel arrives unreadable"


def test_images_are_excluded_by_default(library):
    names = _names(pj.export_bundle([PID]))
    assert not any(n.startswith(f"{PID}/pages/") for n in names), \
        "an ordinary backup must not carry hundreds of photos"


def test_images_travel_when_asked_for(library):
    names = _names(pj.export_bundle([PID], include_images=True))
    assert f"{PID}/pages/page-0001.jpg" in names, "opting in must actually include them"


def test_paragraph_variants_travel(library):
    names = _names(pj.export_bundle([PID]))
    assert f"{PID}/variants/chapter-01.json" in names, \
        "per-paragraph edit history must survive a move between devices"


def test_previous_translations_travel(library):
    """previous/ is the one-click revert. It was missing from the bundle, so moving a
    novel dropped every revert point and "Compare previous" found nothing."""
    (library / PID / "previous").mkdir(exist_ok=True)
    (library / PID / "previous" / "chapter-01.md").write_text(
        "the translation before the last overwrite", encoding="utf-8")

    names = _names(pj.export_bundle([PID]))
    assert f"{PID}/previous/chapter-01.md" in names


def test_superseded_repair_artifacts_do_not_travel(library):
    """tools/repair_library.py parks an older, stranded generation of a chapter in
    ``.superseded/`` rather than deleting it. Those files stay on disk so the repair
    is reversible — but a dead second copy of every chapter has no business riding
    along in every backup and every move to another device."""
    aside = library / PID / "chapters" / ".superseded"
    aside.mkdir(parents=True)
    (aside / "chapter-01.md").write_text("a superseded translation", encoding="utf-8")
    (library / PID / "chapters" / "chapter-001.md").write_text("the live one",
                                                               encoding="utf-8")

    names = _names(pj.export_bundle([PID]))
    assert f"{PID}/chapters/chapter-001.md" in names, "the live chapter still travels"
    assert not any(".superseded" in n for n in names)


def test_a_google_doc_novel_round_trips(library):
    """Every bundle test used an images-only fixture, so nothing covered the shape all
    52 real novels actually have: a source_doc_id, chapters, and no pages at all."""
    import shutil

    pdir = library / "0123456789ab"
    (pdir / "chapters").mkdir(parents=True)
    (pdir / "previous").mkdir()
    (pdir / "project.json").write_text(
        json.dumps({"id": "0123456789ab", "name": "From a Google Doc",
                    "source_doc_id": "1AbCdEf", "chapter_count": 2}), encoding="utf-8")
    (pdir / "chapters" / "chapter-01.md").write_text("Chapter one.", encoding="utf-8")
    (pdir / "previous" / "chapter-01.md").write_text("An older chapter one.", encoding="utf-8")
    (pdir / "state.json").write_text(
        json.dumps({"chapters": {"1": {"status": "validated"}}}), encoding="utf-8")

    data = pj.export_bundle(["0123456789ab"]).read_bytes()
    shutil.rmtree(pdir)

    assert [p["id"] for p in pj.import_bundle(data)] == ["0123456789ab"]
    assert (pdir / "chapters" / "chapter-01.md").read_text(encoding="utf-8") == "Chapter one."
    assert (pdir / "previous" / "chapter-01.md").exists(), "revert points must survive"
    restored = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    assert restored["source_doc_id"] == "1AbCdEf", "the novel must still know its document"


def test_bundle_has_images_reports_what_can_be_offered(library):
    assert pj.bundle_has_images([PID]) is True
    assert pj.bundle_has_images(["ffffffffffff"]) is False, "a novel with no photos offers none"


# ---- how it is written -------------------------------------------------------

def test_bundle_is_written_to_a_file_not_held_in_memory(library):
    path = pj.export_bundle([PID], include_images=True)
    assert isinstance(path, Path), "the bundle is a file on disk"
    assert path.is_file() and path.stat().st_size > 0
    assert zipfile.is_zipfile(path), "and it is a real zip"


def test_images_are_stored_uncompressed(library):
    """JPEG/PNG are already compressed — deflating them again burns CPU across
    hundreds of files to save almost nothing."""
    path = pj.export_bundle([PID], include_images=True)
    with zipfile.ZipFile(path) as z:
        info = z.getinfo(f"{PID}/pages/page-0001.jpg")
        assert info.compress_type == zipfile.ZIP_STORED
        text = z.getinfo(f"{PID}/pages.json")
        assert text.compress_type == zipfile.ZIP_DEFLATED, "text is still compressed"


def test_a_caller_supplied_destination_is_honoured(library, tmp_path):
    dest = tmp_path / "mine.zip"
    assert pj.export_bundle([PID], dest=dest) == dest
    assert dest.is_file()


# ---- round trip --------------------------------------------------------------

def test_bundle_round_trips_through_import(library):
    data = pj.export_bundle([PID], include_images=True).read_bytes()
    # Drop the original so the import restores rather than colliding.
    import shutil
    shutil.rmtree(library / PID)

    imported = pj.import_bundle(data)
    assert [p["id"] for p in imported] == [PID], "the novel keeps its identity"

    restored = json.loads((library / PID / "pages.json").read_text(encoding="utf-8"))
    assert restored["pages"][0]["text"] == "그는 천천히 문을 열었다.", \
        "Korean survives the round trip"
    assert (library / PID / "pages" / "page-0001.jpg").read_bytes() == JPEG


def test_text_only_bundle_restores_a_usable_novel(library):
    """The common case: photos stayed behind, but the novel still works."""
    data = pj.export_bundle([PID]).read_bytes()
    import shutil
    shutil.rmtree(library / PID)

    pj.import_bundle(data)
    restored = json.loads((library / PID / "pages.json").read_text(encoding="utf-8"))
    assert restored["pages"][0]["text"], "the transcription is what makes it translatable"
    assert not (library / PID / "pages" / "page-0001.jpg").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
