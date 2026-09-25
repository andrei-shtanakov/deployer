"""The consumer gate (A §7, §8.5): ``accept_for_fix`` refuses by default and
never raises.

Two sources of documents:

- hand-built raw wire dicts (JSON-shaped) over a try directory written here,
  for every refusal — the invariant violations must be raw, since the model
  would reject them;
- pipeline documents: the committed ``run-1`` / ``run-5`` bundles replayed
  through R, then :func:`prepare` and :func:`decide`, with a hand-built
  confirmed ownership (the signed ``admit-run-*`` bundles are Task 13's data)
  for the acceptance cases and evidence mutations on R's real files.
"""

import copy
import dataclasses
import json
import os
import shutil
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from deployer import runtime as runtime_mod
from deployer.admission import consumer, decide, prepare, templates
from deployer.admission.consumer import (
    Accepted,
    Refused,
    Target,
    accept_for_fix,
)
from deployer.admission.model import AdmissionSection, Binding
from deployer.forge import load_snapshot
from deployer.reproduce.run import reproduce_run
from tests.admission.conftest import confirmed_ownership
from tests.reproduce.bundles import BUNDLES, BundleGh, _containers
from tests.reproduce.conftest import FakeContainers

HEAD = "a" * 40
ART_SHA = "b" * 64
CI_LOG = "line 1\nline 2\nline 3\n"
STDERR = "Error: copy failed\n"


def _target(**overrides: str) -> Target:
    """The target the hand-built documents are bound to."""
    base = dict(
        repo="o/r", head_sha=HEAD, artifact_path="Dockerfile", artifact_sha256=ART_SHA
    )
    base.update(overrides)
    return Target(**base)


def _section(**overrides: object) -> dict[str, Any]:
    """A valid ``admitted`` section as it reads on the wire."""
    base: dict[str, Any] = {
        "verdict": "admitted",
        "binding": {
            "repo": "o/r",
            "head_sha": HEAD,
            "artifact_path": "Dockerfile",
            "artifact_sha256": ART_SHA,
        },
        "ownership": {
            "status": "confirmed",
            "reason": None,
            "key_fingerprint": "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "record_sha256": "c" * 64,
            "snapshot_sha256": "d" * 64,
        },
        "defect": {
            "class": "missing_copy_source",
            "file": "Dockerfile",
            "lines": [2, 2],
            "object": "src",
        },
        "link": {
            "ci": {
                "row": "copy-missing/buildkit",
                "object": "src",
                "evidence_file": "ci.log",
                "evidence_lines": [1, 3],
            },
            "local": {
                "row": "copy-missing/podman",
                "object": "src",
                "evidence_file": "build.stderr",
                "evidence_lines": [1],
            },
            "differences": [
                {"name": "backend", "value": "differs", "allowed": True},
                {"name": "host_arch", "value": "unknown", "allowed": True},
                {"name": "base_image_digests", "value": "unknown", "allowed": True},
                {"name": "ignore_file", "value": "same", "allowed": True},
                {"name": "restoration", "value": "same", "allowed": True},
            ],
        },
        "unmet": [],
    }
    base.update(overrides)
    return base


def _document(section: object = None, **overrides: object) -> dict[str, Any]:
    """A 1.3 verdict document around ``section`` (the valid one by default)."""
    base: dict[str, Any] = {
        "verdict_schema_version": "1.3",
        "reproduction": {"status": "attempted"},
        "admission": _section() if section is None else section,
    }
    base.update(overrides)
    return base


@pytest.fixture()
def try_dir(tmp_path: Path) -> Path:
    """A try directory holding the hand-built documents' evidence files."""
    path = tmp_path / "try"
    path.mkdir()
    (path / "ci.log").write_text(CI_LOG, encoding="utf-8", newline="")
    (path / "build.stderr").write_text(STDERR, encoding="utf-8", newline="")
    return path


