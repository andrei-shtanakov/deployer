"""Closed "passed" template tables (design §6.3, §7.3, §9, §10 test seam).

The local (Podman) rows are backed by the L-recordings and replayed on them in
``test_local_recordings.py``; the tests here pin their rules on synthetic
lines. The CI (BuildKit) matchers are still **hypotheses**: these tests assert
the negatives, and only that the matcher returns ``passed`` on the
*synthetic* shape the pipeline tests use — never that this shape is what
BuildKit actually prints.
"""

from pathlib import Path

import pytest

from deployer.fix import templates
from deployer.fix.templates import ROWS, Outcome, enabled_rows, match_ci, match_local
from tests.fix.conftest import enable_for_test
from tests.fix.test_local_recordings import EXPECTED as RECORDED
from tests.fix.test_local_recordings import recorded_checks, replay

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
_COPY = "COPY docs/setup.md /app/docs/setup.md"
_FROM = "FROM python:3.12-slim AS extra"
_TAG = "localhost/deployer-fix-x"
_DOCKERFILE = f'{_FROM}\nWORKDIR /app\n{_COPY}\nCMD ["python"]\n'.encode()
_LOCAL_ROWS = ("copy-passed/podman", "from-parsed/podman")

# Synthetic shapes (NOT recordings) ------------------------------------------

_PODMAN_COPY_OK = (
    "STEP 1/3: FROM python:3.12-slim\n"
    "STEP 2/3: COPY docs/setup.md /app/docs/setup.md\n"
    "--> 1a2b3c\n"
    'STEP 3/3: CMD ["python"]\n'
    "Successfully tagged localhost/deployer-fix-x:latest\n"
)
_BUILDKIT_COPY_OK = (
    "#1 [internal] load build definition from Dockerfile\n"
    "#1 DONE 0.0s\n"
    "#5 [2/3] COPY docs/setup.md /app/docs/setup.md\n"
    "#5 DONE 0.1s\n"
)
_BUILDKIT_FROM_OK = (
    "#1 [internal] load build definition from Dockerfile\n"
    "#1 DONE 0.0s\n"
    "#4 [extra 1/2] FROM docker.io/library/python:3.12-slim\n"
    "#4 DONE 1.0s\n"
)


# The table and the seam -----------------------------------------------------


def test_rows_are_the_four_closed_rows() -> None:
    """Exactly the four rows of §6.3/§7.3, each on its side and backend."""
    assert [(r.id, r.side, r.backend, r.kind) for r in ROWS] == [
        ("copy-passed/podman", "local", "podman", "copy"),
        ("from-parsed/podman", "local", "podman", "from"),
        ("copy-passed/buildkit", "ci", "buildkit", "copy"),
        ("from-parsed/buildkit", "ci", "buildkit", "from"),
    ]


def test_no_row_enabled_without_recording() -> None:
    """§9: a production row is enabled only with its recording. Exactly the
    two local rows are; the cases under each one's ``Row.recording`` replay
    through ``match_local`` to the replay test's table; the CI rows stay
    disabled."""
    enabled = enabled_rows()
    assert tuple(row.id for row in enabled) == _LOCAL_ROWS
    for row in enabled:
        assert row.recording is not None
        root = _ROOT / row.recording
        mine = [c for c in recorded_checks(root) if c[1] == row.kind]
        assert mine, row.id
        for check in mine:
            outcome = replay(root, check)
            got = (outcome.evidence, outcome.lines, outcome.detail)
            assert got == RECORDED[check], (row.id, check)
    assert all(row.recording is None for row in ROWS if row.side == "ci")


def test_seam_enables_and_restores() -> None:
    """The seam injects rows for the ``with`` block only."""
    production = enabled_rows()
    with enable_for_test("copy-passed/buildkit") as rows:
        assert enabled_rows() == production + rows
    assert enabled_rows() == production


def test_seam_rejects_unknown_row() -> None:
    """A typo in a test cannot silently enable nothing."""
    with pytest.raises(KeyError), enable_for_test("nope"):
        pass


