"""The closed COPY/ADD envelope (design §4.1): conditions 1–5 and the edit."""

import pytest

from deployer.admission.model import Defect
from deployer.admission.prepare import _as_r_reads
from deployer.fix.binding import Bound, bind_instruction, link_problem, splice
from deployer.fix.envelope import Candidates, apply_source, eligible_sources
from deployer.provenance.model import TreeRow
from deployer.reproduce.dockerfile import parse
from deployer.reproduce.ignore import IgnoreRules

_NO_RULES = IgnoreRules(None, [], None)
_SHA = "0" * 40


def _file(path: str, mode: str = "100644") -> TreeRow:
    """A regular-file row of the listing."""
    return TreeRow(path=path, mode=mode, type="blob", sha=_SHA)


def _row(path: str, mode: str, kind: str) -> TreeRow:
    """Any row of the listing."""
    return TreeRow(path=path, mode=mode, type=kind, sha=_SHA)


def _bound(dockerfile: bytes, line: int = 2) -> Bound:
    """The instruction starting at ``line``, bound without A's cross-check.

    Lets the reading refusals be tested on forms ``bind_instruction``
    itself would already refuse.
    """
    parsed = parse(_as_r_reads(dockerfile))
    ordinal, instruction = next(
        (n, i) for n, i in enumerate(parsed.instructions) if i.first_line == line
    )
    lines = dockerfile.splitlines(keepends=True)
    original = b"".join(lines[line - 1 : instruction.last_line])
    return Bound(
        ordinal=ordinal,
        lines=(instruction.first_line, instruction.last_line),
        original=original,
        instruction=instruction,
    )


def _eligible(
    dockerfile: bytes,
    absent: str,
    listing: list[TreeRow],
    ci_rules: IgnoreRules = _NO_RULES,
    local_rules: IgnoreRules = _NO_RULES,
) -> Candidates | str:
    """Run the envelope on the instruction at line 2 of ``dockerfile``."""
    return eligible_sources(
        _bound(dockerfile),
        absent,
        listing,
        ci_rules,
        local_rules,
        dockerfile=dockerfile,
    )


def _paths(result: Candidates | str) -> tuple[str, ...]:
    """The eligible paths of a successful envelope."""
    assert isinstance(result, Candidates), result
    return result.eligible


_SIMPLE = b"FROM python:3.12\nCOPY app.py /app/\n"


# --- the happy path ----------------------------------------------------------


def test_eligible_through_bind_instruction() -> None:
    """Bound by Task 4's binder, the envelope lists the regular files."""
    defect = Defect(
        cls="missing_copy_source", file="Dockerfile", lines=(2, 2), object="app.py"
    )
    bound = bind_instruction(_SIMPLE, defect)
    assert isinstance(bound, Bound)
    listing = [_file("src/app.py"), _file("README.md", "100755")]
    result = eligible_sources(
        bound, "app.py", listing, _NO_RULES, _NO_RULES, dockerfile=_SIMPLE
    )
    assert isinstance(result, Candidates)
    assert result.eligible == ("README.md", "src/app.py")
    names = [entry["condition"] for entry in result.conditions]
    assert names == [
        "1 read alike, one token changes",
        "2 regular file",
        "3 effective build context",
        "4 collisions",
        "5 basename floor",
    ]
    assert all(entry["ok"] is True and entry["detail"] for entry in result.conditions)
    assert result.position == 0


# --- position: the absent token's index among the instruction's sources ------


def test_position_is_the_absent_source_index() -> None:
    """``position`` is the absent source's index in ``_sources`` order.

    Task 8's ``regressions``/``defect_check_passes`` key a fresh
    ``copy_sources`` re-check by this index, not by subject text, so it
    must name the absent source's own slot among the instruction's
    sources — not always 0, and not affected by which source is absent.
    """
    first = _eligible(_MULTI, "app.py", [_file("lib/util.py"), _file("src/app.py")])
    assert isinstance(first, Candidates)
    assert first.position == 0

    second = eligible_sources(
        _bound(_MULTI),
        "lib/util.py",
        [_file("app.py"), _file("tools/util.py")],
        _NO_RULES,
        _NO_RULES,
        dockerfile=_MULTI,
    )
    assert isinstance(second, Candidates)
    assert second.position == 1