def _refused(result: Accepted | Refused, *needles: str) -> str:
    """Assert a refusal whose reason names every needle; return the reason."""
    assert isinstance(result, Refused), result
    for needle in needles:
        assert needle in result.reason, result.reason
    return result.reason


def _with(path: str, value: object) -> dict[str, Any]:
    """The valid section with the dotted ``path`` set to ``value``."""
    section = _section()
    *parents, last = path.split(".")
    node = section
    for key in parents:
        node = node[key]
    node[last] = value
    return section


# --- acceptance ------------------------------------------------------------


def test_valid_hand_built_document_is_accepted(try_dir: Path) -> None:
    """Refuse by default has exactly one exit: this shape."""
    result = accept_for_fix(_document(), try_dir, _target())
    assert isinstance(result, Accepted), result
    assert result.section == AdmissionSection.model_validate(_section())


def test_accept_for_fix_is_the_package_entry_point() -> None:
    """A §7: ``deployer.admission.accept_for_fix`` is the single entry."""
    import deployer.admission as admission

    assert admission.accept_for_fix is accept_for_fix


# --- (1) absent, version, verdict ------------------------------------------


def test_a_1_2_document_without_admission_is_refused(try_dir: Path) -> None:
    """Review Focus 5: a pre-feature document is refused, never raises."""
    document = {"verdict_schema_version": "1.2", "run": {}, "reproduction": {}}
    _refused(accept_for_fix(document, try_dir, _target()), "no admission section")


@pytest.mark.parametrize("version", ["1.2", "1.4", "", None, 1.3, ["1.3"]])
def test_unknown_schema_version_is_refused(try_dir: Path, version: object) -> None:
    """Only the string ``"1.3"`` is read."""
    document = _document(verdict_schema_version=version)
    _refused(
        accept_for_fix(document, try_dir, _target()), "unknown verdict schema version"
    )


def test_missing_schema_version_is_refused(try_dir: Path) -> None:
    """An ``admission`` key without a version is not a 1.3 document."""
    document = _document()
    del document["verdict_schema_version"]
    _refused(
        accept_for_fix(document, try_dir, _target()), "unknown verdict schema version"
    )


@pytest.mark.parametrize("verdict", ["accepted", "ADMITTED", "", None, 1, ["x"]])
def test_unknown_verdict_is_refused(try_dir: Path, verdict: object) -> None:
    """A verdict outside the closed pair."""
    document = _document(_section(verdict=verdict))
    _refused(accept_for_fix(document, try_dir, _target()), "unknown admission verdict")


def test_missing_verdict_is_refused(try_dir: Path) -> None:
    """A section with no ``verdict`` key has an unknown verdict."""
    section = _section()
    del section["verdict"]
    document = _document(section)
    _refused(accept_for_fix(document, try_dir, _target()), "unknown admission verdict")


def test_absence_is_checked_before_version(try_dir: Path) -> None:
    """A §7 order: an absent section is named as absent, whatever the
    version says."""
    document = {"verdict_schema_version": "0.9"}
    _refused(accept_for_fix(document, try_dir, _target()), "no admission section")


def test_version_is_checked_before_verdict(try_dir: Path) -> None:
    """A §7 order: version before verdict."""
    document = _document(_section(verdict="bogus"), verdict_schema_version="1.2")
    _refused(
        accept_for_fix(document, try_dir, _target()), "unknown verdict schema version"
    )


# --- (2) malformed: each §1 / §6.2 invariant ---------------------------------

_NOT_CONFIRMED = {"status": "not_confirmed", "reason": "step 1: no set"}
_UNMET = [{"condition": 1, "reason": "step 1: no set"}]


