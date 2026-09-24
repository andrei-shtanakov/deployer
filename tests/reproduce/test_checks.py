"""§3.2-3.3 source checks and the exactness conditions (d) and (e)."""

import pytest

from deployer.reproduce.checks import (
    context_conditions,
    copy_source_checks,
    external_images,
    from_ref_checks,
)
from deployer.reproduce.dockerfile import parse
from deployer.reproduce.ignore import load_rules

RUN1 = (
    "FROM python:3.12-slim\n\n"
    "COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/\n\n"
    "WORKDIR /app\n\n"
    "COPY pyproject.toml uv.lock ./\n"
    "RUN uv sync --frozen --no-install-project\n\n"
    "COPY src/ci_build ./src/ci_build\n"
    "COPY docs/setup.md ./setup.md\n"
)


def _tree(tmp_path):
    for rel in ("pyproject.toml", "uv.lock", "src/ci_build/__init__.py"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    return tmp_path


def test_run1_missing_copy_source_is_the_finding(tmp_path):
    ctx = _tree(tmp_path)
    checks = copy_source_checks(parse(RUN1), ctx, "Dockerfile", load_rules(ctx, None))
    failed = [c for c in checks if c.status == "failed"]
    assert [(c.finding, c.location.lines if c.location else None) for c in failed] == [
        ("source docs/setup.md absent from the context", (11, 11))
    ]
    kinds = [e.kind for e in failed[0].evidence]
    assert kinds == ["path_absent", "ignore_file"]
    assert failed[0].evidence[0].listing == "../../source.json#tree"


def test_excluded_source_names_file_and_line(tmp_path):
    ctx = _tree(tmp_path)
    (ctx / "docs").mkdir()
    (ctx / "docs/setup.md").write_text("x")
    (ctx / ".dockerignore").write_text("docs\n")
    checks = copy_source_checks(
        parse(RUN1), ctx, "Dockerfile", load_rules(ctx, ".dockerignore")
    )
    assert [c.finding for c in checks if c.status == "failed"] == [
        "source docs/setup.md excluded by .dockerignore line 1"
    ]


def test_clean_tree_passes_and_from_sources_are_skipped(tmp_path):
    ctx = _tree(tmp_path)
    (ctx / "docs").mkdir()
    (ctx / "docs/setup.md").write_text("x")
    checks = copy_source_checks(parse(RUN1), ctx, "Dockerfile", load_rules(ctx, None))
    assert [c.status for c in checks if c.status != "skipped"] == ["passed"]
    assert any(c.status == "skipped" and "--from" in (c.reason or "") for c in checks)


def test_empty_glob_and_remote_add(tmp_path):
    ctx = _tree(tmp_path)
    text = "FROM a\nCOPY *.txt /x/\nADD https://example.com/f /f\n"
    checks = copy_source_checks(parse(text), ctx, "Dockerfile", load_rules(ctx, None))
    assert [c.finding for c in checks if c.status == "failed"] == [
        "source *.txt matches nothing in the context"
    ]
    assert any(c.status == "skipped" and "remote" in (c.reason or "") for c in checks)


def test_bracket_glob_source_is_skipped_not_absent(tmp_path):
    ctx = _tree(tmp_path)
    (ctx / "file5.txt").write_text("x")
    text = "FROM a\nCOPY file[0-9].txt /dst/\n"
    checks = copy_source_checks(parse(text), ctx, "Dockerfile", load_rules(ctx, None))
    assert [c.finding for c in checks if c.status == "failed"] == []
    assert any(
        c.status == "skipped"
        and c.reason == "COPY at line 2: source pattern not modelled: file[0-9].txt"
        for c in checks
    )


def test_bracket_source_is_not_a_root_glob_for_git_reachability(tmp_path):
    rules = load_rules(tmp_path, None)
    assert context_conditions(parse("FROM a\nCOPY file[0-9].txt /dst/\n"), rules) == []


def test_unmodelled_ignore_pattern_skips_the_check(tmp_path):
    ctx = _tree(tmp_path)
    (ctx / ".dockerignore").write_text("a[bc]\n")
    checks = copy_source_checks(
        parse(RUN1), ctx, "Dockerfile", load_rules(ctx, ".dockerignore")
    )
    assert [(c.check_id, c.status) for c in checks] == [("copy_sources", "skipped")]


def test_from_refs_are_observations():
    text = "FROM python:3.12 AS base\nFROM base\nCOPY --from=base /a /a\n"
    text += "COPY --from=ghcr.io/x/y:1 /b /b\n"
    obs = from_ref_checks(parse(text))
    assert [c.finding for c in obs] == [
        "--from=base resolved to a stage of this Dockerfile",
        "--from=ghcr.io/x/y:1 is an external image dependency",
    ]
    assert external_images(parse(text)) == ["python:3.12", "ghcr.io/x/y:1"]


def test_dotdot_source_is_clamped_to_the_context_root(tmp_path):
    """BuildKit clamps a COPY source to the context root; ``../../x`` must
    read as the context-relative ``x``, never resolved against the host
    filesystem outside the context."""
    ctx = _tree(tmp_path)
    checks = copy_source_checks(
        parse("FROM a\nCOPY ../../x /y\n"), ctx, "Dockerfile", load_rules(ctx, None)
    )
    assert [c.finding for c in checks if c.status == "failed"] == [
        "source x absent from the context"
    ]


def test_context_conditions_git_and_mount(tmp_path):
    rules = load_rules(tmp_path, None)
    assert context_conditions(parse("FROM a\nCOPY . /app\n"), rules) == [
        ".git reachable: COPY . at line 2"
    ]
    assert context_conditions(parse("FROM a\nCOPY .git/HEAD /h\n"), rules) == [
        ".git reachable: COPY .git/HEAD at line 2"
    ]
    assert context_conditions(
        parse("FROM a\nRUN --mount=type=cache,target=/c x\n"), rules
    ) == ["RUN --mount at line 2"]
    (tmp_path / ".dockerignore").write_text(".git\n")
    assert (
        context_conditions(
            parse("FROM a\nCOPY . /app\n"), load_rules(tmp_path, ".dockerignore")
        )
        == []
    )


def test_unmodelled_ignore_pattern_leaves_git_exclusion_unproven(tmp_path):
    """A pattern we cannot evaluate may re-include .git: not exact (review of #77)."""
    (tmp_path / ".dockerignore").write_text(".git\n![.]git\n")
    rules = load_rules(tmp_path, ".dockerignore")
    assert context_conditions(parse("FROM a\nCOPY . /app\n"), rules) == [
        ".git reachable: COPY . at line 2 (ignore pattern not modelled: [.]git)"
    ]


@pytest.mark.parametrize(
    ("dockerignore", "dockerfile", "expected"),
    [
        # an unmodelled COPY form cannot prove .git stays out
        (
            None,
            "FROM a\nCOPY --parents . /app\n",
            [
                ".git reachability not established: COPY at line 2 "
                "(flag not modelled: --parents)"
            ],
        ),
        # a supported negation re-includes a file under .git
        (
            ".git\n!.git/HEAD\n",
            "FROM a\nCOPY .git/HEAD /h\n",
            [".git reachable: COPY .git/HEAD at line 2"],
        ),
        (
            ".git\n!.git/config\n",
            "FROM a\nCOPY . /app\n",
            [".git reachable: COPY . at line 2 (re-included by !.git/config)"],
        ),
        # a negation that cannot reach .git keeps the exclusion proven
        (".git\n!README.md\n", "FROM a\nCOPY . /app\n", []),
        # a heredoc COPY carries its own content; it reads nothing from context
        (None, "FROM a\nCOPY <<EOF /x\nhello\nEOF\n", []),
    ],
)
def test_git_reachability_needs_proof(tmp_path, dockerignore, dockerfile, expected):
    """Exactness (d) holds only when .git exclusion is proven (review of #77)."""
    if dockerignore is not None:
        (tmp_path / ".dockerignore").write_text(dockerignore)
    rules = load_rules(tmp_path, ".dockerignore" if dockerignore else None)
    assert context_conditions(parse(dockerfile), rules) == expected
