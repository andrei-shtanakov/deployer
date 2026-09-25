"""The pure decision (A §6.1), level D (A §8.3): hand-built verified facts over
the committed ``run-1`` / ``run-5`` bundles, one targeted mutation per case.

R's reproduction section is the real one: each bundle is replayed once through
R against the same fakes as R's acceptance test (§8.A), so every fact but the
hand-built ownership comes from the committed recordings.
"""

import dataclasses
import hashlib
import json
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from deployer import runtime as runtime_mod
from deployer.admission import templates
from deployer.admission.decide import VerifiedFacts, decide
from deployer.admission.model import AdmissionSection, Binding
from deployer.admission.ownership import OwnershipFacts
from deployer.forge import load_snapshot
from deployer.provenance.model import TreeRow
from deployer.reproduce.dockerfile import Instruction, parse
from deployer.reproduce.model import (
    InstructionRef,
    Location,
    ReproductionCheck,
    ReproductionSection,
    ReproEvidence,
    Restoration,
)
from deployer.reproduce.run import reproduce_run
from deployer.reproduce.shape import job_text
from tests.admission.conftest import confirmed_ownership
from tests.reproduce.bundles import BUNDLES, BundleGh, _containers
from tests.reproduce.conftest import FakeContainers

COPY_LINE = 11
"""``COPY docs/setup.md ./setup.md`` in ``run-1/tree/Dockerfile``."""
SHA_A = "a" * 64
SHA_B = "b" * 64
Mutation = Callable[[VerifiedFacts], VerifiedFacts]


def _replay(case: str) -> ReproductionSection:
    """R's real reproduction section for a committed bundle (as R §8.A)."""
    bundle = BUNDLES / case
    with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as tmp:
        fake = FakeContainers()
        mp.setattr(runtime_mod, "container_run", fake)
        rt, env = _containers(bundle, fake)
        return reproduce_run(
            load_snapshot((bundle / "snapshot.json").read_text()),
            gh=BundleGh(bundle, Path(tmp) / "bundle"),
            rt=rt,
            runtime_error=None,
            env=env,
            root=Path(tmp) / "work",
            build_timeout=60,
        )


def _listing(case: str) -> list[TreeRow]:
    """The ``head_sha`` listing R stores, from the bundle's tree listing."""
    body = json.loads((BUNDLES / case / "tree-listing.json").read_text())
    return [TreeRow(**entry) for entry in body["tree"]]


def _base(case: str) -> VerifiedFacts:
    """The admitted shape of a committed bundle."""
    bundle = BUNDLES / case
    run = load_snapshot((bundle / "snapshot.json").read_text())
    dockerfile = (bundle / "tree" / "Dockerfile").read_bytes()
    listing = _listing(case)
    return VerifiedFacts(
        binding=Binding(
            repo=run.repo,
            head_sha=run.head_sha,
            artifact_path="Dockerfile",
            artifact_sha256=hashlib.sha256(dockerfile).hexdigest(),
        ),
        ownership=confirmed_ownership(run.head_sha, listing),
        reproduction=_replay(case),
        parsed=parse(dockerfile.decode()),
        head_listing=listing,
        head_listing_complete=True,
        ci_text=job_text(run.jobs[0]),
        ci_evidence_file="ci.log",
        local_stdout=(bundle / "local.stdout").read_text(),
        local_stderr=(bundle / "local.stderr").read_text(),
        ignore_hashes=((None, None), (None, None)),
        syntax_directive_ci=None,
        syntax_directive_local=None,
    )


BASES: dict[str, VerifiedFacts] = {}


@pytest.fixture(scope="module", autouse=True)
def _bases() -> Iterator[None]:
    """Replay each bundle once for the module."""
    BASES.update({case: _base(case) for case in ("run-1", "run-5")})
    yield
    BASES.clear()


def _facts(case: str = "run-1", **overrides: Any) -> VerifiedFacts:
    """The admitted ``case`` shape with the named fields replaced."""
    return dataclasses.replace(BASES[case], **overrides)


# --- mutation helpers -----------------------------------------------------