def test_registry_unreachable_from_src() -> None:
    """No code under ``src/`` except ``templates.py`` names the registry or the
    seam, so no CLI flag, env var or config can reach it."""
    offenders = [
        str(path.relative_to(_SRC))
        for path in _SRC.rglob("*.py")
        if path.name != "templates.py" or path.parent.name != "fix"
        if "_TEST_REGISTRY" in path.read_text(encoding="utf-8")
        or "enable_for_test" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
    own = (_SRC / "deployer" / "fix" / "templates.py").read_text(encoding="utf-8")
    assert "enable_for_test" not in own
    assert "os.environ" not in own
    assert "getenv" not in own


@pytest.mark.parametrize("kind", ["copy", "from"])
def test_ci_not_enabled_by_default(kind: str) -> None:
    """Without the seam the CI matchers yield ``not_enabled``, even on text
    that would otherwise pass."""
    ci = match_ci(kind, _COPY, _BUILDKIT_COPY_OK)  # type: ignore[arg-type]
    assert ci == Outcome("not_enabled", (), "templates not enabled", None)


def test_local_enabled_by_default() -> None:
    """The recording-backed local rows need no seam."""
    outcome = match_local(
        "copy", _COPY, _PODMAN_COPY_OK, "", dockerfile=_DOCKERFILE, tag=_TAG
    )
    assert outcome.evidence == "passed"


def test_other_row_does_not_enable_kind() -> None:
    """Enabling the CI COPY row leaves CI FROM disabled."""
    with enable_for_test("copy-passed/buildkit"):
        assert match_ci("from", _FROM, _BUILDKIT_FROM_OK).evidence == "not_enabled"


def test_unknown_kind_is_not_enabled() -> None:
    """A kind outside the closed table never raises."""
    with enable_for_test():
        outcome = match_local(
            "run",  # type: ignore[arg-type]
            _COPY,
            "",
            "",
            dockerfile=_DOCKERFILE,
            tag=_TAG,
        )
    assert outcome.evidence == "not_enabled"


# COPY / Podman --------------------------------------------------------------


def _local_copy(
    stdout: str,
    stderr: str = "",
    text: str = _COPY,
    dockerfile: bytes = _DOCKERFILE,
    tag: str = _TAG,
) -> Outcome:
    """``match_local`` for COPY (a production row)."""
    return match_local("copy", text, stdout, stderr, dockerfile=dockerfile, tag=tag)


def test_podman_copy_synthetic_shape_passes() -> None:
    """The synthetic shape used by the pipeline tests (not a recording)."""
    assert _local_copy(_PODMAN_COPY_OK) == Outcome("passed", (2, 4), None, None)


def test_podman_copy_completion_passes() -> None:
    """A completion line after the last step of the sequence passes."""
    stdout = f"STEP 2/2: {_COPY}\nSuccessfully tagged {_TAG}:latest\n"
    assert _local_copy(stdout).evidence == "passed"


def test_podman_copy_step_absent() -> None:
    """No step line carrying the corrected text → not confirmed."""
    stdout = "STEP 1/2: FROM python:3.12-slim\nSTEP 2/2: COPY other /app/\n"
    assert _local_copy(stdout).evidence == "not_confirmed"


def test_podman_copy_step_twice_is_ambiguous() -> None:
    """The corrected step line must appear exactly once."""
    stdout = f"STEP 2/3: {_COPY}\nSTEP 3/3: RUN x\nSTEP 2/2: {_COPY}\n"
    assert _local_copy(stdout).evidence == "binding_ambiguous"


def test_podman_copy_no_following_step() -> None:
    """A STEP line only proves the step started; nothing after it binds."""
    stdout = f"STEP 1/3: FROM python:3.12-slim\nSTEP 2/3: {_COPY}\n"
    assert _local_copy(stdout).evidence == "binding_ambiguous"


def test_podman_copy_error_after_step() -> None:
    """An error line after the step and before the next step."""
    stdout = f"STEP 2/3: {_COPY}\nError: something broke\nSTEP 3/3: RUN x\n"
    assert _local_copy(stdout).evidence == "not_confirmed"


def test_podman_copy_error_bound_to_step_on_stderr() -> None:
    """Podman's step-bound error names the corrected step → not confirmed."""
    stderr = f'Error: building at STEP "{_COPY}": checking on sources: nope\n'
    outcome = _local_copy(f"STEP 2/3: {_COPY}\n", stderr)
    assert outcome.evidence == "not_confirmed"
    assert outcome.lines == (1,)


@pytest.mark.parametrize(
    "next_line",
    [
        "STEP 1/4: FROM python:3.12-slim AS next",  # another stage's sequence
        "STEP 4/3: RUN x",  # skips a step
        "STEP 3/4: RUN x",  # another n
        "STEP 2/3: RUN x",  # same k again
    ],
)
def test_podman_copy_next_step_other_sequence(next_line: str) -> None:
    """The stage boundary is never guessed from k/n."""
    stdout = f"STEP 2/3: {_COPY}\n{next_line}\n"
    assert _local_copy(stdout).evidence == "binding_ambiguous"


def test_podman_copy_last_step_then_new_stage_is_ambiguous() -> None:
    """k == n followed by a new sequence is not assumed to be a boundary."""
    stdout = f"STEP 2/2: {_COPY}\nSTEP 1/2: FROM python:3.12-slim\n"
    assert _local_copy(stdout).evidence == "binding_ambiguous"


@pytest.mark.parametrize("text", ["", "COPY a b\nCOPY c d", f" {_COPY}", f"{_COPY} "])
def test_podman_copy_unbindable_text(text: str) -> None:
    """Empty, multi-line or unstripped corrected text cannot bind to a line."""
    assert _local_copy(_PODMAN_COPY_OK, text=text).evidence == "binding_ambiguous"


def test_podman_copy_prefixed_next_step_passes() -> None:
    """``[i/n] STEP k/m`` followed by the same stage's ``k+1`` passes (l7)."""
    stdout = f"[1/2] STEP 2/3: {_COPY}\n--> 1a2b\n[1/2] STEP 3/3: RUN x\n"
    assert _local_copy(stdout) == Outcome("passed", (1, 3), None, None)


def test_podman_copy_prefixed_next_step_of_other_stage() -> None:
    """The same ``k+1/m`` under another stage prefix does not pass."""
    stdout = f"[1/2] STEP 2/3: {_COPY}\n[2/2] STEP 3/3: RUN x\n"
    outcome = _local_copy(stdout)
    assert (outcome.evidence, outcome.detail) == (
        "binding_ambiguous",
        "next step not of this stage",
    )


def test_podman_copy_last_step_of_non_final_stage() -> None:
    """k == m of a non-final stage: the next stage's start does not prove it."""
    stdout = (
        f"[1/2] STEP 3/3: {_COPY}\n--> 1a2b\n[2/2] STEP 1/2: FROM python:3.12-slim\n"
    )
    outcome = _local_copy(stdout)
    assert (outcome.evidence, outcome.detail) == (
        "binding_ambiguous",
        "next stage start does not prove it",
    )


@pytest.mark.parametrize(
    "done",
    [f"[2/2] COMMIT {_TAG}", f"COMMIT {_TAG}", f"Successfully tagged {_TAG}:latest"],
)
def test_podman_copy_final_stage_own_tag_passes(done: str) -> None:
    """After the final stage's last step, a completion naming the build's own
    tag passes (a ``COMMIT`` carries the step's own prefix)."""
    prefix = "[2/2] " if done.startswith("[") else ""
    stdout = f"{prefix}STEP 3/3: {_COPY}\n{done}\n"
    assert _local_copy(stdout).evidence == "passed"


@pytest.mark.parametrize(
    "done",
    [
        "COMMIT localhost/other",
        "Successfully tagged localhost/other:latest",
        f"Successfully tagged {_TAG}-2:latest",
    ],
)
def test_podman_copy_foreign_tag_completion(done: str) -> None:
    """A completion naming another tag does not count: binding ambiguous
    (l2 prints a foreign ``Successfully tagged`` after its own)."""
    outcome = _local_copy(f"STEP 3/3: {_COPY}\n{done}\n")
    assert (outcome.evidence, outcome.detail) == (
        "binding_ambiguous",
        "completion of another tag",
    )


def test_podman_copy_explicit_tag_part_gets_no_latest() -> None:
    """``:latest`` is added only to a tag with no explicit tag part."""
    stdout = f"STEP 1/1: {_COPY}\nSuccessfully tagged localhost/x:v1:latest\n"
    assert _local_copy(stdout, tag="localhost/x:v1").evidence == "binding_ambiguous"
    stdout = f"STEP 1/1: {_COPY}\nSuccessfully tagged localhost/x:v1\n"
    assert _local_copy(stdout, tag="localhost/x:v1").evidence == "passed"


def test_podman_copy_empty_tag_binds_nothing() -> None:
    """No tag given: no completion line can bind."""
    stdout = f"STEP 1/1: {_COPY}\nSuccessfully tagged :latest\n"
    assert _local_copy(stdout, tag="").evidence == "binding_ambiguous"


def test_podman_copy_dockerfile_duplicate_ambiguous() -> None:
    """An identical instruction elsewhere in the Dockerfile refuses before
    any output is read, even when one step line would pass (l5)."""
    dockerfile = f"FROM a AS x\n{_COPY}\nFROM b\n{_COPY}\n".encode()
    outcome = _local_copy(_PODMAN_COPY_OK, dockerfile=dockerfile)
    assert outcome == Outcome(
        "binding_ambiguous",
        (),
        "Dockerfile: corrected instruction repeated at lines 2, 4",
        None,
    )


def test_podman_copy_dockerfile_duplicate_as_r_reads() -> None:
    """Instructions are compared as R reads them: keyword case, spacing and
    continuations do not hide a duplicate."""
    dockerfile = f"FROM a\n{_COPY}\ncopy  docs/setup.md \\\n  /app/docs/setup.md\n"
    outcome = _local_copy(_PODMAN_COPY_OK, dockerfile=dockerfile.encode())
    assert outcome.evidence == "binding_ambiguous"


def test_podman_copy_absent_from_dockerfile() -> None:
    """A corrected text that is no instruction of the Dockerfile cannot bind."""
    outcome = _local_copy(_PODMAN_COPY_OK, dockerfile=b"FROM a\nRUN x\n")
    assert (outcome.evidence, outcome.detail) == (
        "binding_ambiguous",
        "Dockerfile: corrected instruction absent",
    )


def test_i2_copy_continuation_twin_refused() -> None:
    """Review I2: ``docs/a\\<newline>b`` reads ``docs/a b`` in R but prints
    ``docs/ab`` in Podman; the whole-file reading check refuses it, so the
    skipped stage's COPY cannot borrow the built one's line."""
    text = "COPY docs/ab ./x"
    dockerfile = (
        b"FROM python:3.12-slim AS a\n"
        b"COPY docs/ab ./x\n"
        b"FROM python:3.12-slim AS b\n"
        b"COPY docs/a\\\n"
        b"b ./x\n"
        b"RUN true\n"
    )
    stdout = "[2/2] STEP 2/3: COPY docs/ab ./x\n[2/2] STEP 3/3: RUN true\n"
    outcome = _local_copy(stdout, text=text, dockerfile=dockerfile)
    assert outcome.evidence == "binding_ambiguous"
    assert outcome.detail is not None and "continuation" in outcome.detail


@pytest.mark.parametrize(
    "other", ['COPY "docs/setup.md" /app/docs/setup.md', "COPY $SRC /app/docs/setup.md"]
)
def test_j_copy_family_unmodelled_refused(other: str) -> None:
    """Ruling J: any COPY/ADD with a quote or ``$`` makes uniqueness
    unprovable."""
    dockerfile = f"FROM a AS x\n{other}\nFROM b\n{_COPY}\n".encode()
    outcome = _local_copy(_PODMAN_COPY_OK, dockerfile=dockerfile)
    assert outcome.evidence == "binding_ambiguous"
    assert outcome.detail is not None and "unprovable" in outcome.detail


def test_podman_copy_escape_directive_unread() -> None:
    """A non-default escape directive: the split is untrusted."""
    dockerfile = b"# escape=`\n" + _DOCKERFILE
    assert _local_copy(_PODMAN_COPY_OK, dockerfile=dockerfile).evidence == (
        "binding_ambiguous"
    )


# FROM / Podman --------------------------------------------------------------


def _local_from(
    stdout: str, stderr: str = "", dockerfile: bytes = _DOCKERFILE
) -> Outcome:
    """``match_local`` for FROM (a production row)."""
    return match_local("from", _FROM, stdout, stderr, dockerfile=dockerfile, tag=_TAG)


@pytest.mark.parametrize("prefix", ["", "[2/2] "])
def test_podman_from_own_step_line_passes(prefix: str) -> None:
    """The corrected FROM's own ``STEP 1/m`` line, prefixed or not; no next
    marker needed."""
    assert _local_from(f"{prefix}STEP 1/2: {_FROM}\n") == Outcome(
        "passed", (1,), None, None
    )


def test_podman_from_other_step_is_not_evidence() -> None:
    """The old file-wide rule is gone: another stage's step proves nothing
    about the corrected FROM (l9)."""
    stdout = "STEP 1/2: FROM python:3.12-slim\nSTEP 2/2: RUN x\n"
    outcome = _local_from(stdout)
    assert (outcome.evidence, outcome.detail) == (
        "not_confirmed",
        "corrected step line absent",
    )


def test_podman_from_step_repeated_or_not_first() -> None:
    """Twice → ambiguous; not step 1 → ambiguous."""
    twice = f"STEP 1/2: {_FROM}\nSTEP 1/2: {_FROM}\n"
    assert _local_from(twice).evidence == "binding_ambiguous"
    assert _local_from(f"STEP 2/2: {_FROM}\n").evidence == "binding_ambiguous"


def test_podman_from_step_bound_error() -> None:
    """An error bound to the FROM step → not confirmed."""
    stderr = f'Error: building at STEP "{_FROM}": pull access denied\n'
    outcome = _local_from(f"STEP 1/2: {_FROM}\n", stderr)
    assert (outcome.evidence, outcome.detail) == (
        "not_confirmed",
        "stderr: step-bound error",
    )


@pytest.mark.parametrize(
    "built", ["FROM ${BASE}", 'FROM "python:3.12-slim"'], ids=["arg", "quoted"]
)
def test_i1_skipped_from_beside_rebuilt_twin(built: str) -> None:
    """Review I1: the corrected FROM sits in a skipped stage; another FROM
    that Podman prints rebuilt as the same line must not confirm it."""
    text = "FROM python:3.12-slim"
    dockerfile = (
        f"ARG BASE=python:3.12-slim\n{text}\nRUN true\n{built}\nRUN true\n"
    ).encode()
    stdout = f"[2/2] STEP 1/2: {text}\n[2/2] STEP 2/2: RUN true\n"
    outcome = match_local("from", text, stdout, "", dockerfile=dockerfile, tag=_TAG)
    assert outcome.evidence == "binding_ambiguous"
    assert outcome.detail is not None and "unprovable" in outcome.detail


def test_from_display_twin_refused() -> None:
    """FROMs differing only in ``as``/name case print the same line."""
    dockerfile = f"{_FROM}\nRUN x\nFROM python:3.12-slim as EXTRA\n".encode()
    outcome = _local_from(f"STEP 1/2: {_FROM}\n", dockerfile=dockerfile)
    assert outcome.evidence == "binding_ambiguous"


def test_from_numeric_stage_name_refused() -> None:
    """Podman drops a numeric ``AS <n>`` from the printed line."""
    dockerfile = f"{_FROM}\nRUN x\nFROM python:3.12-slim AS 0\n".encode()
    outcome = _local_from(f"STEP 1/2: {_FROM}\n", dockerfile=dockerfile)
    assert outcome.evidence == "binding_ambiguous"


def test_podman_from_dockerfile_duplicate() -> None:
    """Two identical FROMs: ambiguous before the output is read."""
    dockerfile = f"{_FROM}\nRUN x\n{_FROM}\nRUN y\n".encode()
    outcome = _local_from(f"[2/2] STEP 1/2: {_FROM}\n", dockerfile=dockerfile)
    assert outcome.evidence == "binding_ambiguous"


def test_podman_from_no_step() -> None:
    """No build-stage step → not confirmed."""
    assert _local_from("Trying to pull python...\n").evidence == "not_confirmed"


@pytest.mark.parametrize(
    "stderr",
    [
        "Error: FROM requires either one argument, or three: FROM a b\n",
        "Error: dockerfile parse error line 3: unknown instruction\n",
    ],
)
def test_podman_from_parse_error(stderr: str) -> None:
    """A parse error anywhere refutes the parse, whatever steps started."""
    outcome = _local_from(f"STEP 1/2: {_FROM}\n", stderr)
    assert outcome.evidence == "not_confirmed"
    assert outcome.lines == (1,)


# COPY / BuildKit ------------------------------------------------------------


def _ci_copy(log: str, text: str = _COPY) -> Outcome:
    """``match_ci`` for COPY with the seam on."""
    with enable_for_test("copy-passed/buildkit"):
        return match_ci("copy", text, log)


def test_buildkit_copy_synthetic_shape_passes() -> None:
    """The synthetic shape used by the pipeline tests (not a recording)."""
    assert _ci_copy(_BUILDKIT_COPY_OK) == Outcome("passed", (3, 4), None, None)


def test_buildkit_copy_cached_not_accepted() -> None:
    """``#k CACHED`` is never accepted automatically."""
    log = f"#5 [2/3] {_COPY}\n#5 CACHED\n"
    assert _ci_copy(log).evidence == "not_confirmed"


def test_buildkit_copy_error() -> None:
    """A step-bound error → not confirmed."""
    log = f"#5 [2/3] {_COPY}\n#5 ERROR: failed to calculate checksum\n"
    assert _ci_copy(log).evidence == "not_confirmed"


def test_buildkit_copy_header_absent() -> None:
    """No header with the corrected text → not confirmed."""
    log = "#5 [2/3] COPY other /app/\n#5 DONE 0.1s\n"
    assert _ci_copy(log).evidence == "not_confirmed"


def test_buildkit_copy_missing_done() -> None:
    """No ``#k DONE`` for the header's k → binding ambiguous."""
    log = f"#5 [2/3] {_COPY}\n#6 DONE 0.1s\n"
    assert _ci_copy(log).evidence == "binding_ambiguous"


@pytest.mark.parametrize(
    "log",
    [
        f"#5 [2/3] {_COPY}\n#5 [2/3] {_COPY}\n#5 DONE 0.1s\n",  # header twice
        f"#5 [2/3] {_COPY}\n#9 [2/3] {_COPY}\n#5 DONE 0.1s\n#9 DONE 0.1s\n",
        f"#5 [2/3] {_COPY}\n#5 [3/3] RUN x\n#5 DONE 0.1s\n",  # k reused
        f"#5 [2/3] {_COPY}\n#5 DONE 0.1s\n#5 DONE 0.2s\n",  # DONE twice
    ],
)
def test_buildkit_copy_repeated_k(log: str) -> None:
    """A repeated header or ``k`` → binding ambiguous."""
    assert _ci_copy(log).evidence == "binding_ambiguous"


def test_buildkit_copy_internal_header_not_a_step() -> None:
    """An ``[internal]`` header is not a build-stage step header."""
    log = f"#5 [internal] {_COPY}\n#5 DONE 0.1s\n"
    assert _ci_copy(log).evidence == "not_confirmed"


def test_buildkit_unknown_format() -> None:
    """Text with no BuildKit plain-progress lines at all."""
    assert _ci_copy("Step 2/3 : COPY x y\n").evidence == "unknown_format"


# FROM / BuildKit ------------------------------------------------------------


def _ci_from(log: str) -> Outcome:
    """``match_ci`` for FROM with the seam on."""
    with enable_for_test("from-parsed/buildkit"):
        return match_ci("from", _FROM, log)


def test_buildkit_from_synthetic_shape_passes() -> None:
    """A build-stage header and no parse error (file-wide)."""
    assert _ci_from(_BUILDKIT_FROM_OK) == Outcome("passed", (3,), None, None)


def test_buildkit_from_parse_error() -> None:
    """``dockerfile parse error`` anywhere → not confirmed."""
    log = _BUILDKIT_FROM_OK + (
        "ERROR: failed to solve: dockerfile parse error on line 3: "
        "FROM requires either one or three arguments\n"
    )
    outcome = _ci_from(log)
    assert outcome.evidence == "not_confirmed"
    assert outcome.lines == (5,)


def test_buildkit_from_loading_steps_are_not_evidence() -> None:
    """Loading the definition, context or frontend proves no parse."""
    log = (
        "#1 [internal] load build definition from Dockerfile\n"
        "#1 DONE 0.0s\n"
        "#2 resolve image config for docker.io/docker/dockerfile:1\n"
        "#3 [internal] load build context\n"
        "#3 DONE 0.0s\n"
    )
    assert _ci_from(log).evidence == "not_confirmed"


# Never raise ----------------------------------------------------------------

_PATHOLOGICAL = [
    "",
    "\n\n\n",
    "\r\n" * 50,
    "\x00" * 1000,
    "#" * 100_000,
    "#1 [" * 20_000,
    "STEP " * 20_000,
    "#1 [" + "]" * 50_000 + " x\n#1 DONE\n",
    "STEP 999999999/999999999: " + "x" * 100_000,
    " STEP 1/1: x \n",
    "#99999999999999999999 [1/1] COPY a b\n",
]


@pytest.mark.parametrize(
    "text",
    _PATHOLOGICAL,
    # Short ids: the inputs are huge or hold NUL bytes, which must not end up
    # in node ids, PYTEST_CURRENT_TEST or CI logs.
    ids=[f"case{index}" for index in range(len(_PATHOLOGICAL))],
)
@pytest.mark.parametrize("kind", ["copy", "from"])
def test_matchers_never_raise(text: str, kind: str) -> None:
    """Any text, as corrected text or output, yields an ``Outcome``."""
    with enable_for_test():
        results = [
            match_local(
                kind,  # type: ignore[arg-type]
                text,
                text,
                text,
                dockerfile=text.encode(),
                tag=text,
            ),
            match_local(
                kind,  # type: ignore[arg-type]
                _COPY,
                text,
                text,
                dockerfile=text.encode(),
                tag=_TAG,
            ),
            match_ci(kind, text, text),  # type: ignore[arg-type]
            match_ci(kind, _COPY, text),  # type: ignore[arg-type]
        ]
    assert all(isinstance(result, Outcome) for result in results)
    assert all(result.evidence != "passed" for result in results[2:])


def test_overlong_line_is_unknown_format() -> None:
    """A line past the bound is not read: fail closed as unknown format."""
    long_line = "x" * (templates.MAX_LINE + 1)
    assert _ci_copy(f"{_BUILDKIT_COPY_OK}{long_line}\n").evidence == "unknown_format"
    assert _local_copy(f"{_PODMAN_COPY_OK}{long_line}\n").evidence == ("unknown_format")
    assert _local_copy(_PODMAN_COPY_OK, long_line).evidence == "unknown_format"


def test_crlf_reads_like_lf() -> None:
    """Lines are stripped, so CRLF output binds like LF."""
    crlf = _BUILDKIT_COPY_OK.replace("\n", "\r\n")
    assert _ci_copy(crlf).evidence == "passed"


# Fix round 1 regressions ----------------------------------------------------


def test_buildkit_copy_done_before_header_not_counted() -> None:
    """A ``#k DONE`` printed before the header is not this step's result."""
    log = f"#5 DONE 0.1s\n#5 [2/3] {_COPY}\n"
    assert _ci_copy(log).evidence == "binding_ambiguous"


def test_buildkit_copy_cached_before_header_not_counted() -> None:
    """Result lines before the header never decide the step."""
    log = f"#5 CACHED\n#5 [2/3] {_COPY}\n#5 DONE 0.1s\n"
    assert _ci_copy(log).evidence == "passed"


def test_buildkit_copy_k_reused_by_later_build() -> None:
    """A later build reusing ``#5`` for a non-stage vertex cannot lend its
    ``DONE`` to our step: the numbering restart refuses the log."""
    log = (
        f"#5 [2/3] {_COPY}\n"
        "#1 [internal] load build definition from Dockerfile\n"
        "#5 exporting to image\n"
        "#5 DONE 0.3s\n"
    )
    assert _ci_copy(log).evidence == "binding_ambiguous"


def test_buildkit_copy_two_definition_loads() -> None:
    """The build definition loaded twice means two builds in one log."""
    load = "#1 [internal] load build definition from Dockerfile\n#1 DONE 0.0s\n"
    log = f"{load}#5 [2/3] {_COPY}\n#5 DONE 0.1s\n{load}"
    outcome = _ci_copy(log)
    assert outcome.evidence == "binding_ambiguous"
    assert outcome.lines == (1, 5)


def test_buildkit_interleaved_lower_k_is_not_a_restart() -> None:
    """A lower ``#k`` already seen (interleaved output) is not a restart."""
    log = (
        "#1 [internal] load build definition from Dockerfile\n"
        f"#5 [2/3] {_COPY}\n"
        "#1 DONE 0.0s\n"
        "#5 DONE 0.1s\n"
    )
    assert _ci_copy(log).evidence == "passed"


def test_buildkit_from_two_builds_refused() -> None:
    """An earlier build's stage header cannot pass our failed build."""
    log = (
        "#1 [internal] load build definition from Dockerfile\n"
        "#4 [1/2] FROM x\n"
        "#4 DONE\n"
        "#1 [internal] load build definition from Dockerfile\n"
        "#1 DONE\n"
        "ERROR: failed to solve: invalid reference format\n"
    )
    assert _ci_from(log).evidence == "binding_ambiguous"


def test_buildkit_from_numbering_restart_refused() -> None:
    """A restart of ``#k`` numbering also refuses the FROM row."""
    log = "#4 [1/2] FROM x\n#4 DONE\n#2 [internal] load .dockerignore\n"
    assert _ci_from(log).evidence == "binding_ambiguous"


def test_podman_copy_completion_before_last_step() -> None:
    """Completion right after step k < n is not this sequence's end."""
    stdout = f"STEP 2/3: {_COPY}\nSuccessfully tagged a\n"
    assert _local_copy(stdout).evidence == "binding_ambiguous"


def test_podman_from_wrapped_argument_error() -> None:
    """The FROM-args message wrapped in a step-bound error still refutes."""
    stderr = (
        'Error: building at STEP "FROM a b c d": FROM requires either one '
        "argument, or three: FROM <source> [AS <name>]\n"
    )
    outcome = _local_from("STEP 1/3: FROM a\n", stderr)
    assert outcome.evidence == "not_confirmed"
    assert outcome.detail == "stderr: parse error"


def test_module_documents_forged_step_limit_and_pull_todo() -> None:
    """The hypothesis limits are written down, not implied."""
    own = (_SRC / "deployer" / "fix" / "templates.py").read_text(encoding="utf-8")
    doc = templates.__doc__ or ""
    assert "forged" in doc
    assert "TODO:" in own and "§7.3" in own