# --- condition 2: regular files only ----------------------------------------


def test_condition_2_only_regular_files() -> None:
    """Directories, symlinks, submodules and anything under a link drop out."""
    listing = [
        _file("src/app.py"),
        _row("src", "040000", "tree"),
        _row("link.py", "120000", "blob"),
        _row("vendor", "160000", "commit"),
        _file("vendor/app2.py"),
        _row("linked", "120000", "blob"),
        _file("linked/deep/x.py"),
    ]
    assert _paths(_eligible(_SIMPLE, "app.py", listing)) == ("src/app.py",)


# --- condition 3: effective build context ------------------------------------


@pytest.mark.parametrize("side", ["ci", "local"])
def test_condition_3_excluded_by_either_rule_set(side: str) -> None:
    """A file excluded by the CI or the local ignore file is not eligible."""
    rules = IgnoreRules(".dockerignore", [(1, "docs", False)], None)
    listing = [_file("src/app.py"), _file("docs/app2.py")]
    ci, local = (rules, _NO_RULES) if side == "ci" else (_NO_RULES, rules)
    result = _eligible(_SIMPLE, "app.py", listing, ci, local)
    assert _paths(result) == ("src/app.py",)


@pytest.mark.parametrize("side", ["CI", "local"])
def test_condition_3_unmodelled_ignore_pattern(side: str) -> None:
    """An unsupported pattern on either side leaves exclusion unprovable."""
    rules = IgnoreRules(".dockerignore", [], "[ab]*")
    ci, local = (rules, _NO_RULES) if side == "CI" else (_NO_RULES, rules)
    result = _eligible(_SIMPLE, "app.py", [_file("src/app.py")], ci, local)
    assert isinstance(result, str)
    assert result.startswith("3 effective build context")
    assert side in result and "unmodelled pattern" in result


@pytest.mark.parametrize(
    "path",
    [
        "src/a*.py",  # glob character
        "src/a[1].py",  # glob class
        "src/a b.py",  # whitespace
        "src/$x.py",  # substitution
        "src/a\\b.py",  # escape
        "-flag.py",  # reads as a flag
        "git@host",  # reads as a remote source
    ],
)
def test_condition_3_unmodelled_form_not_eligible(path: str) -> None:
    """Only the closed literal form can be written as a replacement."""
    listing = [_file("src/app.py"), _file(path)]
    assert _paths(_eligible(_SIMPLE, "app.py", listing)) == ("src/app.py",)


def test_condition_3_unmodelled_notation_of_the_original() -> None:
    """``/app.py`` normalises to ``app.py`` but its notation is not modelled."""
    dockerfile = b"FROM python:3.12\nCOPY /app.py /app/\n"
    result = _eligible(dockerfile, "app.py", [_file("src/app.py")])
    assert isinstance(result, str)
    assert "notation" in result


@pytest.mark.parametrize(
    ("original", "written"),
    [(b"app.py", b"src/app.py"), (b"./app.py", b"./src/app.py")],
)
def test_condition_3_notation_kept(original: bytes, written: bytes) -> None:
    """A leading ``./`` is written iff the original had one."""
    dockerfile = b"FROM python:3.12\nCOPY " + original + b" /app/\n"
    bound = _bound(dockerfile)
    assert _paths(_eligible(dockerfile, "app.py", [_file("src/app.py")])) == (
        "src/app.py",
    )
    assert apply_source(bound, "app.py", "src/app.py") == (
        b"COPY " + written + b" /app/\n"
    )


# --- condition 4: collisions -------------------------------------------------


_MULTI = b"FROM python:3.12\nCOPY app.py lib/util.py /app/\n"


