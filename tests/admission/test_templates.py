"""The closed template table, A §4.1: every row against its real recording,
synthetic variants only as negatives (A §8.1)."""

import random
from pathlib import Path

import pytest

from deployer.admission.templates import (
    ROWS,
    CopyMatch,
    FromMatch,
    Row,
    match_copy_ci,
    match_copy_local,
    match_from_ci,
    match_from_local,
)
from deployer.forge import load_snapshot
from deployer.reproduce.shape import job_text

ROOT = Path(__file__).resolve().parents[2]
REF = "a4efb8b6-20f9-46f4-b827-91ac0547be3a::t0jkpmm4mq76x0tyiprh1w5o7"
HEADER = "#12 [stage-0 7/9] COPY docs/setup.md ./setup.md"
STEP_BOUND = (
    f'#12 ERROR: failed to calculate checksum of ref {REF}: "/docs/setup.md": not found'
)
SUMMARY = (
    "ERROR: failed to build: failed to solve: failed to compute cache key: "
    f'failed to calculate checksum of ref {REF}: "/docs/setup.md": not found'
)
BLOCK = [
    "Dockerfile:11",
    "--------------------",
    "  10 |     COPY src/ci_build ./src/ci_build",
    "  11 | >>> COPY docs/setup.md ./setup.md",
    "  12 |     ",
    "--------------------",
]
PODMAN_COPY = (
    'Error: building at STEP "COPY docs/setup.md ./setup.md": checking on '
    'sources under "/ctx": copier: stat: "/docs/setup.md": no such file or '
    "directory"
)
FROM_CI = (
    "ERROR: failed to build: failed to solve: dockerfile parse error on line 1: "
    "FROM requires either one or three arguments"
)
FROM_PODMAN = "Error: FROM requires either one argument, or three: FROM <source>"


def _ci_text(run: str) -> str:
    """The CI job text of a committed recording, as R reads it."""
    path = ROOT / "tests/fixtures/reproduction" / run / "snapshot.json"
    return job_text(load_snapshot(path.read_text()).jobs[0])


def _read(row: Row, name: str) -> str:
    """A sibling file of the row's recording (``local.stdout`` etc.)."""
    return (ROOT / row.recording).with_name(name).read_text()


def _match(row: Row) -> object:
    """Run the row's own matcher on its recording."""
    if row.recording.endswith("snapshot.json"):
        text = _ci_text(Path(row.recording).parent.name)
        if row.cls == "missing_copy_source":
            return match_copy_ci(text)
        return match_from_ci(text)
    if row.cls == "missing_copy_source":
        return match_copy_local(_read(row, "local.stderr"))
    return match_from_local(_read(row, "local.stdout"), _read(row, "local.stderr"))


def _ci_copy(*extra: str, header: str = HEADER) -> str:
    """A minimal synthetic BuildKit COPY failure plus ``extra`` lines."""
    return "\n".join([header, STEP_BOUND, *BLOCK, SUMMARY, *extra])


def test_table_is_closed_with_four_recorded_rows() -> None:
    """Exactly the four rows of A §4.1, each pinned to a committed file."""
    assert [r.id for r in ROWS] == [
        "copy-missing/buildkit",
        "copy-missing/podman",
        "from-args/buildkit",
        "from-args/podman",
    ]
    assert all((ROOT / r.recording).is_file() for r in ROWS)


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r.id)
def test_every_row_matches_its_real_recording(row: Row) -> None:
    """Each row matches its real output, unambiguously, as itself."""
    got = _match(row)
    assert isinstance(got, CopyMatch | FromMatch)
    assert got.row == row.id
    assert got.evidence_lines


def test_copy_buildkit_recording_binds_span_path_and_repeat() -> None:
    """run-1: span (11, 11), path docs/setup.md, step line and summary."""
    text = _ci_text("run-1")
    got = match_copy_ci(text)
    assert isinstance(got, CopyMatch)
    assert (got.lines, got.path) == ((11, 11), "docs/setup.md")
    assert got.step_text == "COPY docs/setup.md ./setup.md"
    lines = text.split("\n")
    quoted = {lines[n - 1] for n in got.evidence_lines}
    assert {HEADER, STEP_BOUND, "Dockerfile:11"} <= quoted
    assert any(q.startswith("ERROR: failed to build:") for q in quoted)