def _instruction(line: int, args: str, keyword: str = "COPY") -> Mutation:
    """Replace the instruction starting at ``line``, keeping its span."""

    def apply(f: VerifiedFacts) -> VerifiedFacts:
        out = [
            Instruction(keyword, args, i.first_line, i.last_line)
            if i.first_line == line
            else i
            for i in f.parsed.instructions
        ]
        return _facts_of(f, parsed=dataclasses.replace(f.parsed, instructions=out))

    return apply


def _appended(keyword: str, args: str, line: int) -> Mutation:
    """Add one more instruction at ``line`` (after the file's last one)."""

    def apply(f: VerifiedFacts) -> VerifiedFacts:
        extra = Instruction(keyword, args, line, line)
        parsed = dataclasses.replace(
            f.parsed, instructions=[*f.parsed.instructions, extra]
        )
        return _facts_of(f, parsed=parsed)

    return apply


def _parsed(**changes: Any) -> Mutation:
    """Replace fields of the parsed Dockerfile."""
    return lambda f: _facts_of(
        f,
        parsed=dataclasses.replace(f.parsed, **changes),
    )


def _snapshot_row(path: str, mode: str = "100644") -> Mutation:
    """Add an entry to the verified source snapshot's listing."""

    def apply(f: VerifiedFacts) -> VerifiedFacts:
        snapshot = f.ownership.snapshot
        assert snapshot is not None
        row = TreeRow(path=path, mode=mode, type="blob", sha=SHA_A[:40])
        tree = [*snapshot.tree, row]
        new = snapshot.model_copy(update={"tree": tree})
        return _facts_of(f, ownership=dataclasses.replace(f.ownership, snapshot=new))

    return apply


def _head_row(path: str, mode: str = "100644") -> Mutation:
    """Add an entry to the ``head_sha`` listing."""
    row = TreeRow(path=path, mode=mode, type="blob", sha=SHA_A[:40])
    return lambda f: _facts_of(f, head_listing=[*f.head_listing, row])


def _text(field: str, old: str, new: str) -> Mutation:
    """Replace ``old`` by ``new`` in one text fact; ``old`` must occur."""

    def apply(f: VerifiedFacts) -> VerifiedFacts:
        value = getattr(f, field)
        assert old in value
        return _facts_of(f, **{field: value.replace(old, new)})

    return apply


def _set(**changes: Any) -> Mutation:
    """Replace facts as given."""
    return lambda f: _facts_of(f, **changes)


def _repro(**changes: Any) -> Mutation:
    """Replace fields of R's section."""
    return lambda f: _facts_of(
        f, reproduction=f.reproduction.model_copy(update=changes)
    )


def _comparison(**changes: Any) -> Mutation:
    """Replace fields of R's comparison."""

    def apply(f: VerifiedFacts) -> VerifiedFacts:
        comparison = f.reproduction.comparison
        assert comparison is not None
        return _repro(comparison=comparison.model_copy(update=changes))(f)

    return apply


def _dimension(name: str, value: str) -> Mutation:
    """Set one comparison dimension."""

    def apply(f: VerifiedFacts) -> VerifiedFacts:
        comparison = f.reproduction.comparison
        assert comparison is not None
        return _comparison(dimensions={**comparison.dimensions, name: value})(f)

    return apply


def _extra_check(check: ReproductionCheck) -> Mutation:
    """Add one more check result to R's section."""
    return lambda f: _repro(checks=[*f.reproduction.checks, check])(f)


def _facts_of(f: VerifiedFacts, **changes: Any) -> VerifiedFacts:
    return dataclasses.replace(f, **changes)


def _failed(check_id: str, finding: str, line: int) -> ReproductionCheck:
    return ReproductionCheck(
        check_id=check_id,
        status="failed",
        finding=finding,
        location=Location(file="Dockerfile", lines=(line, line)),
        evidence=[ReproEvidence(kind="log_excerpt", text="x")],
    )


# --- baselines ------------------------------------------------------------


def _roundtrip(section: AdmissionSection) -> None:
    text = section.model_dump_json()
    assert AdmissionSection.model_validate_json(text) == section
    if section.defect is not None:
        assert '"class"' in text