def test_collision_a_duplicate_source() -> None:
    """(a) the other source itself is never a candidate."""
    listing = [_file("lib/util.py"), _file("src/app.py")]
    assert _paths(_eligible(_MULTI, "app.py", listing)) == ("src/app.py",)


def test_collision_b_destination_name_conflict() -> None:
    """(b) with two sources, a candidate named like the other one drops out."""
    listing = [_file("lib/util.py"), _file("tools/util.py"), _file("src/app.py")]
    assert _paths(_eligible(_MULTI, "app.py", listing)) == ("src/app.py",)


def test_collision_b_needs_two_sources() -> None:
    """With one source the destination is not a directory: no (b) conflict."""
    listing = [_file("tools/util.py"), _file("src/app.py")]
    assert _paths(_eligible(_SIMPLE, "app.py", listing)) == (
        "src/app.py",
        "tools/util.py",
    )


@pytest.mark.parametrize(
    ("dockerfile", "listing"),
    [
        (
            b"FROM python:3.12\nCOPY app.py lib /app/\n",
            [_row("lib", "040000", "tree"), _file("lib/x.py"), _file("src/app.py")],
        ),
        (b"FROM python:3.12\nCOPY app.py *.txt /app/\n", [_file("src/app.py")]),
        (b"FROM python:3.12\nCOPY app.py gone.txt /app/\n", [_file("src/app.py")]),
    ],
    ids=["directory-sibling", "glob-sibling", "absent-sibling"],
)
def test_condition_4_multi_source_needs_regular_siblings(
    dockerfile: bytes, listing: list[TreeRow]
) -> None:
    """Any other source that is not a regular file of the listing → stop."""
    result = _eligible(dockerfile, "app.py", listing)
    assert isinstance(result, str)
    assert result.startswith("4 collisions")


# --- condition 5 and zero candidates -----------------------------------------


def test_basename_floor() -> None:
    """Two eligible blobs named like the absent source stop before any model."""
    listing = [_file("a/app.py"), _file("b/app.py"), _file("README.md")]
    result = _eligible(_SIMPLE, "app.py", listing)
    assert isinstance(result, str)
    assert result.startswith("5 basename floor")
    assert "a/app.py" in result and "b/app.py" in result


def test_basename_floor_counts_only_eligible_blobs() -> None:
    """An excluded namesake does not count toward the floor."""
    rules = IgnoreRules(".dockerignore", [(1, "b", False)], None)
    listing = [_file("a/app.py"), _file("b/app.py")]
    assert _paths(_eligible(_SIMPLE, "app.py", listing, rules)) == ("a/app.py",)


def test_zero_candidates() -> None:
    """No eligible file at all → stop."""
    rules = IgnoreRules(".dockerignore", [(1, "src", False)], None)
    result = _eligible(_SIMPLE, "app.py", [_file("src/app.py")], rules)
    assert isinstance(result, str)
    assert result.startswith("candidates")


# --- condition 1: R and the builders read the instruction alike --------------