def test_copy_podman_recording_binds_step_and_path() -> None:
    """run-1 local: the STEP text and the path, no span."""
    row = ROWS[1]
    got = match_copy_local(_read(row, "local.stderr"))
    assert got == CopyMatch(
        row="copy-missing/podman",
        lines=None,
        step_text="COPY docs/setup.md ./setup.md",
        path="docs/setup.md",
        evidence_lines=(1,),
    )


def test_from_recordings_bind_line() -> None:
    """run-5: CI names line 1; Podman names none."""
    got = match_from_ci(_ci_text("run-5"))
    assert isinstance(got, FromMatch) and got.line == 1
    row = ROWS[3]
    local = match_from_local(_read(row, "local.stdout"), _read(row, "local.stderr"))
    assert local == FromMatch(row="from-args/podman", line=None, evidence_lines=(1,))


def test_rows_do_not_cross_match_recordings() -> None:
    """The COPY rows see nothing in run-5, the FROM rows nothing in run-1."""
    assert match_copy_ci(_ci_text("run-5")) is None
    assert match_from_ci(_ci_text("run-1")) is None
    assert match_copy_local(_read(ROWS[3], "local.stderr")) is None
    run1 = ROWS[1]
    assert (
        match_from_local(_read(run1, "local.stdout"), _read(run1, "local.stderr"))
        is None
    )


def test_synthetic_copy_ci_matches() -> None:
    """The synthetic baseline binds, so each negative below isolates one cause."""
    assert isinstance(match_copy_ci(_ci_copy()), CopyMatch)


def test_second_step_bound_line_for_another_step_is_ambiguous() -> None:
    """Another step's checksum failure is a second candidate."""
    other = f'#9 ERROR: failed to calculate checksum of ref {REF}: "/x": not found'
    assert match_copy_ci(_ci_copy(other)) == "ambiguous"


@pytest.mark.parametrize(
    "summary",
    [
        SUMMARY.replace(REF, "other::ref"),
        SUMMARY.replace("/docs/setup.md", "/docs/other.md"),
    ],
    ids=["ref", "path"],
)
def test_summary_with_other_ref_or_path_is_ambiguous(summary: str) -> None:
    """A summary line that is not a repeat is a second candidate."""
    assert match_copy_ci(_ci_copy(summary)) == "ambiguous"


def test_two_headers_with_same_instruction_are_ambiguous() -> None:
    """Two steps with the same text leave the step number open."""
    twin = "#15 [stage-1 3/4] COPY docs/setup.md ./setup.md"
    assert match_copy_ci(_ci_copy(twin)) == "ambiguous"


def test_copy_ci_without_block_or_step_line_is_ambiguous() -> None:
    """The diagnostic is present but not bound to one instruction."""
    assert match_copy_ci("\n".join([HEADER, STEP_BOUND, SUMMARY])) == "ambiguous"
    assert match_copy_ci("\n".join([HEADER, *BLOCK, SUMMARY])) == "ambiguous"


def test_copy_ci_prefixed_checksum_line_is_ambiguous() -> None:
    """A checksum line that is neither step-bound nor a summary counts."""
    stray = f'  failed to calculate checksum of ref {REF}: "/docs/setup.md": not found'
    assert match_copy_ci(_ci_copy(stray)) == "ambiguous"


def test_copy_ci_block_not_copy_is_ambiguous() -> None:
    """The single block must be a COPY/ADD."""
    text = _ci_copy().replace(">>> COPY docs/setup.md", ">>> RUN docs/setup.md")
    assert match_copy_ci(text) == "ambiguous"


def test_copy_ci_add_lowercase_and_continuation_bind() -> None:
    """ADD counts; the keyword's case and continuations are normalised."""
    header = "#12 [stage-0 7/9] ADD docs/setup.md ./setup.md"
    text = _ci_copy(header=header).replace(
        "  11 | >>> COPY docs/setup.md ./setup.md",
        "  11 | >>> add docs/setup.md \\\n  12 | >>>     ./setup.md",
    )
    got = match_copy_ci(text)
    assert isinstance(got, CopyMatch)
    assert got.lines == (11, 12)


