"""Cases for the parity of R's aggregate checks with their detailed records.

Shared by the one-off golden script (run on the code before the refactor) and
``test_detail.py``, so both evaluate exactly the same inputs.
"""

from pathlib import Path

from deployer.reproduce import checks, dockerfile, ignore

FIXTURES = Path(__file__).parent.parent / "fixtures"
REPO_ROOT = FIXTURES.parent.parent
GOLDEN = Path(__file__).parent / "detail_golden.json"
BUNDLES = sorted(
    [
        *(FIXTURES / "reproduction").glob("*/tree"),
        *(FIXTURES / "admission").glob("*/tree"),
    ]
)
SYNTHETIC = {
    "multi_source_one_missing": "FROM a:1\nCOPY present.txt missing.txt /d/\n",
    "glob_match": "FROM a:1\nCOPY *.txt /d/\n",
    "remote_add": "FROM a:1\nADD https://x/y /d\n",
    "unmodelled": "FROM a:1\nCOPY $X /d\n",
    "escape_directive": "# escape=`\nFROM a:1\nCOPY present.txt /d\n",
    "syntax_directive_bad_from": "# syntax=docker/dockerfile:1\nFROM a b\n",
    "empty": "",
    "no_copy": "FROM a:1\nRUN true\n",
    "no_copy_unsupported_ignore": "FROM a:1\nRUN true\n",
    # Beyond the brief's list: every other branch of R's two checks.
    "glob_no_match": "FROM a:1\nCOPY *.md /d/\n",
    "excluded_source": "FROM a:1\nCOPY present.txt /d/\n",
    "root_source": "FROM a:1\nCOPY . /d/\n",
    "heredoc": "FROM a:1\nCOPY <<EOF /d\nhi\nEOF\nCOPY present.txt /d/\n",
    "from_flag": "FROM a:1 AS b\nFROM c:1\nCOPY --from=b /x /y\n",
    "json_form": 'FROM a:1\nCOPY ["present.txt", "gone.txt", "/d/"]\n',
    "json_escape": 'FROM a:1\nCOPY ["pre\\\\sent.txt", "/d/"]\n',
    "json_bad": "FROM a:1\nCOPY [oops /d/\n",
    "unmodelled_flag": "FROM a:1\nCOPY --parents present.txt /d/\n",
    "no_pair": "FROM a:1\nCOPY present.txt\n",
    "bracket_source": "FROM a:1\nCOPY a[b].txt /d/\n",
    "mixed_skips_and_failures": (
        "FROM a:1\nADD https://x/y /d\nCOPY missing.txt present.txt /d/\n"
        "COPY $Y /d\nCOPY *.md /d/\n"
    ),
    "unsupported_ignore_with_copy": (
        "FROM a:1\nCOPY present.txt missing.txt /d/\nCOPY <<EOF /d\nx\nEOF\n"
    ),
    "escape_with_sources": (
        "# escape=`\nFROM a b\nCOPY present.txt $X /d/\nCOPY --from=x /a /b\nRUNN x\n"
    ),
    "first_not_from": "RUN x\nFROM a\n",
    "arg_only": "ARG V=1\n",
    "arg_then_from": "ARG V=1\nFROM a:$V\n",
    "two_bad_froms": "FROM a b\nFROM c:1\nFROM d e f g\n",
    "unknown_keyword_dangling": "FROM a\nRUNN x\nRUN y \\\n",
    "syntax_directive_all": "# syntax=x/y\nRUNN x\nFROM a b \\\n",
    "platform_from": "FROM --platform=linux/amd64 a:1 AS b\nFROM --platform=x a b\n",
}
UNSUPPORTED_IGNORE = "[ab]\n"
IGNORES = {
    "no_copy_unsupported_ignore": UNSUPPORTED_IGNORE,
    "unsupported_ignore_with_copy": UNSUPPORTED_IGNORE,
    "excluded_source": "present.txt\n",
}
NOTHING_IGNORED = "# excludes nothing\n"


def tree_cases() -> list[tuple[str, Path, Path]]:
    """``(key, context, dockerfile path)`` for every Dockerfile in every tree."""
    return [
        (path.relative_to(REPO_ROOT).as_posix(), tree, path)
        for tree in BUNDLES
        for path in sorted(tree.rglob("Dockerfile*"))
        if path.is_file() and not path.name.endswith(".current")
    ]


def make_context(root: Path, case: str) -> Path:
    """A synthetic context: ``present.txt`` and the case's ``.dockerignore``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "present.txt").write_text("x\n")
    (root / ".dockerignore").write_text(IGNORES.get(case, NOTHING_IGNORED))
    return root


def golden(tree: Path, text: str) -> tuple[list[dict], list[dict]]:
    """R's two aggregate outputs for ``text`` built from ``tree``."""
    parsed = dockerfile.parse(text)
    rules = ignore.load_rules(tree, ignore.ci_ignore_file(tree, "Dockerfile"))
    return (
        [
            c.model_dump()
            for c in checks.copy_source_checks(parsed, tree, "Dockerfile", rules)
        ],
        [c.model_dump() for c in dockerfile.syntax_checks(parsed, "Dockerfile")],
    )
