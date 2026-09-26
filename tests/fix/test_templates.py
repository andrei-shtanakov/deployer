"""Closed "passed" template tables (design §6.3, §7.3, §9, §10 test seam).

The local (Podman) rows are backed by the L-recordings and replayed on them in
``test_local_recordings.py``; the CI (BuildKit) rows by the C-recordings,
replayed in ``test_ci_recordings.py``. The tests here pin their rules on
synthetic lines; a synthetic shape is never a claim about what a builder
prints.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from deployer.fix import templates
from deployer.fix.templates import ROWS, Outcome, enabled_rows, match_ci, match_local
from tests.fix.conftest import enable_for_test
from tests.fix.test_ci_recordings import EXPECTED as CI_RECORDED
from tests.fix.test_ci_recordings import TEMPLATE as CI_TEMPLATE
from tests.fix.test_local_recordings import EXPECTED as RECORDED
from tests.fix.test_local_recordings import recorded_checks, replay

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
_COPY = "COPY docs/setup.md /app/docs/setup.md"
_FROM = "FROM python:3.12-slim AS extra"
_TAG = "localhost/deployer-fix-x"
_DOCKERFILE = f'{_FROM}\nWORKDIR /app\n{_COPY}\nCMD ["python"]\n'.encode()
_ALL_ROWS = (
    "copy-passed/podman",
    "from-parsed/podman",
    "copy-passed/buildkit",
    "from-parsed/buildkit",
)

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
    "#5 [extra 3/3] COPY docs/setup.md /app/docs/setup.md\n"
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
    """§9: a production row is enabled only with its recording. All four
    are; each one's ``Row.recording`` holds cases of its kind, and they
    replay to the replay tests' tables (local: ``match_local``; CI: the
    replay test's table covers every case, with its own replay)."""
    enabled = enabled_rows()
    assert tuple(row.id for row in enabled) == _ALL_ROWS
    for row in enabled:
        assert row.recording is not None
        root = _ROOT / row.recording
        mine = [c for c in recorded_checks(root) if c[1] == row.kind]
        assert mine, row.id
        if row.side == "ci":
            names = sorted({name for name, _, _ in recorded_checks(root)})
            assert names == sorted(CI_RECORDED), row.id
            continue
        for check in mine:
            outcome = replay(root, check)
            got = (outcome.evidence, outcome.lines, outcome.detail)
            assert got == RECORDED[check], (row.id, check)
    assert set(CI_TEMPLATE) <= set(CI_RECORDED)


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


def test_ci_enabled_by_default() -> None:
    """The recording-backed CI rows need no seam."""
    copy = match_ci(
        "copy", _COPY, _BUILDKIT_COPY_OK, dockerfile=_DOCKERFILE, conclusion="success"
    )
    from_ = match_ci("from", _FROM, _BUILDKIT_FROM_OK, dockerfile=_DOCKERFILE)
    assert (copy.evidence, from_.evidence) == ("passed", "passed")


@pytest.mark.parametrize("kind", ["copy", "from"])
def test_ci_row_without_recording_not_enabled(kind: str) -> None:
    """A CI row with no recording yields ``not_enabled``, even on text that
    would otherwise pass."""
    with patch.object(templates, "ROWS", _without_recording("ci")):
        ci = match_ci(
            kind,  # type: ignore[arg-type]
            _COPY,
            _BUILDKIT_COPY_OK,
            dockerfile=_DOCKERFILE,
        )
    assert ci == Outcome("not_enabled", (), "templates not enabled", None)


def _without_recording(side: str, kind: str | None = None) -> tuple[templates.Row, ...]:
    """The production table with ``side``'s rows (of ``kind``, when given)
    disabled."""
    return tuple(
        templates.Row(r.id, r.side, r.backend, r.kind, None)
        if r.side == side and kind in (None, r.kind)
        else r
        for r in ROWS
    )


def test_local_enabled_by_default() -> None:
    """The recording-backed local rows need no seam."""
    outcome = match_local(
        "copy", _COPY, _PODMAN_COPY_OK, "", dockerfile=_DOCKERFILE, tag=_TAG
    )
    assert outcome.evidence == "passed"


