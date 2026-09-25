"""The local template rows replayed on the L-recordings (F §6.3, §9).

Every check of every case's ``checks.json`` is replayed through
:func:`deployer.fix.templates.match_local` with the case's ``tree/Dockerfile``
and the build's own tag (the value after ``--tag`` in ``argv.json``), and
compared with the table below, written here. The committed ``expected.json``
is the observation made with the earlier hypothesis matchers and is not read:
the recordings are data and stay as they were recorded.
"""

import json
from pathlib import Path

import pytest

from deployer.fix import templates

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / templates.LOCAL_RECORDING
FROM_B = "FROM python:3.12-slim AS b"

EXPECTED: dict[tuple[str, str, str], tuple[str, tuple[int, ...], str | None]] = {
    ("l1-copy-cold", "copy", "COPY docs/guide/setup.md ./setup.md"): (
        "passed",
        (16, 18),
        None,
    ),
    ("l2-copy-warm", "copy", "COPY docs/guide/setup.md ./setup.md"): (
        "passed",
        (17, 20),
        None,
    ),
    ("l3-from-run5", "from", "FROM python:3.12-slim AS extra"): (
        "passed",
        (1,),
        None,
    ),
    ("l4-from-bad-later", "from", "FROM python:3.12-slim extra"): (
        "not_confirmed",
        (1,),
        "stderr: parse error",
    ),
    ("l5-stages-same-image", "copy", "COPY docs/guide/setup.md ./setup.md"): (
        "binding_ambiguous",
        (),
        "Dockerfile: corrected instruction repeated at lines 3, 7",
    ),
    ("l5-stages-same-image", "from", FROM_B): ("passed", (1,), None),
    ("l6-copy-later-failure", "copy", "COPY docs/guide/setup.md ./setup.md"): (
        "passed",
        (17, 19),
        None,
    ),
    ("l7-stages-both-built", "copy", "COPY docs/guide/setup.md ./setup.md"): (
        "binding_ambiguous",
        (),
        "Dockerfile: corrected instruction repeated at lines 3, 7",
    ),
    ("l7-stages-both-built", "from", FROM_B): ("passed", (7,), None),
    ("l8-from-bad-after-built-stage", "from", "FROM python:3.12-slim extra"): (
        "not_confirmed",
        (1,),
        "stderr: parse error",
    ),
    ("l9-from-bad-in-skipped-stage", "from", "FROM python:3.12-slim extra"): (
        "not_confirmed",
        (),
        "corrected step line absent",
    ),
}


def recorded_checks(root: Path = LOCAL) -> list[tuple[str, str, str]]:
    """``(case, kind, corrected)`` for every check of every case under
    ``root``."""
    return [
        (case.name, check["kind"], check["corrected"])
        for case in sorted(root.iterdir())
        if case.is_dir()
        for check in json.loads((case / "checks.json").read_text())
    ]


def replay(root: Path, check: tuple[str, str, str]) -> templates.Outcome:
    """``match_local`` over one recorded check under ``root``."""
    name, kind, corrected = check
    case = root / name
    return templates.match_local(
        kind,  # type: ignore[arg-type]
        corrected,
        (case / "build.stdout").read_text(),
        (case / "build.stderr").read_text(),
        dockerfile=(case / "tree" / "Dockerfile").read_bytes(),
        tag=_tag(case),
    )


def _checks() -> list[tuple[str, str, str]]:
    """Every check of the local recording."""
    return recorded_checks()


def _tag(case: Path) -> str:
    """The build's own tag: the value after ``--tag`` in ``argv.json``."""
    argv = json.loads((case / "argv.json").read_text())["argv"]
    return argv[argv.index("--tag") + 1]


def test_the_table_covers_every_recorded_check() -> None:
    """No recorded check is left out of the table, and none is invented."""
    assert sorted(_checks()) == sorted(EXPECTED)


@pytest.mark.parametrize(
    "check", _checks(), ids=[f"{c}-{k}{i}" for i, (c, k, _) in enumerate(_checks())]
)
def test_local_recording_replays(check: tuple[str, str, str]) -> None:
    """The production rows (no seam) give the table's outcome."""
    outcome = replay(LOCAL, check)
    assert (outcome.evidence, outcome.lines, outcome.detail) == EXPECTED[check]


def test_l4_no_step_before_parse_error() -> None:
    """The bad FROM of ``l4`` fails before Podman prints any ``STEP`` line."""
    stdout = (LOCAL / "l4-from-bad-later" / "build.stdout").read_text()
    assert "STEP" not in stdout


def test_l9_builds_with_the_bad_from_in_the_file() -> None:
    """``l9`` exits 0 with the bad FROM still in the file: the old file-wide
    FROM rule would have passed it; the step-line rule does not."""
    case = LOCAL / "l9-from-bad-in-skipped-stage"
    assert (case / "build.exit").read_text().strip() == "0"
    assert b"FROM python:3.12-slim extra" in (case / "tree" / "Dockerfile").read_bytes()