def test_run_1_admits_missing_copy_source() -> None:
    section = decide(_facts("run-1"))
    assert section.verdict == "admitted", section.unmet
    assert section.defect is not None and section.link is not None
    assert section.defect.cls == "missing_copy_source"
    assert section.defect.lines == (COPY_LINE, COPY_LINE)
    # rule (g): one object on the defect and on both sides
    assert section.defect.object == "docs/setup.md"
    assert section.link.ci.object == section.link.local.object == "docs/setup.md"
    assert section.link.ci.row == "copy-missing/buildkit"
    assert section.link.local.row == "copy-missing/podman"
    assert section.link.ci.evidence_file == "ci.log"
    assert section.link.local.evidence_file == "build.stderr"
    assert section.link.local.evidence_lines == [1]
    assert section.link.ci.evidence_lines
    decisions = {d.name: (d.value, d.allowed) for d in section.link.differences}
    assert decisions["backend"] == ("differs", True)
    assert decisions["host_arch"] == ("unknown", True)
    assert decisions["base_image_digests"] == ("unknown", True)
    assert decisions["ignore_file"] == ("same", True)
    _roundtrip(section)


def test_run_5_admits_from_argument_count() -> None:
    section = decide(_facts("run-5"))
    assert section.verdict == "admitted", section.unmet
    assert section.defect is not None and section.link is not None
    assert section.defect.cls == "from_argument_count"
    assert section.defect.lines == (1, 1)
    # rule (g): the FROM text is the object; the builders print none
    assert section.defect.object == "FROM python:3.12-slim extra"
    assert section.link.ci.object is None and section.link.local.object is None
    assert section.link.ci.row == "from-args/buildkit"
    assert section.link.local.row == "from-args/podman"
    _roundtrip(section)


def test_decide_is_pure() -> None:
    facts = _facts("run-1")
    assert decide(facts) == decide(facts)


# --- level D, A §8.3 ------------------------------------------------------