@pytest.mark.parametrize(
    ("section", "problem"),
    [
        (_section(ownership=_NOT_CONFIRMED), "admitted requires confirmed ownership"),
        (_section(defect=None), "admitted requires a defect and a link"),
        (_section(link=None), "admitted requires a defect and a link"),
        (_section(unmet=_UNMET), "admitted requires an empty unmet list"),
        (
            _with("binding.artifact_sha256", None),
            "admitted requires the binding's artifact_sha256",
        ),
        (
            _section(verdict="insufficient_grounds", defect=None, link=None, unmet=[]),
            "insufficient_grounds requires a non-empty unmet list",
        ),
        (
            _section(verdict="insufficient_grounds", unmet=_UNMET),
            "insufficient_grounds has no defect or link",
        ),
    ],
    ids=[
        "admitted-not-confirmed",
        "admitted-no-defect",
        "admitted-no-link",
        "admitted-with-unmet",
        "admitted-null-artifact-hash",
        "insufficient-empty-unmet",
        "insufficient-with-defect",
    ],
)
def test_invariant_violation_is_refused_naming_it(
    try_dir: Path, section: dict[str, Any], problem: str
) -> None:
    """Ruling 5: the model's invariants apply, and the reason names them."""
    document = _document(section)
    _refused(accept_for_fix(document, try_dir, _target()), "malformed", problem)


@pytest.mark.parametrize(
    ("section", "problem"),
    [
        (_with("binding", {"repo": "o/r"}), "binding.head_sha"),
        (_with("binding.repo", 7), "binding.repo"),
        (_with("ownership.status", "maybe"), "ownership.status"),
        (_with("defect.class", "typo_in_readme"), "defect.class"),
        (_with("defect.lines", [3, 2]), "start <= end"),
        (_with("link.ci.evidence_lines", "1"), "link.ci.evidence_lines"),
        (_section(extra="field"), "extra"),
        (_with("unmet", "none"), "unmet"),
    ],
    ids=[
        "missing-binding-keys",
        "wrong-type",
        "unknown-ownership-status",
        "unknown-defect-class",
        "unordered-lines",
        "non-list-lines",
        "extra-key",
        "non-list-unmet",
    ],
)
def test_wrong_shape_is_refused_naming_it(
    try_dir: Path, section: dict[str, Any], problem: str
) -> None:
    """Missing keys and wrong types are a ``ValidationError``, mapped."""
    document = _document(section)
    _refused(accept_for_fix(document, try_dir, _target()), "malformed", problem)


@pytest.mark.parametrize(
    ("side", "name"),
    [("ci", "build.stderr"), ("local", "ci.log"), ("local", "check.stderr")],
)
def test_untyped_evidence_reference_is_malformed(
    try_dir: Path, side: str, name: str
) -> None:
    """A §6.2: evidence is typed per side (``ci.log``; ``build.stdout`` /
    ``build.stderr``), even when the named file exists."""
    (try_dir / name).write_text(CI_LOG)
    document = _document(_with(f"link.{side}.evidence_file", name))
    _refused(
        accept_for_fix(document, try_dir, _target()),
        "malformed",
        f"{side} evidence_file",
    )


def test_malformed_is_checked_before_insufficient_grounds(try_dir: Path) -> None:
    """A §7 order: an invariant break is named as such, not as a verdict."""
    section = _section(verdict="insufficient_grounds", defect=None, link=None, unmet=[])
    reason = _refused(accept_for_fix(_document(section), try_dir, _target()))
    assert reason.startswith("malformed"), reason


# --- (3) insufficient_grounds -------------------------------------------------


def test_insufficient_grounds_is_refused_with_its_unmet(try_dir: Path) -> None:
    """A valid negative verdict is a refusal carrying its conditions."""
    section = _section(
        verdict="insufficient_grounds",
        ownership=_NOT_CONFIRMED,
        defect=None,
        link=None,
        unmet=_UNMET,
    )
    _refused(
        accept_for_fix(_document(section), try_dir, _target()),
        "insufficient_grounds",
        "(1) step 1: no set",
    )


def test_insufficient_grounds_is_checked_before_binding(try_dir: Path) -> None:
    """A §7 order: the verdict before the binding comparison."""
    section = _section(
        verdict="insufficient_grounds",
        ownership=_NOT_CONFIRMED,
        defect=None,
        link=None,
        unmet=_UNMET,
    )
    result = accept_for_fix(_document(section), try_dir, _target(repo="x/y"))
    _refused(result, "insufficient_grounds")