def test_other_row_does_not_enable_kind() -> None:
    """The CI COPY row enabled does not enable a disabled CI FROM row."""
    with patch.object(templates, "ROWS", _without_recording("ci", "from")):
        outcome = match_ci("from", _FROM, _BUILDKIT_FROM_OK, dockerfile=_DOCKERFILE)
    assert outcome.evidence == "not_enabled"


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


_N_SPLIT = {"n1": b"RUN true # x<<EOF", "n2": b"RUN true \\\x0c"}


@pytest.mark.parametrize("case", ["n1", "n2"])
def test_n_copy_divergent_line_refused(case: str) -> None:
    """Re-review N1/N2 (real Podman 5.7.0): Buildah reads the line as a plain
    RUN, R as a heredoc or a continuation that swallows the built COPY; the
    whole-file reading check refuses before any output is read."""
    dockerfile = (
        b"FROM python:3.12-slim AS a\nCOPY docs/ab ./x\nFROM python:3.12-slim AS b\n"
        + _N_SPLIT[case]
        + b"\nCOPY docs/ab ./x\nRUN true\n"
    )
    stdout = "[2/2] STEP 2/3: COPY docs/ab ./x\n[2/2] STEP 3/3: RUN true\n"
    outcome = _local_copy(stdout, text="COPY docs/ab ./x", dockerfile=dockerfile)
    assert outcome.evidence == "binding_ambiguous"
    assert outcome.detail is not None and outcome.detail.startswith("Dockerfile: ")


@pytest.mark.parametrize("case", ["n1", "n2"])
def test_n_from_divergent_line_refused(case: str) -> None:
    """The FROM variants of N1/N2: R hides the second FROM, Buildah builds it."""
    text = "FROM python:3.12-slim"
    dockerfile = (
        text.encode() + b"\n" + _N_SPLIT[case] + b"\n" + text.encode() + b"\nRUN true\n"
    )
    stdout = f"[2/2] STEP 1/2: {text}\n[2/2] STEP 2/2: RUN true\n"
    outcome = match_local("from", text, stdout, "", dockerfile=dockerfile, tag=_TAG)
    assert outcome.evidence == "binding_ambiguous"
    assert outcome.detail is not None and outcome.detail.startswith("Dockerfile: ")


# Ruling O: the round-4 re-review repros (real Podman 5.7.0 builds). Buildah
# reads the COPY/FROM in the second stage; R hides it (a lone CR taken as a
# newline, a heredoc R does not open, a BOM hiding ``# escape=``). The other
# delimiters Buildah opens and R does not (``<<"EOF"``, ``<<'EOF'``,
# ``<<-"EOF"``, ``<<1EOF``) are in ``test_reading``'s gate cases.
_O_COPY_SPLIT = {
    "cr": b"RUN true \\\r \n",
    "hd-space": b'RUN cat <<" "\nx \\\n \n',
}
_O_COPY_PREAMBLE = {
    "bom-escape": b"\xef\xbb\xbf# escape=`\n",
    "unknown-escape": b"# foo=bar\n# escape=`\n",
}
_O_COPY_IDS = [*_O_COPY_SPLIT, *_O_COPY_PREAMBLE]


def _o_copy_dockerfile(case: str) -> bytes:
    """The two-stage COPY repro for ``case``: stage ``a`` skipped."""
    preamble = _O_COPY_PREAMBLE.get(case, b"")
    split = _O_COPY_SPLIT.get(case, b"RUN true \\\n")
    return (
        preamble
        + b"FROM python:3.12-slim AS a\nCOPY docs/ab ./x\nFROM python:3.12-slim AS b\n"
        + split
        + b"COPY docs/ab ./x\nRUN true\n"
    )


@pytest.mark.parametrize("case", _O_COPY_IDS, ids=_O_COPY_IDS)
def test_o_copy_repro_refused(case: str) -> None:
    """The strict-form gate refuses before any output is read."""
    stdout = "[2/2] STEP 3/4: COPY docs/ab ./x\n[2/2] STEP 4/4: RUN true\n"
    dockerfile = _o_copy_dockerfile(case)
    outcome = _local_copy(stdout, text="COPY docs/ab ./x", dockerfile=dockerfile)
    assert outcome.evidence == "binding_ambiguous"
    assert outcome.detail is not None and outcome.detail.startswith("Dockerfile: ")
    assert outcome.lines == ()