def test_unrelated_text_matches_no_row() -> None:
    """No row's diagnostic, no match."""
    text = "#1 [internal] load build definition\nERROR: something else"
    assert match_copy_ci(text) is None
    assert match_copy_local(text) is None
    assert match_from_ci(text) is None
    assert match_from_local("", text) is None


def test_podman_copy_other_message_is_none() -> None:
    """A different copier message is not the row."""
    other = PODMAN_COPY.replace("no such file or directory", "permission denied")
    assert match_copy_local(other) is None


def test_podman_copy_two_lines_are_ambiguous() -> None:
    """Two distinct Podman COPY failures leave the object open."""
    second = PODMAN_COPY.replace("docs/setup.md", "docs/b.md")
    assert match_copy_local(f"{PODMAN_COPY}\n{second}") == "ambiguous"


def test_parse_error_of_another_kind_is_none() -> None:
    """A parse error on line 1 that is not the FROM count does not count."""
    other = "ERROR: dockerfile parse error on line 1: unknown instruction: FORM"
    assert match_from_ci(other) is None


def test_from_ci_two_distinct_lines_are_ambiguous() -> None:
    """Two parse-error lines name two instructions."""
    second = FROM_CI.replace("line 1:", "line 7:")
    assert match_from_ci(f"{FROM_CI}\n{second}") == "ambiguous"
    assert isinstance(match_from_ci(f"{FROM_CI}\n{FROM_CI}"), FromMatch)


def test_from_local_with_step_line_is_none() -> None:
    """Podman printed a STEP: the failure is not the parse-time FROM row."""
    assert match_from_local("STEP 1/3: FROM python:3.12-slim\n", FROM_PODMAN) is None
    assert match_from_local("[1/2] STEP 1/3: FROM x\n", FROM_PODMAN) is None
    assert isinstance(match_from_local("", FROM_PODMAN), FromMatch)


def test_crlf_line_endings_read_like_lf() -> None:
    """CRLF text binds the same, with the same 1-based line numbers."""
    lf = _ci_copy()
    got_lf = match_copy_ci(lf)
    got_crlf = match_copy_ci(lf.replace("\n", "\r\n"))
    assert isinstance(got_lf, CopyMatch) and got_crlf == got_lf
    assert match_copy_local(PODMAN_COPY + "\r\n") == match_copy_local(PODMAN_COPY)
    assert match_from_ci(FROM_CI + "\r\n") == match_from_ci(FROM_CI)
    assert match_from_local("\r\n", FROM_PODMAN + "\r\n") == match_from_local(
        "", FROM_PODMAN
    )


def _noise(seed: int) -> str:
    """Random text over printable, control and odd Unicode characters."""
    rng = random.Random(seed)
    alphabet = (
        'abc#:[]>|"/ -\n\r\t\x00\x1b\x85 é\U0001f600'
        "0123456789"
        "COPY ADD FROM ERROR STEP >>> --- Dockerfile:1 checksum of ref "
    )
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 4000)))


@pytest.mark.parametrize("seed", range(40))
def test_matchers_never_raise_on_noise(seed: int) -> None:
    """Untrusted text: a match, None or "ambiguous" — never an exception."""
    text = _noise(seed)
    allowed = (CopyMatch, FromMatch, type(None), str)
    for got in (
        match_copy_ci(text),
        match_copy_local(text),
        match_from_ci(text),
        match_from_local(text, text),
    ):
        assert isinstance(got, allowed)
        assert not isinstance(got, str) or got == "ambiguous"


def test_matchers_accept_empty_text() -> None:
    """Empty input matches nothing."""
    assert match_copy_ci("") is None
    assert match_copy_local("") is None
    assert match_from_ci("") is None
    assert match_from_local("", "") is None


def test_matchers_handle_huge_text() -> None:
    """Megabyte-scale lines and many lines return without raising."""
    long_line = "ERROR: " + f"a: {SUMMARY} " * 5000
    many = "\n".join([*BLOCK] * 20000)
    for text in (long_line, many):
        assert match_copy_ci(text) in (None, "ambiguous")
        assert match_copy_local(text) is None
        assert match_from_ci(text) is None
        assert match_from_local(text, text) is None