D_CASES: list[tuple[str, str, Mutation, int, str]] = [
    # (2) form
    (
        "glob_source",
        "run-1",
        _instruction(COPY_LINE, "docs/*.md ./setup.md"),
        2,
        "glob source docs/*.md",
    ),
    (
        "from_flag",
        "run-1",
        _instruction(COPY_LINE, "--from=x docs/setup.md ./setup.md"),
        2,
        "flag --from=x excluded",
    ),
    (
        "remote_add",
        "run-1",
        _instruction(COPY_LINE, "https://example.com/setup.md ./setup.md", "ADD"),
        2,
        "remote source",
    ),
    ("heredoc", "run-1", _instruction(COPY_LINE, "<<EOF ./setup.md"), 2, "heredoc"),
    (
        "json_escape",
        "run-1",
        _instruction(COPY_LINE, '["docs\\u002fsetup.md", "./setup.md"]'),
        2,
        "escape in JSON form",
    ),
    (
        "unread_escape_directive",
        "run-1",
        _parsed(escape_directive="`"),
        2,
        "not fully read",
    ),
    # (2) absence
    (
        "present_in_snapshot",
        "run-1",
        _snapshot_row("docs/setup.md"),
        2,
        "source snapshot: docs/setup.md present",
    ),
    (
        "child_in_snapshot",
        "run-1",
        _snapshot_row("docs/setup.md/part"),
        2,
        "source snapshot: entry docs/setup.md/part under",
    ),
    (
        "present_at_head",
        "run-1",
        _head_row("docs/setup.md"),
        2,
        "head_sha listing: docs/setup.md present",
    ),
    (
        "ancestor_symlink",
        "run-1",
        _snapshot_row("docs", "120000"),
        2,
        "source snapshot: ancestor docs is a symlink",
    ),
    (
        "ancestor_submodule",
        "run-1",
        _snapshot_row("docs", "160000"),
        2,
        "source snapshot: ancestor docs is a submodule",
    ),
    (
        "ancestor_symlink_at_head",
        "run-1",
        _head_row("docs", "120000"),
        2,
        "head_sha listing: ancestor docs is a symlink",
    ),
    (
        "ancestor_submodule_at_head",
        "run-1",
        _head_row("docs", "160000"),
        2,
        "head_sha listing: ancestor docs is a submodule",
    ),
    # (2) syntax
    (
        "other_syntax_check",
        "run-1",
        _extra_check(_failed("syntax_keyword", "unknown instruction X", 3)),
        2,
        "check syntax_keyword not admissible",
    ),
    # (3) rows
    (
        "ci_no_row",
        "run-1",
        _text("ci_text", "failed to calculate checksum of ref", "failed to read ref"),
        3,
        "CI output matches no missing_copy_source row",
    ),
    (
        "local_no_row",
        "run-1",
        _set(local_stderr="Error: building failed for an unknown reason\n"),
        3,
        "local output matches no missing_copy_source row",
    ),
    # (3) object
    (
        "object_not_source",
        "run-1",
        _text("local_stderr", '"/docs/setup.md"', '"/docs/other.md"'),
        3,
        "local object docs/other.md not bound",
    ),
    (
        "two_object_candidates",
        "run-1",
        _instruction(COPY_LINE, "docs/setup.md ./docs/setup.md ./"),
        3,
        "object docs/setup.md not bound: 2 candidates",
    ),
    # (3) instruction
    (
        "two_identical_copy",
        "run-1",
        _appended("COPY", "docs/setup.md ./setup.md", 21),
        3,
        "local binding ambiguous",
    ),
    (
        "two_bad_froms",
        "run-5",
        _appended("FROM", "python:3.12-slim other", 21),
        3,
        "2 FROM candidates",
    ),
    # (3) same check
    (
        "other_parse_error_same_line",
        "run-5",
        _text(
            "ci_text",
            "FROM requires either one or three arguments",
            "unknown flag: platfrom",
        ),
        3,
        "not the same check",
    ),
    # (3) differences
    (
        "ignore_path_differs",
        "run-1",
        _set(ignore_hashes=((None, None), (".containerignore", SHA_A))),
        3,
        "difference ignore_file=same not allowed",
    ),
    (
        "ignore_content_differs",
        "run-1",
        _set(ignore_hashes=((".dockerignore", SHA_A), (".dockerignore", SHA_B))),
        3,
        "difference ignore_file=same not allowed",
    ),
    (
        "approximation",
        "run-1",
        _repro(restoration=Restoration(state="approximation", sha="0" * 40)),
        3,
        "restoration approximation, not exact",
    ),
    (
        "unknown_required_dimension",
        "run-1",
        _dimension("restoration", "unknown"),
        3,
        "difference restoration=unknown not allowed",
    ),
    # fix round 1: an empty (or foreign) head listing proves no absence
    (
        "empty_head_listing",
        "run-1",
        _set(head_listing=[]),
        2,
        "head listing lacks Dockerfile; absence at head_sha not provable",
    ),
    # T11 fix round 1: a listing R marked truncated proves no absence, even
    # with every other fact exact
    (
        "incomplete_head_listing",
        "run-1",
        _set(head_listing_complete=False),
        2,
        "head_sha listing incomplete; absence not provable",
    ),
    # fix round 1: "absent on both sides" means all four values are None
    (
        "ignore_hash_without_path",
        "run-1",
        _set(ignore_hashes=((None, SHA_A), (None, None))),
        3,
        "difference ignore_file=same not allowed",
    ),
]


def _assert_refused(
    section: AdmissionSection, condition: int, reason: str, *, only: bool = True
) -> None:
    """Refused on ``condition`` with ``reason``; with ``only``, on it alone."""
    assert section.verdict == "insufficient_grounds"
    assert section.defect is None and section.link is None
    by_condition = {u.condition: u.reason for u in section.unmet}
    assert condition in by_condition, section.unmet
    if only:
        assert set(by_condition) == {condition}, section.unmet
    assert reason in by_condition[condition], section.unmet
    _roundtrip(section)