# --- (4) binding ≠ target -----------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repo", "other/repo"),
        ("head_sha", "f" * 40),
        ("artifact_path", "docker/Dockerfile"),
        ("artifact_sha256", "e" * 64),
    ],
)
def test_binding_differing_from_target_is_refused(
    try_dir: Path, field: str, value: str
) -> None:
    """Each of the four fields on its own."""
    result = accept_for_fix(_document(), try_dir, _target(**{field: value}))
    _refused(result, f"binding {field}", "differs from target")


def test_null_artifact_hash_refused_before_any_comparison() -> None:
    """Ruling 1: ``null`` means "differs", ahead of the field comparisons
    (here the repo differs too, and is not the reason given)."""
    binding = Binding(
        repo="o/r", head_sha=HEAD, artifact_path="Dockerfile", artifact_sha256=None
    )
    result = consumer._binding_refusal(binding, _target(repo="x/y"))
    assert result is not None
    _refused(result, "artifact_sha256 is null", "differs from the target")
    assert "x/y" not in result.reason


def test_binding_is_checked_before_evidence(try_dir: Path) -> None:
    """A §7 order: a foreign target is named even with evidence missing."""
    (try_dir / "ci.log").unlink()
    result = accept_for_fix(_document(), try_dir, _target(head_sha="f" * 40))
    _refused(result, "binding head_sha")


# --- (5) evidence -------------------------------------------------------------


def test_missing_evidence_file_is_refused(try_dir: Path) -> None:
    """``ci.log`` absent from the try directory."""
    (try_dir / "ci.log").unlink()
    _refused(accept_for_fix(_document(), try_dir, _target()), "ci evidence", "missing")


def test_evidence_path_that_is_a_directory_is_refused(try_dir: Path) -> None:
    """An unreadable one: a directory at ``build.stderr``."""
    (try_dir / "build.stderr").unlink()
    (try_dir / "build.stderr").mkdir()
    _refused(
        accept_for_fix(_document(), try_dir, _target()),
        "local evidence file unreadable",
        "not a regular file",
    )


def test_symlinked_evidence_is_not_followed(try_dir: Path, tmp_path: Path) -> None:
    """Ruling 3: a planted symlink cannot redirect the read."""
    outside = tmp_path / "elsewhere.log"
    outside.write_text(CI_LOG)
    (try_dir / "ci.log").unlink()
    (try_dir / "ci.log").symlink_to(outside)
    _refused(accept_for_fix(_document(), try_dir, _target()), "ci evidence", "symlink")


def test_fifo_evidence_does_not_hang(try_dir: Path) -> None:
    """Ruling 3: a planted FIFO is refused without blocking on it."""
    (try_dir / "build.stderr").unlink()
    os.mkfifo(try_dir / "build.stderr")
    _refused(
        accept_for_fix(_document(), try_dir, _target()),
        "local evidence",
        "not a regular file",
    )


