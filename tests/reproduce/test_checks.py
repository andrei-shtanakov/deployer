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


def test_all_sources_skipped_gives_no_passed(tmp_path):
    """Nothing checked is not a pass (third review of #77)."""
    checks = copy_source_checks(
        parse("FROM a\nCOPY file[0-9].txt /dst/\n"),
        tmp_path,
        "Dockerfile",
        load_rules(tmp_path, None),
    )
    assert [c.status for c in checks] == ["skipped"]


@pytest.mark.parametrize(
    ("dockerfile", "expected"),
    [
        ("FROM a\nCOPY . /app\n", [".git exclusion not proven: COPY . at line 2"]),
        (
            "FROM a\nCOPY .git/HEAD /h\n",
            [".git exclusion not proven: COPY .git/HEAD at line 2"],
        ),
        (
            "FROM a\nCOPY .git /saved\n",
            [".git exclusion not proven: COPY .git at line 2"],
        ),
        (
            "FROM a\nCOPY */HEAD /h\n",
            [".git exclusion not proven: COPY */HEAD at line 2"],
        ),
        (
            "FROM a\nCOPY **/config /c\n",
            [".git exclusion not proven: COPY **/config at line 2"],
        ),
        (
            "FROM a\nCOPY .g?t/HEAD /h\n",
            [".git exclusion not proven: COPY .g?t/HEAD at line 2"],
        ),
        ("FROM a\nCOPY * /x/\n", [".git exclusion not proven: COPY * at line 2"]),
        (
            "FROM a\nCOPY --parents . /app\n",
            [
                ".git exclusion not proven: COPY at line 2 "
                "(flag not modelled: --parents)"
            ],
        ),
        # cannot reach .git: a named path, a bracket glob with a literal prefix
        ("FROM a\nCOPY src ./src\n", []),
        ("FROM a\nCOPY file[0-9].txt /dst/\n", []),
        # inline content reads nothing from the context
        ("FROM a\nCOPY <<EOF /x\nhello\nEOF\n", []),
        # a --from source is checked in its stage or image
        ("FROM a AS b\nFROM b\nCOPY --from=b . /x\n", []),
        ("FROM a\nRUN --mount=type=cache,target=/c x\n", ["RUN --mount at line 2"]),
    ],
)
def test_git_exclusion_is_never_proven_from_ignore_rules(dockerfile, expected):
    """§1.3 (d), narrowed (owner, 2026-09-24): any source that may reach .git
    is an approximation, whatever the ignore file says."""
    assert context_conditions(parse(dockerfile)) == expected


def test_ignore_rules_do_not_make_git_reachability_exact(tmp_path):
    """Even '.git' in .dockerignore does not prove the exclusion (narrowed rule)."""
    (tmp_path / ".dockerignore").write_text(".git\n")
    assert load_rules(tmp_path, ".dockerignore").patterns  # the rule is there
    assert context_conditions(parse("FROM a\nCOPY . /app\n")) == [
        ".git exclusion not proven: COPY . at line 2"
    ]


def test_a_quoted_heredoc_marker_in_the_destination_opens_nothing(tmp_path):
    """Only an unquoted `<<` is a heredoc (fifth review of #77)."""
    parsed = parse('FROM a\nCOPY .git "/saved<<marker"\n')
    assert context_conditions(parsed) == [
        ".git exclusion not proven: COPY .git at line 2"
    ]


def test_json_form_after_flags_checks_the_real_sources(tmp_path):
    """Flags are split off before the JSON array is read (fifth review of #77)."""
    for name in ("a", "b"):
        (tmp_path / name).write_text("x")
    checks = copy_source_checks(
        parse('FROM a\nCOPY --chown=0 ["a", "b", "/dst/"]\n'),
        tmp_path,
        "Dockerfile",
        load_rules(tmp_path, None),
    )
    assert [(c.check_id, c.status) for c in checks] == [("copy_sources", "passed")]
    missing = copy_source_checks(
        parse('FROM a\nCOPY --chown=0 ["a", "c", "/dst/"]\n'),
        tmp_path,
        "Dockerfile",
        load_rules(tmp_path, None),
    )
    assert [c.finding for c in missing if c.status == "failed"] == [
        "source c absent from the context"
    ]


# The closed source alphabet (owner, 2026-09-24): a literal path or a glob of
# `*`, `?`, `**`, `[...]` over letters, digits and `. _ - / + = , @ ~`.
UNMODELLED_SOURCES = [
    ("s\\rc /dst", "s\\rc"),  # a shell-form escape is kept, not unescaped
    ('"src" /dst', '"src"'),  # quotes are not processed: the token is unread
    ("$SRC /x", "$SRC"),  # ARG substitution may name .git
    ("$SRC/../safe /x", "$SRC/../safe"),  # normalisation must not erase it
    ("${SRC}/.. /x", "${SRC}/.."),
    ("${SRC} /x", "${SRC}"),
    ('["a b", "/x"]', "a b"),  # whitespace inside a JSON source
]

# A backslash anywhere in a JSON form: the decoded text cannot be trusted
# ("s\u0072c" decodes to "src"), so the whole instruction is unread.
JSON_ESCAPES = ['["\\\\.git", "/saved"]', '["s\\u0072c", "/x"]', '["a\\u0007b", "/x"]']


@pytest.mark.parametrize("args", JSON_ESCAPES)
def test_a_json_form_with_an_escape_is_unread(tmp_path, args):
    """Escapes are resolved by the JSON decoder before any check could see them."""
    (tmp_path / "src").write_text("x")
    parsed = parse(f"FROM a\nCOPY {args}\n")
    assert context_conditions(parsed) == [
        ".git exclusion not proven: COPY at line 2 (escape in JSON form not modelled)"
    ]
    checks = copy_source_checks(
        parsed, tmp_path, "Dockerfile", load_rules(tmp_path, None)
    )
    assert [(c.status, c.reason) for c in checks] == [
        ("skipped", "COPY at line 2: escape in JSON form not modelled")
    ]


@pytest.mark.parametrize(("args", "source"), UNMODELLED_SOURCES)
def test_an_unmodelled_source_is_never_exact(args, source):
    """Outside the alphabet: (d) cannot hold (fifth review of #77, class fix)."""
    assert context_conditions(parse(f"FROM a\nCOPY {args}\n")) == [
        f".git exclusion not proven: COPY {source} at line 2 "
        "(source pattern not modelled)"
    ]


@pytest.mark.parametrize(("args", "source"), UNMODELLED_SOURCES)
def test_an_unmodelled_source_is_skipped_not_checked(tmp_path, args, source):
    """The same alphabet decides the source check: skipped, never absent."""
    checks = copy_source_checks(
        parse(f"FROM a\nCOPY {args}\n"),
        tmp_path,
        "Dockerfile",
        load_rules(tmp_path, None),
    )
    assert [(c.status, c.reason) for c in checks] == [
        ("skipped", f"COPY at line 2: source pattern not modelled: {source}")
    ]