@pytest.mark.parametrize(
    ("case", "mutate", "condition", "reason"),
    [pytest.param(c, m, n, r, id=i) for i, c, m, n, r in D_CASES],
)
def test_level_d_refusal(
    case: str, mutate: Mutation, condition: int, reason: str
) -> None:
    _assert_refused(decide(mutate(_facts(case))), condition, reason)


def test_backend_pair_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = tuple(r for r in templates.ROWS if r.id != "copy-missing/podman")
    monkeypatch.setattr(templates, "ROWS", rows)
    _assert_refused(decide(_facts("run-1")), 3, "difference backend=differs")


def test_every_required_case_is_covered() -> None:
    required = {
        "glob_source",
        "from_flag",
        "remote_add",
        "heredoc",
        "json_escape",
        "unread_escape_directive",
        "present_in_snapshot",
        "child_in_snapshot",
        "present_at_head",
        "ancestor_symlink",
        "ancestor_submodule",
        "other_syntax_check",
        "ci_no_row",
        "local_no_row",
        "object_not_source",
        "two_object_candidates",
        "two_identical_copy",
        "two_bad_froms",
        "other_parse_error_same_line",
        "ignore_path_differs",
        "ignore_content_differs",
        "approximation",
        "unknown_required_dimension",
    }
    assert required <= {case[0] for case in D_CASES}


# --- the binding rules (a)-(g) and the other conditions -------------------


def _ci_ref(facts: VerifiedFacts) -> InstructionRef:
    """R's recorded CI instruction."""
    comparison = facts.reproduction.comparison
    assert comparison is not None and comparison.ci_instruction is not None
    return comparison.ci_instruction


def test_copy_span_differs_from_rs_ci_instruction() -> None:
    """Rule (a): ``CopyMatch.lines`` must equal R's CI instruction span."""
    facts = _facts("run-1")
    moved = _ci_ref(facts).model_copy(update={"lines": (10, 10)})
    section = decide(_comparison(ci_instruction=moved)(facts))
    _assert_refused(section, 3, "is not R's CI instruction")


def test_from_line_differs_from_rs_ci_instruction() -> None:
    """Rule (a): ``FromMatch.line`` must equal the start of R's CI span."""
    facts = _facts("run-5")
    moved = _ci_ref(facts).model_copy(update={"lines": (2, 2)})
    section = decide(_comparison(ci_instruction=moved)(facts))
    _assert_refused(section, 3, "CI parse line 1 is not R's CI instruction")


def test_ci_log_matching_both_classes_is_refused() -> None:
    """Rule (b): one CI log matching rows of both classes."""
    extra = (
        "\nERROR: failed to solve: dockerfile parse error on line 1: "
        "FROM requires either one or three arguments"
    )
    facts = _facts("run-1")
    section = decide(_set(ci_text=facts.ci_text + extra)(facts))
    _assert_refused(section, 3, "CI output matches rows of both classes")


def test_ci_object_not_the_absent_source() -> None:
    """Rule (d) on the CI side: its ``<P>`` is bound like the local one."""
    section = decide(
        _text("ci_text", '"/docs/setup.md"', '"/docs/other.md"')(_facts("run-1"))
    )
    _assert_refused(section, 3, "CI object docs/other.md not bound")


def test_object_normalises_to_the_absent_source() -> None:
    """Rule (d): ``/<P>`` is normalised before binding (``./`` collapses)."""
    mutate = _text("local_stderr", '"/docs/setup.md"', '"/./docs//setup.md"')
    assert decide(mutate(_facts("run-1"))).verdict == "admitted"


def test_object_outside_the_alphabet_is_not_bound() -> None:
    mutate = _text("local_stderr", '"/docs/setup.md"', '"/docs/$X.md"')
    _assert_refused(decide(mutate(_facts("run-1"))), 3, "normalisation ambiguous")


def test_bad_from_elsewhere_than_the_ci_line() -> None:
    """Rule (e): the single bad FROM must be the instruction at ``<N>``."""
    good = _instruction(1, "python:3.12-slim", "FROM")
    bad = _appended("FROM", "python:3.12-slim extra", 21)
    section = decide(bad(good(_facts("run-5"))))
    _assert_refused(section, 3, "the bad FROM is at (21, 21)")