_O_FROM = {
    "cr": b"FROM python:3.12-slim\nRUN true \\\r \nFROM python:3.12-slim\nRUN true\n",
    "hd-space": (
        b'FROM python:3.12-slim\nRUN cat <<" "\nx \\\n \n'
        b"FROM python:3.12-slim\nRUN true\n"
    ),
    "bom": (
        b"\xef\xbb\xbfFROM python:3.12-slim\nRUN true\nFROM python:3.12-slim\n"
        b"RUN true\nFROM python:3.13-slim\nCOPY --from=0 /etc/hostname /h\n"
    ),
}


@pytest.mark.parametrize("case", list(_O_FROM), ids=list(_O_FROM))
def test_o_from_repro_refused(case: str) -> None:
    """The FROM variants: R sees one ``FROM python:3.12-slim``, Buildah two."""
    text = "FROM python:3.12-slim"
    stdout = f"[1/3] STEP 1/2: {text}\n[1/3] STEP 2/2: RUN true\n"
    outcome = match_local("from", text, stdout, "", dockerfile=_O_FROM[case], tag=_TAG)
    assert outcome.evidence == "binding_ambiguous"
    assert outcome.detail is not None and outcome.detail.startswith("Dockerfile: ")


def test_o_crlf_dockerfile_still_matches() -> None:
    """CRLF endings pass the gate: the synthetic COPY shape still passes."""
    dockerfile = _DOCKERFILE.replace(b"\n", b"\r\n")
    assert _local_copy(_PODMAN_COPY_OK, dockerfile=dockerfile).evidence == "passed"


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


def test_from_flag_other_than_platform_refused() -> None:
    """Only ``--platform=`` has a recorded display; any other FROM flag makes
    uniqueness unprovable (#99 review)."""
    dockerfile = f"{_FROM}\nRUN x\nFROM --foo=bar python:3.12-slim\n".encode()
    outcome = _local_from(f"STEP 1/2: {_FROM}\n", dockerfile=dockerfile)
    assert outcome.evidence == "binding_ambiguous"
    assert "--platform=" in (outcome.detail or "")


def test_from_platform_flag_still_read() -> None:
    """``--platform=`` on another FROM is modelled: no refusal on that ground."""
    dockerfile = f"{_FROM}\nRUN x\nFROM --platform=linux/amd64 busybox:1\n".encode()
    outcome = _local_from(f"STEP 1/2: {_FROM}\n", dockerfile=dockerfile)
    assert "flag other than" not in (outcome.detail or "")


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


def _ci_copy(
    log: str,
    text: str = _COPY,
    dockerfile: bytes = _DOCKERFILE,
    conclusion: str | None = "success",
) -> Outcome:
    """``match_ci`` for COPY (a production row), the build step concluded
    ``conclusion``."""
    return match_ci("copy", text, log, dockerfile=dockerfile, conclusion=conclusion)


def test_buildkit_copy_synthetic_shape_passes() -> None:
    """The synthetic shape used by the pipeline tests (not a recording)."""
    assert _ci_copy(_BUILDKIT_COPY_OK) == Outcome("passed", (3, 4), None, None)


def test_buildkit_copy_cached_not_accepted() -> None:
    """``#k CACHED`` is never accepted automatically."""
    log = f"#5 [extra 3/3] {_COPY}\n#5 CACHED\n"
    assert _ci_copy(log).evidence == "not_confirmed"


def test_buildkit_copy_error() -> None:
    """A step-bound error → not confirmed."""
    log = f"#5 [extra 3/3] {_COPY}\n#5 ERROR: failed to calculate checksum\n"
    assert _ci_copy(log).evidence == "not_confirmed"


def test_buildkit_copy_header_absent() -> None:
    """No header with the corrected text → not confirmed."""
    log = "#5 [stage-0 2/3] COPY other /app/\n#5 DONE 0.1s\n"
    assert _ci_copy(log).evidence == "not_confirmed"


def test_buildkit_copy_missing_done() -> None:
    """No ``#k DONE`` for the header's k → binding ambiguous."""
    log = f"#5 [extra 3/3] {_COPY}\n#6 DONE 0.1s\n"
    assert _ci_copy(log).evidence == "binding_ambiguous"