def test_oversized_evidence_is_refused(
    try_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling 3: a size cap, refused beyond it."""
    monkeypatch.setattr(consumer, "MAX_EVIDENCE_BYTES", len(CI_LOG) - 1)
    _refused(accept_for_fix(_document(), try_dir, _target()), "ci evidence", "exceeds")


def test_evidence_cap_is_generous() -> None:
    """The cap is not a trap for real logs."""
    assert consumer.MAX_EVIDENCE_BYTES == 64 * 1024 * 1024


@pytest.mark.parametrize(
    "name", ["/etc/passwd", "../ci.log", "sub/../ci.log", "ci\x00.log", ""]
)
def test_evidence_name_outside_plain_relative_is_refused(
    try_dir: Path, name: str
) -> None:
    """Ruling 3 at the reader: absolute, ``..``, NUL, empty — all refused
    (called on the reader directly, since the typed-name check would refuse
    them first through the document)."""
    result = consumer._line_count("ci", name, try_dir)
    assert isinstance(result, Refused), result
    assert "ci evidence file" in result.reason


def test_non_utf8_evidence_is_refused(try_dir: Path) -> None:
    """Ruling 2: a decode error is a refusal."""
    (try_dir / "ci.log").write_bytes(b"line 1\n\xff\xfe\nline 3\n")
    _refused(
        accept_for_fix(_document(), try_dir, _target()), "ci evidence", "not UTF-8"
    )


@pytest.mark.parametrize(
    ("side", "lines"),
    [("ci", [4]), ("ci", [0]), ("ci", [-1]), ("local", [2]), ("ci", [1, 99])],
)
def test_evidence_lines_out_of_range_are_refused(
    try_dir: Path, side: str, lines: list[int]
) -> None:
    """Ruling 2: 1-based, each within the file's line count."""
    document = _document(_with(f"link.{side}.evidence_lines", lines))
    _refused(
        accept_for_fix(document, try_dir, _target()),
        f"{side} evidence",
        "out of range",
    )


def test_no_evidence_lines_is_refused(try_dir: Path) -> None:
    """Refuse by default: evidence that references nothing is no evidence."""
    document = _document(_with("link.local.evidence_lines", []))
    _refused(accept_for_fix(document, try_dir, _target()), "references no lines")


def test_line_count_matches_how_lines_were_written(try_dir: Path) -> None:
    """UTF-8, newlines untranslated, split on ``\\n`` (the producer's rule):
    a CRLF file of three lines has three lines, the last one unterminated."""
    (try_dir / "ci.log").write_bytes("é 1\r\nline 2\r\nline 3".encode())
    assert consumer._line_count("ci", "ci.log", try_dir) == 3
    document = _document(_with("link.ci.evidence_lines", [3]))
    assert isinstance(accept_for_fix(document, try_dir, _target()), Accepted)


def test_try_dir_missing_is_refused(tmp_path: Path) -> None:
    """An ``OSError`` opening the try directory itself is a refusal."""
    result = accept_for_fix(_document(), tmp_path / "nope", _target())
    _refused(result, "ci evidence file", "unreadable")


# --- never raises ---------------------------------------------------------------


class _Exploding(Mapping[str, object]):
    """A mapping whose every access raises."""

    def __getitem__(self, key: str) -> object:
        """Raise."""
        raise ValueError("boom")

    def __iter__(self) -> Iterator[str]:
        """Raise."""
        raise RecursionError("deep")

    def __len__(self) -> int:
        """Raise."""
        raise OSError("io")

    def __contains__(self, key: object) -> bool:
        """Raise."""
        raise ValueError("boom")


def _deep(depth: int) -> dict[str, Any]:
    """A section nested ``depth`` levels under ``binding``."""
    node: dict[str, Any] = {}
    for _ in range(depth):
        node = {"x": node}
    return _section(binding=node)


@pytest.mark.parametrize(
    "document",
    [
        None,
        "admitted",
        ["admission"],
        42,
        _Exploding(),
        {"verdict_schema_version": "1.3", "admission": None},
        {"verdict_schema_version": "1.3", "admission": ["admitted"]},
        {"verdict_schema_version": "1.3", "admission": _Exploding()},
        {"verdict_schema_version": "1.3", "admission": _deep(100_000)},
    ],
    ids=[
        "none",
        "str",
        "list",
        "int",
        "exploding-document",
        "null-section",
        "list-section",
        "exploding-section",
        "deep-nesting",
    ],
)
def test_never_raises(try_dir: Path, document: Any) -> None:
    """Ruling 4: every exception path is a ``Refused``."""
    assert isinstance(accept_for_fix(document, try_dir, _target()), Refused)


@pytest.mark.parametrize(
    "error",
    [
        OSError("io"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
        RecursionError("deep"),
        ValueError("v"),
    ],
)
def test_unexpected_errors_are_refusals(
    try_dir: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Ruling 4: an error raised anywhere below the gate is mapped."""

    def explode(*_: object) -> bytes:
        raise error

    monkeypatch.setattr(consumer, "read_in_tree", explode)
    result = accept_for_fix(_document(), try_dir, _target())
    _refused(result, type(error).__name__)


# --- pipeline documents (R replay → prepare → decide) ---------------------------


@dataclass(frozen=True)
class Pipeline:
    """A decided run's verdict document, its try directory and target."""

    document: dict[str, Any]
    try_dir: Path
    target: Target


def _pipeline(case: str, root: Path) -> Pipeline:
    """Replay ``case`` through R, prepare its facts, decide with a confirmed
    ownership over the bundle's listing, and wrap the section in a 1.3
    document as ``render_verdict`` would (JSON round-trip)."""
    bundle = BUNDLES / case
    run = load_snapshot((bundle / "snapshot.json").read_text())
    with pytest.MonkeyPatch.context() as mp:
        fake = FakeContainers()
        mp.setattr(runtime_mod, "container_run", fake)
        rt, env = _containers(bundle, fake)
        section = reproduce_run(
            run,
            gh=BundleGh(bundle, root / "bundle"),
            rt=rt,
            runtime_error=None,
            env=env,
            root=root / "work",
            build_timeout=60,
        )
    (root / "trust").mkdir()
    trust = {"DEPLOYER_TRUST_DIR": str(root / "trust")}
    facts = prepare(run, section, root / "work", trust)
    ownership = confirmed_ownership(run.head_sha, facts.head_listing)
    admission = decide(dataclasses.replace(facts, ownership=ownership))
    assert admission.verdict == "admitted", admission.unmet
    document = {
        "verdict_schema_version": "1.3",
        "reproduction": json.loads(section.model_dump_json()),
        "admission": json.loads(admission.model_dump_json()),
    }
    b = facts.binding
    assert b.artifact_sha256 is not None and section.try_dir is not None
    target = Target(b.repo, b.head_sha, b.artifact_path, b.artifact_sha256)
    return Pipeline(document, root / "work" / section.try_dir, target)


@pytest.fixture(scope="module")
def pipelines(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Pipeline]:
    """Each bundle decided once for the module."""
    return {
        case: _pipeline(case, tmp_path_factory.mktemp(case))
        for case in ("run-1", "run-5")
    }


@pytest.fixture()
def pipeline_copy(
    pipelines: dict[str, Pipeline], tmp_path: Path
) -> Callable[[str], Pipeline]:
    """A per-test copy of a decided run's try directory, safe to mutate."""

    def make(case: str) -> Pipeline:
        original = pipelines[case]
        copied = tmp_path / "try"
        shutil.copytree(original.try_dir, copied)
        return dataclasses.replace(
            original, document=copy.deepcopy(original.document), try_dir=copied
        )

    return make


@pytest.mark.parametrize("case", ["run-1", "run-5"])
def test_pipeline_document_is_accepted_against_its_own_target(
    pipelines: dict[str, Pipeline], case: str
) -> None:
    """A §8.5: a decided ``admitted`` run, against its own target, over R's
    real ``ci.log`` / ``build.*`` files."""
    p = pipelines[case]
    result = accept_for_fix(p.document, p.try_dir, p.target)
    assert isinstance(result, Accepted), result
    assert result.section.verdict == "admitted"


@pytest.mark.parametrize("case", ["run-1", "run-5"])
def test_pipeline_document_against_the_other_target_is_refused(
    pipelines: dict[str, Pipeline], case: str
) -> None:
    """A §8.5: each run accepts only against its own target."""
    other = "run-5" if case == "run-1" else "run-1"
    p = pipelines[case]
    result = accept_for_fix(p.document, p.try_dir, pipelines[other].target)
    _refused(result, "binding")


def test_pipeline_missing_ci_log_is_refused(
    pipeline_copy: Callable[[str], Pipeline],
) -> None:
    """R's try without the preparation layer's ``ci.log``."""
    p = pipeline_copy("run-1")
    (p.try_dir / "ci.log").unlink()
    _refused(accept_for_fix(p.document, p.try_dir, p.target), "ci evidence", "missing")


def test_pipeline_truncated_local_evidence_is_out_of_range(
    pipeline_copy: Callable[[str], Pipeline],
) -> None:
    """R's ``build.stderr`` emptied after the decision."""
    p = pipeline_copy("run-5")
    local = p.document["admission"]["link"]["local"]["evidence_file"]
    (p.try_dir / local).write_text("")
    _refused(
        accept_for_fix(p.document, p.try_dir, p.target),
        "local evidence",
        "out of range",
    )


# --- fix round 1: the link agrees with the table, the defect, the binding ------


def _mutated(mutate: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    """The valid document with ``mutate`` applied to its section."""
    section = _section()
    mutate(section)
    return _document(section)


def _ci(key: str, value: object) -> Callable[[dict[str, Any]], object]:
    """Set ``link.ci.<key>``."""
    return lambda s: s["link"]["ci"].__setitem__(key, value)


def _drop_difference(name: str) -> Callable[[dict[str, Any]], object]:
    """Remove the ``name`` difference."""

    def apply(s: dict[str, Any]) -> None:
        diffs = s["link"]["differences"]
        s["link"]["differences"] = [d for d in diffs if d["name"] != name]

    return apply


def _set_rows(ci: str, local: str) -> Callable[[dict[str, Any]], object]:
    """Set both sides' row ids."""

    def apply(s: dict[str, Any]) -> None:
        s["link"]["ci"]["row"] = ci
        s["link"]["local"]["row"] = local

    return apply


def _objects(ci: object, local: object) -> Callable[[dict[str, Any]], object]:
    """Set both sides' objects."""

    def apply(s: dict[str, Any]) -> None:
        s["link"]["ci"]["object"] = ci
        s["link"]["local"]["object"] = local

    return apply


def _from_args(s: dict[str, Any]) -> None:
    """A consistent ``from_argument_count`` section."""
    s["defect"].update({"class": "from_argument_count", "object": "FROM a b"})
    _set_rows("from-args/buildkit", "from-args/podman")(s)
    _objects(None, None)(s)


_REFUSED_DIFF = {"name": "backend", "value": "differs", "allowed": False}


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (
            lambda s: s["link"]["differences"].__setitem__(0, _REFUSED_DIFF),
            "not allowed in an admitted section",
        ),
        (lambda s: s["link"].__setitem__("differences", []), "are missing"),
        (_drop_difference("restoration"), "['restoration'] are missing"),
        (_drop_difference("host_arch"), "['host_arch'] are missing"),
        (
            lambda s: s["link"]["differences"].append(
                {"name": "restoration", "value": "same", "allowed": True}
            ),
            "['restoration'] appear more than once",
        ),
        (_ci("row", "anything"), "ci row 'anything' is not in the template table"),
        (_ci("row", "copy-missing/podman"), "ci row copy-missing/podman is a local"),
        (
            _set_rows("from-args/buildkit", "from-args/podman"),
            "is not of class missing_copy_source",
        ),
        (_objects("other", "x"), "ci object 'other' disagrees"),
        (_objects("src", "x"), "local object 'x' disagrees"),
        (
            lambda s: s["defect"].__setitem__("class", "from_argument_count"),
            "is not of class from_argument_count",
        ),
        (
            lambda s: (_from_args(s), _objects("src", None)(s)),
            "ci object 'src' disagrees with the from_argument_count defect",
        ),
        (
            lambda s: s["defect"].__setitem__("file", "other/Dockerfile"),
            "defect file 'other/Dockerfile' is not the binding's artifact_path",
        ),
        (_ci("evidence_lines", [3, 1]), "not strictly increasing"),
        (_ci("evidence_lines", [1, 1]), "not strictly increasing"),
        (_ci("evidence_lines", [True]), "link.ci.evidence_lines.0"),
        (_ci("evidence_lines", ["1"]), "link.ci.evidence_lines.0"),
        (_ci("evidence_lines", [1.0]), "link.ci.evidence_lines.0"),
        (lambda s: s["binding"].__setitem__("repo", b"o/r"), "binding.repo"),
        (lambda s: s.pop("unmet"), "the unmet key is missing"),
    ],
    ids=[
        "difference-not-allowed",
        "differences-empty",
        "restoration-missing",
        "required-dimension-missing",
        "difference-duplicated",
        "row-not-in-table",
        "row-of-the-other-side",
        "rows-of-another-class",
        "ci-object-disagrees",
        "local-object-disagrees",
        "class-without-its-rows",
        "from-args-with-an-object",
        "defect-file-not-artifact",
        "lines-unsorted",
        "lines-duplicated",
        "lines-bool",
        "lines-str",
        "lines-float",
        "repo-bytes",
        "unmet-missing",
    ],
)
def test_malformed_link_or_wire_is_refused(
    try_dir: Path, mutate: Callable[[dict[str, Any]], object], problem: str
) -> None:
    """Each probe once accepted; now a step-2 "malformed" refusal."""
    result = accept_for_fix(_mutated(mutate), try_dir, _target())
    _refused(result, "malformed", problem)


def test_consistent_from_args_section_is_accepted(try_dir: Path) -> None:
    """The class-specific object rule has an accepting side too."""
    result = accept_for_fix(_mutated(_from_args), try_dir, _target())
    assert isinstance(result, Accepted), result


@pytest.mark.parametrize(
    "reproduction",
    [{"status": "not_attempted"}, {}, None, "attempted", {"status": None}],
)
def test_admission_without_an_attempted_reproduction_is_refused(
    try_dir: Path, reproduction: object
) -> None:
    """A §6.2: the section exists only for an ``attempted`` reproduction."""
    document = _document(reproduction=reproduction)
    _refused(
        accept_for_fix(document, try_dir, _target()),
        "malformed",
        "is not 'attempted'",
    )


def test_missing_reproduction_is_refused(try_dir: Path) -> None:
    """No ``reproduction`` key at all contradicts an admission too."""
    document = _document()
    del document["reproduction"]
    _refused(accept_for_fix(document, try_dir, _target()), "is not 'attempted'")


@pytest.mark.parametrize(("lines", "accepted"), [([3], False), ([1], True)])
def test_carriage_returns_do_not_make_lines(
    try_dir: Path, lines: list[int], accepted: bool
) -> None:
    """The producer's rule, ``\\n`` only: ``a\\rb\\rc\\rd\\n`` is one line."""
    (try_dir / "ci.log").write_bytes(b"a\rb\rc\rd\n")
    document = _document(_with("link.ci.evidence_lines", lines))
    result = accept_for_fix(document, try_dir, _target())
    assert isinstance(result, Accepted) is accepted, result
    if not accepted:
        _refused(result, "ci evidence", "out of range", "1 lines")


@pytest.mark.parametrize(
    ("text", "count"),
    [("", 0), ("a", 1), ("a\n", 1), ("a\n\n", 2), ("a\nb", 2), ("\n", 1)],
)
def test_line_count_drops_only_the_final_empty_element(
    try_dir: Path, text: str, count: int
) -> None:
    """``split("\\n")`` less the empty element after a final ``\\n``."""
    (try_dir / "ci.log").write_text(text, encoding="utf-8", newline="")
    assert consumer._line_count("ci", "ci.log", try_dir) == count


def test_split_lines_is_the_templates_numbering() -> None:
    """One rule: the matchers' line ``i`` is ``split_lines`` element ``i``."""
    text = "x\r\n  y \x0cz\n\u2028w"
    assert templates.split_lines(text) == text.split("\n")
    assert templates._lines(text) == [p.strip() for p in text.split("\n")]
