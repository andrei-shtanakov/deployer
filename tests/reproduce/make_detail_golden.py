"""Record R's aggregate check outputs for the parity test.

The committed entries were recorded on the code before the detailed refactor
(``ceb1a55``) and pin its behaviour. This script therefore only **adds** the
entries of new trees or cases; it refuses to write if any existing entry
would change, since that would re-bless a behaviour change instead of
failing ``test_outputs_unchanged``. Run: ``uv run python
tests/reproduce/make_detail_golden.py``.
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
    out = json.loads(json.dumps(out))  # tuples as lists, the committed form
    existing = json.loads(GOLDEN.read_text()) if GOLDEN.is_file() else {}
    changed = sorted(k for k in existing if k in out and out[k] != existing[k])
    if changed:
        sys.exit(f"refusing to re-bless changed entries: {', '.join(changed)}")
    added = sorted(set(out) - set(existing))
    merged = {**existing, **{k: out[k] for k in added}}
    GOLDEN.write_text(json.dumps(merged, indent=1, sort_keys=True) + "\n")
    print(f"added {len(added)} entries to {GOLDEN}: {', '.join(added) or 'none'}")


if __name__ == "__main__":
    main()
