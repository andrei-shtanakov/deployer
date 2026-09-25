"""Test-only seam for the closed "passed" template tables (F §10).

Production rows stay disabled until a real recording backs them (F §9). The
only way to enable a row without a recording is :func:`enable_for_test`,
which patches the module-private registry of ``deployer.fix.templates`` for
the duration of a ``with`` block. Nothing in ``src/`` can reach it: no CLI
flag, environment variable or configuration names the registry.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

from deployer.fix import templates


@contextmanager
def enable_for_test(*row_ids: str) -> Iterator[tuple[templates.Row, ...]]:
    """Inject the production rows named by ``row_ids`` (all four when none are
    named) as enabled synthetic rows, then restore the empty registry."""
    wanted = row_ids or tuple(row.id for row in templates.ROWS)
    by_id = {row.id: row for row in templates.ROWS}
    unknown = [row_id for row_id in wanted if row_id not in by_id]
    if unknown:
        raise KeyError(f"unknown template rows: {unknown}")
    rows = [by_id[row_id] for row_id in wanted]
    with patch.object(templates, "_TEST_REGISTRY", rows):
        yield tuple(rows)