@pytest.mark.parametrize(
    ("dockerfile", "fragment"),
    [
        (b"FROM x:1\nCOPY app.py\\\n /app/\n", "continuation"),
        (b"FROM x:1\nCOPY app.py \\\x0c\n /app/\n", "other than space or tab"),
        (b"FROM x:1\nCOPY\tapp.py /app/\n", "not separated by a space"),
        (b'FROM x:1\nCOPY ["app.py", "/app/"]\n', "JSON"),
        (b'FROM x:1\nCOPY --chown="app" app.py /app/\n', "quote"),
        (b"FROM x:1\nCOPY app.py \\\n# note\n /app/\n", "comment"),
        (b"FROM x:1\nCOPY <<EOF /app/x\nhi\nEOF\n", "heredoc"),
        (b"# escape=`\nFROM x:1\nCOPY app.py /app/\n", "escape"),
        (b"FROM x:1\nCOPY app.py /app\\ dir/\n", "backslash"),
        (b"FROM x:1\nCOPY app.py --chown=x /app/\n", "reads as a flag"),
        (b"FROM x:1\nCOPY --from=build app.py /app/\n", "--from"),
        (b"FROM x:1\nRUN app.py /app/\n", "not COPY"),
    ],
    ids=[
        "glued-continuation",
        "formfeed-continuation",
        "tab-after-keyword",
        "json-form",
        "quote",
        "comment-in-continuation",
        "heredoc",
        "escape-directive",
        "backslash-escape",
        "late-flag",
        "from-flag",
        "not-copy",
    ],
)
def test_reading_divergence_refused(dockerfile: bytes, fragment: str) -> None:
    """Each form R and the builders may read apart stops the envelope."""
    line = 3 if dockerfile.startswith(b"# escape") else 2
    bound = _bound(dockerfile, line)
    result = eligible_sources(
        bound,
        "app.py",
        [_file("src/app.py")],
        _NO_RULES,
        _NO_RULES,
        dockerfile=dockerfile,
    )
    assert isinstance(result, str)
    assert result.startswith("1 read alike")
    assert fragment in result


def test_bound_from_another_dockerfile_refused() -> None:
    """A ``Bound`` not matching the given Dockerfile is never reused."""
    bound = _bound(_SIMPLE)
    other = b"FROM python:3.12\nCOPY main.py /app/\n"
    result = eligible_sources(
        bound, "app.py", [_file("src/app.py")], _NO_RULES, _NO_RULES, dockerfile=other
    )
    assert isinstance(result, str)
    assert "does not match" in result


def test_absent_written_twice_refused() -> None:
    """Two sources normalising to the absent one: which to change is unclear."""
    dockerfile = b"FROM x:1\nCOPY app.py ./app.py /app/\n"
    result = _eligible(dockerfile, "app.py", [_file("src/app.py")])
    assert isinstance(result, str)
    assert "2 sources normalise" in result


# --- apply_source --------------------------------------------------------------


def test_apply_keeps_everything_else_byte_for_byte() -> None:
    """Flags, spacing, continuations, other sources and CRLF stay as written."""
    dockerfile = (
        b"FROM python:3.12\r\n"
        b"COPY --chown=app:app  --chmod=755 \\\r\n"
        b"    lib/util.py\tapp.py \\\r\n"
        b"  /app/\r\n"
        b"RUN true\r\n"
    )
    bound = _bound(dockerfile)
    corrected = apply_source(bound, "app.py", "src/app.py")
    assert corrected == (
        b"COPY --chown=app:app  --chmod=755 \\\r\n"
        b"    lib/util.py\tsrc/app.py \\\r\n"
        b"  /app/\r\n"
    )
    assert isinstance(corrected, bytes)
    spliced = splice(dockerfile, bound, corrected)
    assert link_problem(dockerfile, spliced, bound) is None


def test_apply_token_inside_a_longer_word_is_not_matched() -> None:
    """``app.py`` inside ``/opt/app.py`` is not the source token."""
    bound = _bound(b"FROM x:1\nCOPY app.py /opt/app.py\n")
    assert apply_source(bound, "app.py", "src/app.py") == (
        b"COPY src/app.py /opt/app.py\n"
    )


def test_apply_ambiguous_token_refused() -> None:
    """The destination equal to the source makes the token ambiguous."""
    bound = _bound(b"FROM x:1\nCOPY app.py app.py\n")
    result = apply_source(bound, "app.py", "src/app.py")
    assert isinstance(result, str)
    assert "2 times" in result


@pytest.mark.parametrize(
    "new", ["./src/app.py", "src/a b.py", "src/*.py", "/src/app.py", "-x", "."]
)
def test_apply_unmodelled_replacement_refused(new: str) -> None:
    """The replacement must be a normalised literal in the closed alphabet."""
    bound = _bound(_SIMPLE)
    result = apply_source(bound, "app.py", new)
    assert isinstance(result, str)
    assert result.startswith("replacement")


