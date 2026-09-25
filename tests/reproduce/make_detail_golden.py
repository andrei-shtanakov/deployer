"""One-off: record R's aggregate check outputs before the detailed refactor.

Run on the unchanged code: ``uv run python tests/reproduce/make_detail_golden.py``.
``test_detail.test_outputs_unchanged`` then asserts the refactored functions
return exactly these lists.
"""

import json
import sys
import tempfile
from pathlib import Path

# Run as a script, the repo root is not on sys.path; ``tests`` lives there.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.reproduce.detail_cases import (  # noqa: E402
    GOLDEN,
    SYNTHETIC,
    golden,
    make_context,
    tree_cases,
)


def main() -> None:
    """Write ``detail_golden.json`` with one entry per tree Dockerfile and case."""
    out: dict[str, dict[str, list[dict]]] = {}
    for key, tree, path in tree_cases():
        copy, syntax = golden(tree, path.read_text())
        out[key] = {"copy_sources": copy, "syntax": syntax}
    with tempfile.TemporaryDirectory() as tmp:
        for case, text in SYNTHETIC.items():
            ctx = make_context(Path(tmp) / case, case)
            copy, syntax = golden(ctx, text)
            out[f"synthetic:{case}"] = {"copy_sources": copy, "syntax": syntax}
    GOLDEN.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(f"wrote {len(out)} entries to {GOLDEN}")


if __name__ == "__main__":
    main()
