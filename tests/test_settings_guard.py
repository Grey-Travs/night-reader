"""The rule that decides whether a posted chapter is charging what it should.

``extension/settings.js`` is consulted from two places that must agree:

* the check straight after a save, which stops the run rather than leave a chapter wrong;
* the reconcile that decides whether a half-posted chapter is already finished.

When those two disagreed, a chapter that FAILED the save check still had its text -- the
save writes text and settings together -- so on the next run it read as "finished", was
recorded as posted, and nothing ever went back to it. That is how a side story stayed free
through repeated re-runs: the first run stopped on it, and every later run called it done.

Nothing else in the suite shells out, so this skips cleanly when Node isn't on PATH; it is
a drift alarm, not a build requirement.

Runs under pytest (``pytest tests/``).
"""

import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_JS = os.path.join(REPO, "extension", "settings.js")

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is not installed")

# A save that went through cleanly, as the site reports it back.
GOOD = {"type": "notes", "state": "public", "paid": 1, "coins": 55}


def _wrong(cases: list[tuple[dict, dict]]) -> list[list[str]]:
    """Run nrSettingsWrong over (record, want) pairs in Node and bring the answers back."""
    script = (
        "const { nrSettingsWrong } = require("
        + json.dumps(SETTINGS_JS.replace(os.sep, "/"))
        + ");"
        "const cases = " + json.dumps(cases) + ";"
        "console.log(JSON.stringify(cases.map(([a, b]) => nrSettingsWrong(a, b))));"
    )
    result = subprocess.run([_NODE, "-e", script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, f"node failed: {result.stderr}"
    return json.loads(result.stdout)


def test_a_clean_save_reports_nothing():
    assert _wrong([(GOOD, {"state": "public", "coins": 55})]) == [[]]


def test_a_free_chapter_saved_free_reports_nothing():
    record = {"type": "notes", "state": "public", "paid": 0, "coins": 0}
    assert _wrong([(record, {"state": "public", "coins": 0})]) == [[]]


def test_the_failure_from_the_run_that_prompted_this():
    # Verbatim from the report: the plan asked for free, and the site came back holding a
    # coin. Exactly one thing is wrong, and the run is right to stop on it.
    record = {"type": "notes", "state": "public", "paid": 0, "coins": 1}
    assert _wrong([(record, {"state": "public", "coins": 0})]) == [["coins is 1"]]


def test_the_same_chapter_measured_against_the_price_it_should_have_had():
    # The repair path: once side stories price at 55, that same live chapter is wrong on
    # BOTH counts, and the next run has to correct it rather than call it finished.
    record = {"type": "notes", "state": "public", "paid": 0, "coins": 1}
    assert _wrong([(record, {"state": "public", "coins": 55})]) == [
        ["coins is 1", "paid is 0"]]


def test_each_field_is_reported_on_its_own():
    cases = [
        ({**GOOD, "type": "chapter"}, {"state": "public", "coins": 55}),
        ({**GOOD, "state": "private"}, {"state": "public", "coins": 55}),
        ({**GOOD, "coins": 30}, {"state": "public", "coins": 55}),
        ({**GOOD, "paid": 0}, {"state": "public", "coins": 55}),
    ]
    assert _wrong(cases) == [
        ['type is "chapter"'],
        ['state is "private"'],
        ["coins is 30"],
        ["paid is 0"],
    ]


def test_the_comparison_stays_loose_where_the_server_normalises():
    # The server returns `paid` as 1/0 where `true` was sent, and spells state back in
    # whatever case it likes. Comparing strictly turned correct saves into failures.
    cases = [
        ({**GOOD, "paid": True}, {"state": "public", "coins": 55}),
        ({**GOOD, "state": "Public"}, {"state": "public", "coins": 55}),
        ({**GOOD, "type": "NOTES"}, {"state": "public", "coins": 55}),
        ({**GOOD, "coins": "55"}, {"state": "public", "coins": 55}),
    ]
    assert _wrong(cases) == [[], [], [], []]


def test_a_field_the_server_never_sent_is_not_called_wrong():
    # The new rule, and the one worth pinning. The read-back used by the reconcile can omit
    # `type`; treating absent as wrong would push every resumed chapter down the repair
    # path and re-save chapters that were already fine.
    no_type = {"state": "public", "paid": 1, "coins": 55}
    assert _wrong([(no_type, {"state": "public", "coins": 55})]) == [[]]
    assert _wrong([({}, {"state": "public", "coins": 55})]) == [[]]
    assert _wrong([(None, {"state": "public", "coins": 55})]) == [[]]


def test_an_absent_field_still_does_not_hide_a_wrong_one():
    # Absent is "cannot tell", not "fine" — the fields that ARE present still get judged.
    record = {"state": "private", "coins": 0}
    assert _wrong([(record, {"state": "public", "coins": 55})]) == [
        ['state is "private"', "coins is 0"]]
