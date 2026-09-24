"""Record/snapshot models: strict, canonical, hashable."""

from typing import Any

import pytest
from pydantic import ValidationError

from deployer.models import ProjectFacts
from deployer.provenance.model import (
    Record,
    Snapshot,
    TreeRow,
    canonical_bytes,
    set_dir_name,
    sha256_hex,
)


def _snapshot(**kw: Any) -> Snapshot:
    base: dict[str, Any] = dict(
        format_version="1",
        source_commit="a" * 40,
        tree=[TreeRow(path="Dockerfile", mode="100644", type="blob", sha="b" * 40)],
        tree_complete=True,
        facts=ProjectFacts(name="p"),
    )
    return Snapshot(**{**base, **kw})


def test_canonical_bytes_are_stable_and_sorted():
    snap = _snapshot()
    assert canonical_bytes(snap) == canonical_bytes(_snapshot())
    assert canonical_bytes(snap).endswith(b"\n")
    assert b'"format_version":"1"' in canonical_bytes(snap)


def test_unknown_format_version_is_rejected():
    with pytest.raises(ValidationError):
        _snapshot(format_version="2")


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        Record.model_validate(
            {
                "format_version": "1",
                "repo": "o/r",
                "artifact_path": "Dockerfile",
                "artifact_sha256": "0" * 64,
                "source_commit": "a" * 40,
                "snapshot_sha256": "1" * 64,
                "deployer_version": "0.1",
                "extra": 1,
            }
        )


def test_set_dir_is_named_by_the_record_hash():
    assert set_dir_name("c" * 64) == "Dockerfile/" + "c" * 64
    assert sha256_hex(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