def test_ci_side_alone_never_admits() -> None:
    """Rule (f): an empty local side refuses even with CI and absence proven."""
    section = decide(_set(local_stdout="", local_stderr="")(_facts("run-1")))
    _assert_refused(section, 3, "local output matches no")


def test_ownership_not_confirmed() -> None:
    facts = _facts("run-1")
    refused = OwnershipFacts(
        status="not_confirmed",
        step=3,
        reason="unknown key",
        key_fingerprint=None,
        record_sha256=SHA_A,
        snapshot_sha256=SHA_B,
        record=None,
        snapshot=None,
    )
    section = decide(_facts_of(facts, ownership=refused))
    _assert_refused(section, 1, "ownership not confirmed: step 3: unknown key")
    # no snapshot, so no absence reason is claimed for it
    assert [u.condition for u in section.unmet] == [1]


def test_several_candidates_are_refused() -> None:
    check = _failed("copy_sources", "source other.md absent from the context", 7)
    section = decide(_extra_check(check)(_facts("run-1")))
    _assert_refused(section, 2, "2 defect candidates, one required")


def test_no_candidate_is_refused_without_a_link_claim_on_the_defect() -> None:
    facts = _facts("run-1")
    checks = [c for c in facts.reproduction.checks if c.status != "failed"]
    section = decide(_repro(checks=checks)(facts))
    _assert_refused(section, 2, "no admissible defect")
    assert all(u.condition != 3 for u in section.unmet)


def test_syntax_directive_on_either_side_is_refused() -> None:
    for side in ("syntax_directive_ci", "syntax_directive_local"):
        mutate = _set(**{side: "docker/dockerfile:1"})
        section = decide(mutate(_facts("run-1")))
        _assert_refused(section, 3, "unknown dialect")


def test_comparison_not_reproduced_is_refused() -> None:
    mutate = _comparison(state="different_failure")
    _assert_refused(decide(mutate(_facts("run-1"))), 3, "comparison different_failure")


def test_reproduction_not_attempted() -> None:
    section = ReproductionSection(status="refused", refusal="no build step")
    facts = _facts_of(_facts("run-1"), reproduction=section)
    out = decide(facts)
    _assert_refused(out, 2, "reproduction refused", only=False)
    _assert_refused(out, 3, "reproduction refused", only=False)
    assert {u.condition for u in out.unmet} == {2, 3}


def test_both_ignore_files_same_path_and_hash_are_allowed() -> None:
    mutate = _set(ignore_hashes=((".dockerignore", SHA_A), (".dockerignore", SHA_A)))
    assert decide(mutate(_facts("run-1"))).verdict == "admitted"


def test_host_arch_differs_is_not_tolerated() -> None:
    mutate = _dimension("host_arch", "differs")
    _assert_refused(decide(mutate(_facts("run-1"))), 3, "host_arch=differs")


def test_foreign_head_listing_is_refused() -> None:
    """Fix round 1: a listing without the artifact is not ``head_sha``'s."""
    facts = _facts("run-1")
    foreign = [r for r in facts.head_listing if r.path != "Dockerfile"]
    section = decide(_set(head_listing=foreign)(facts))
    _assert_refused(section, 2, "head listing lacks Dockerfile")


def test_inconsistent_ownership_facts_are_refused_not_raised() -> None:
    """Fix round 1: ``decide`` is total over facts the model rejects."""
    facts = _facts("run-1")
    broken = dataclasses.replace(facts.ownership, key_fingerprint=None)
    section = decide(_facts_of(facts, ownership=broken))
    _assert_refused(section, 1, "ownership facts inconsistent")
    assert section.ownership.status == "not_confirmed"


def test_ci_block_text_must_be_the_defect_instruction() -> None:
    """Fix round 1: the CI side binds by span *and* by instruction text."""
    old, new = "COPY docs/setup.md ./setup.md", "COPY docs/setup.md ./other.md"
    section = decide(_text("ci_text", old, new)(_facts("run-1")))
    _assert_refused(section, 3, "CI block COPY docs/setup.md ./other.md is not")