def test_apply_refuses_divergent_reading() -> None:
    """``apply_source`` re-checks the bound instruction's own reading."""
    bound = _bound(b"FROM x:1\nCOPY app.py\\\n /app/\n")
    result = apply_source(bound, "app.py", "src/app.py")
    assert isinstance(result, str)
    assert "continuation" in result


def test_add_refused_in_this_slice() -> None:
    """ADD extracts archives by content, which no reading here establishes."""
    dockerfile = b"FROM x:1\nADD --chmod=644 app.py /app/\n"
    bound = _bound(dockerfile)
    reason = (
        "ADD replacement not supported in this slice: archive extraction "
        "depends on content"
    )
    result = _eligible(dockerfile, "app.py", [_file("src/app.py")])
    assert result == f"1 read alike, one token changes: {reason}"
    assert apply_source(bound, "app.py", "src/app.py") == reason


# --- fix round 1 regressions --------------------------------------------------


@pytest.mark.parametrize(
    "dockerfile",
    [
        b"FROM x:1\nCOPY app.py app.py\n",
        b"FROM x:1\nCOPY app.py\x1cother.py /a/\n",
        b"FROM x:1\nCOPY app.py\x0cother.py /a/\n",
    ],
    ids=["token-twice", "x1c-separator", "x0c-separator"],
)
def test_token_not_located_once_refused_before_any_model(dockerfile: bytes) -> None:
    """The whole-token byte match runs in the envelope, not only in apply."""
    listing = [_file("src/app.py"), _file("other.py")]
    result = _eligible(dockerfile, "app.py", listing)
    assert isinstance(result, str)
    assert result.startswith("1 read alike")
    assert "as a whole token" in result


def test_deployer_dir_and_artifact_never_candidates() -> None:
    """``.deployer/`` and the Dockerfile itself drop out whatever the rules."""
    listing = [
        _file("src/app.py"),
        _file(".deployer/state/app2.py"),
        _file("Dockerfile"),
        _file("docker/Dockerfile.prod"),
    ]
    assert _paths(_eligible(_SIMPLE, "app.py", listing)) == (
        "docker/Dockerfile.prod",
        "src/app.py",
    )
    result = eligible_sources(
        _bound(_SIMPLE),
        "app.py",
        listing,
        _NO_RULES,
        _NO_RULES,
        dockerfile=_SIMPLE,
        artifact_path="docker/Dockerfile.prod",
    )
    assert _paths(result) == ("Dockerfile", "src/app.py")


def test_only_the_artifact_path_is_excluded_not_the_name() -> None:
    """§4.1: exactly ``binding.artifact_path`` drops out; other files named
    ``Dockerfile`` and a ``.dockerignore`` stay candidates."""
    listing = [
        _file(".dockerignore"),
        _file("Dockerfile"),
        _file("docker/Dockerfile"),
        _file("src/app.py"),
    ]
    result = eligible_sources(
        _bound(_SIMPLE),
        "app.py",
        listing,
        _NO_RULES,
        _NO_RULES,
        dockerfile=_SIMPLE,
        artifact_path="docker/Dockerfile",
    )
    assert _paths(result) == (".dockerignore", "Dockerfile", "src/app.py")


def test_multi_source_destination_must_be_a_directory() -> None:
    """Several sources into a destination without ``/`` → stop."""
    dockerfile = b"FROM x:1\nCOPY app.py lib/util.py /app\n"
    listing = [_file("lib/util.py"), _file("src/app.py")]
    result = _eligible(dockerfile, "app.py", listing)
    assert isinstance(result, str)
    assert result.startswith("4 collisions")
    assert "not ending in '/'" in result


def test_single_source_destination_may_be_a_file() -> None:
    """One source may be copied to a file name."""
    dockerfile = b"FROM x:1\nCOPY app.py /app/main.py\n"
    assert _paths(_eligible(dockerfile, "app.py", [_file("src/app.py")])) == (
        "src/app.py",
    )