@pytest.mark.parametrize(
    "log",
    [
        f"#5 [extra 3/3] {_COPY}\n#5 [extra 3/3] {_COPY}\n#5 DONE 0.1s\n",  # header twice
        f"#5 [extra 3/3] {_COPY}\n#9 [extra 3/3] {_COPY}\n#5 DONE 0.1s\n#9 DONE 0.1s\n",
        f"#5 [extra 3/3] {_COPY}\n#5 [stage-0 3/3] RUN x\n#5 DONE 0.1s\n",  # k reused
        f"#5 [extra 3/3] {_COPY}\n#5 DONE 0.1s\n#5 DONE 0.2s\n",  # DONE twice
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


def _ci_from(log: str, dockerfile: bytes = _DOCKERFILE) -> Outcome:
    """``match_ci`` for FROM (a production row)."""
    return match_ci("from", _FROM, log, dockerfile=dockerfile)


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


# BuildKit padding (owner, 2026-09-26; c4/c9) -------------------------------


@pytest.mark.parametrize(
    ("bracket", "k", "n", "stage"),
    [
        ("stage-0  7/10", 7, 10, ""),
        ("stage-0 10/10", 10, 10, ""),
        ("stage-0 7/9", 7, 9, ""),
        ("a    7/1000", 7, 1000, " AS a"),
        ("x 2/3", 2, 3, " AS x"),
    ],
    ids=["pad", "wide", "bare", "pad3", "named"],
)
def test_buildkit_padded_step_passes(bracket: str, k: int, n: int, stage: str) -> None:
    """``k`` right-aligned to ``n``'s width, or unpadded when as wide; the
    COPY is step ``k`` of ``n`` in the Dockerfile (ruling Z)."""
    runs = "RUN x\n"
    dockerfile = (
        f"FROM python:3.12-slim{stage}\n{runs * (k - 2)}{_COPY}\n{runs * (n - k)}"
    ).encode()
    log = f"#5 [{bracket}] {_COPY}\n#5 DONE 0.1s\n"
    assert _ci_copy(log, dockerfile=dockerfile).evidence == "passed"


@pytest.mark.parametrize(
    "bracket",
    [
        "stage-0   7/10",  # two spaces where one is due
        "stage-0  7/9",  # padding where none is due
        "stage-0  10/10",  # padding wider than len(str(n))
        "stage-0 7/10",  # padding missing
        "stage-0\t7/10",  # a tab
        "stage-0 \t7/10",  # a tab as padding
        "stage-0 07/10",  # a leading zero is not padding
        "  7/10",  # unnamed, padding too wide
        " 7/10",  # unnamed (ruling W), padding right
        "2/3",  # unnamed (ruling W)
    ],
    ids=[
        "two",
        "undue",
        "wide",
        "missing",
        "tab",
        "tabpad",
        "zero",
        "unnamed",
        "unnamed-pad",
        "unnamed-bare",
    ],
)
def test_buildkit_bad_padding_refused(bracket: str) -> None:
    """Any other run of spaces, a tab, or a bracket without a stage name is
    not a stage header."""
    log = f"#5 [{bracket}] {_COPY}\n#5 DONE 0.1s\n"
    assert _ci_copy(log).evidence == "not_confirmed"


def test_buildkit_from_padding_rule_applies() -> None:
    """A FROM stage header is read by the same padding rule."""
    good = "#4 [extra  1/10] FROM docker.io/library/python:3.12-slim\n#4 DONE\n"
    bad = "#4 [extra   1/10] FROM docker.io/library/python:3.12-slim\n#4 DONE\n"
    assert _ci_from(good).evidence == "passed"
    assert _ci_from(bad).evidence == "not_confirmed"


# BuildKit reads the corrected Dockerfile first (rulings P, Q) ---------------


@pytest.mark.parametrize("kind", ["copy", "from"])
def test_ci_strict_form_refused_before_the_log(kind: str) -> None:
    """Ruling P: the strict form runs file-wide first, for both kinds."""
    dockerfile = b"\xef\xbb\xbf" + _DOCKERFILE  # a BOM
    log = _BUILDKIT_COPY_OK if kind == "copy" else _BUILDKIT_FROM_OK
    text = _COPY if kind == "copy" else _FROM
    outcome = match_ci(kind, text, log, dockerfile=dockerfile)  # type: ignore[arg-type]
    assert outcome.evidence == "binding_ambiguous"
    assert outcome.lines == ()
    assert outcome.detail is not None and outcome.detail.startswith("Dockerfile: ")


def test_ci_copy_dockerfile_duplicate_ambiguous() -> None:
    """Ruling Q: an identical COPY in a stage BuildKit may skip refuses
    before the log is read, even when one header would pass."""
    dockerfile = f"FROM a AS x\n{_COPY}\nFROM b\n{_COPY}\n".encode()
    outcome = _ci_copy(_BUILDKIT_COPY_OK, dockerfile=dockerfile)
    assert outcome == Outcome(
        "binding_ambiguous",
        (),
        "Dockerfile: corrected instruction repeated at lines 2, 4",
        None,
    )


@pytest.mark.parametrize(
    "other", ['COPY "docs/setup.md" /x', "ADD $SRC /x", 'COPY ["a", "/x"]']
)
def test_ci_copy_family_unmodelled_refused(other: str) -> None:
    """Ruling Q: a ``$``, quote or unmodelled form in the COPY/ADD family."""
    dockerfile = f"FROM a AS x\n{other}\nFROM b\n{_COPY}\n".encode()
    outcome = _ci_copy(_BUILDKIT_COPY_OK, dockerfile=dockerfile)
    assert outcome.evidence == "binding_ambiguous"
    assert outcome.detail is not None and "unprovable" in outcome.detail


def test_ci_copy_absent_from_dockerfile() -> None:
    """A corrected text that is no instruction of the Dockerfile."""
    outcome = _ci_copy(_BUILDKIT_COPY_OK, dockerfile=b"FROM a\nRUN x\n")
    assert outcome.detail == "Dockerfile: corrected instruction absent"


def test_ci_from_stays_file_wide() -> None:
    """FROM on CI keeps the file-wide rule (c8): a repeated FROM does not
    refuse it, the parse error decides."""
    dockerfile = f"{_FROM}\nRUN x\n{_FROM}\nRUN y\n".encode()
    assert _ci_from(_BUILDKIT_FROM_OK, dockerfile=dockerfile).evidence == "passed"


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
    "#99999999999999999999 [stage-0 1/1] COPY a b\n",
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
            match_ci(
                kind,  # type: ignore[arg-type]
                text,
                text,
                dockerfile=text.encode(),
            ),
            match_ci(
                kind,  # type: ignore[arg-type]
                _COPY,
                text,
                dockerfile=_DOCKERFILE,
            ),
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
    log = f"#5 DONE 0.1s\n#5 [extra 3/3] {_COPY}\n"
    assert _ci_copy(log).evidence == "binding_ambiguous"


def test_buildkit_copy_cached_before_header_not_counted() -> None:
    """Result lines before the header never decide the step."""
    log = f"#5 CACHED\n#5 [extra 3/3] {_COPY}\n#5 DONE 0.1s\n"
    assert _ci_copy(log).evidence == "passed"


def test_buildkit_copy_k_reused_by_later_build() -> None:
    """A later build reusing ``#5`` for a non-stage vertex cannot lend its
    ``DONE`` to our step: the numbering restart refuses the log."""
    log = (
        f"#5 [extra 3/3] {_COPY}\n"
        "#1 [internal] load build definition from Dockerfile\n"
        "#5 exporting to image\n"
        "#5 DONE 0.3s\n"
    )
    assert _ci_copy(log).evidence == "binding_ambiguous"


def test_buildkit_copy_two_definition_loads() -> None:
    """The build definition loaded twice means two builds in one log."""
    load = "#1 [internal] load build definition from Dockerfile\n#1 DONE 0.0s\n"
    log = f"{load}#5 [extra 3/3] {_COPY}\n#5 DONE 0.1s\n{load}"
    outcome = _ci_copy(log)
    assert outcome.evidence == "binding_ambiguous"
    assert outcome.lines == (1, 5)


def test_buildkit_interleaved_lower_k_is_not_a_restart() -> None:
    """A lower ``#k`` already seen (interleaved output) is not a restart."""
    log = (
        "#1 [internal] load build definition from Dockerfile\n"
        f"#5 [extra 3/3] {_COPY}\n"
        "#1 DONE 0.0s\n"
        "#5 DONE 0.1s\n"
    )
    assert _ci_copy(log).evidence == "passed"


def test_buildkit_from_two_builds_refused() -> None:
    """An earlier build's stage header cannot pass our failed build."""
    log = (
        "#1 [internal] load build definition from Dockerfile\n"
        "#4 [stage-0 1/2] FROM x\n"
        "#4 DONE\n"
        "#1 [internal] load build definition from Dockerfile\n"
        "#1 DONE\n"
        "ERROR: failed to solve: invalid reference format\n"
    )
    assert _ci_from(log).evidence == "binding_ambiguous"


def test_buildkit_from_numbering_restart_refused() -> None:
    """A restart of ``#k`` numbering also refuses the FROM row."""
    log = "#4 [stage-0 1/2] FROM x\n#4 DONE\n#2 [internal] load .dockerignore\n"
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


# Round 4, ruling Z: the COPY's BuildKit position from the Dockerfile ---------


@pytest.mark.parametrize(
    "line",
    [
        "ARG X=1",
        "ADD a.txt /a",
        "ONBUILD RUN x",
        "SHELL /bin/sh -c",
        "HEALTHCHECK NONE",
        "EXPOSE 80",
        "VOLUME /data",
        "STOPSIGNAL SIGTERM",
        "ENTRYPOINT python",
        "MAINTAINER me",
    ],
    ids=[
        "arg",
        "add",
        "onbuild",
        "shell",
        "health",
        "expose",
        "volume",
        "stop",
        "entry",
        "maint",
    ],
)
def test_ci_copy_unmodelled_step_refused(line: str) -> None:
    """An instruction whose BuildKit step numbering no C-recording shows
    makes the COPY's position underivable: refused before the log."""
    dockerfile = f"{_FROM}\nWORKDIR /app\n{_COPY}\n{line}\n".encode()
    outcome = _ci_copy(_BUILDKIT_COPY_OK, dockerfile=dockerfile)
    assert outcome.evidence == "binding_ambiguous"
    keyword = line.split()[0]
    assert outcome.detail == (
        f"Dockerfile: {keyword} at line 4: its BuildKit step numbering is not recorded"
    )


@pytest.mark.parametrize(
    "dockerfile",
    [
        f"FROM a AS x\nRUN y\nFROM b\n{_COPY}\n",
        f"FROM --platform=linux/amd64 a\n{_COPY}\n",
        f"FROM a AS Extra\n{_COPY}\n",
        f"FROM a as x\n{_COPY}\n",
    ],
    ids=["stages", "platform", "upper", "lower-as"],
)
def test_ci_copy_unmodelled_stage_refused(dockerfile: str) -> None:
    """Several stages, a FROM flag or an unrecorded stage-name form: the
    stage display BuildKit prints is not recorded."""
    outcome = _ci_copy(_BUILDKIT_COPY_OK, dockerfile=dockerfile.encode())
    assert outcome.evidence == "binding_ambiguous"
    assert outcome.lines == ()


def test_ci_copy_header_must_be_the_derived_position() -> None:
    """A header with the corrected text at any other bracket is refused."""
    log = f"#5 [extra 2/3] {_COPY}\n#5 DONE 0.1s\n"
    assert _ci_copy(log) == Outcome(
        "binding_ambiguous",
        (1,),
        "step header [extra 2/3] is not the Dockerfile's [extra 3/3]",
        None,
    )


def test_buildkit_steps_model() -> None:
    """Steps are FROM/RUN/COPY/WORKDIR; ENV/USER/CMD/LABEL are not."""
    dockerfile = (
        b"FROM a\nENV A=1\nWORKDIR /w\nUSER u\nCOPY a b\nLABEL x=y\nRUN z\nCMD c\n"
    )
    steps = templates.buildkit_steps(dockerfile)
    assert not isinstance(steps, str)
    assert [(i.keyword, p.bracket) for i, p in steps] == [
        ("FROM", "stage-0 1/4"),
        ("WORKDIR", "stage-0 2/4"),
        ("COPY", "stage-0 3/4"),
        ("RUN", "stage-0 4/4"),
    ]
    assert isinstance(templates.buildkit_steps(b""), str)
