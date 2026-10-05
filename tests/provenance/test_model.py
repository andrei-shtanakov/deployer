"""Record/snapshot models: strict, canonical, hashable."""

import json
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


@pytest.mark.parametrize("value", ["true", "yes", 1, 0, None])
def test_tree_complete_is_strict_on_load(value: object) -> None:
    """TODO admission-strict-tree-complete: only a JSON boolean is accepted."""
    document = json.loads(_snapshot().model_dump_json())
    document["tree_complete"] = value
    with pytest.raises(ValidationError):
        Snapshot.model_validate_json(json.dumps(document))


@pytest.mark.parametrize("value", [True, False])
def test_a_json_boolean_tree_complete_loads_as_before(value: bool) -> None:
    document = json.loads(_snapshot().model_dump_json())
    document["tree_complete"] = value
    assert Snapshot.model_validate_json(json.dumps(document)).tree_complete is value


def test_only_tree_complete_is_strict() -> None:
    """The strictness is scoped to this one field; the snapshot model is not
    switched to strict mode as a whole."""
    from pydantic import Strict

    assert Snapshot.model_config.get("strict") is None
    strict = {
        name
        for name, field in Snapshot.model_fields.items()
        if any(isinstance(m, Strict) for m in field.metadata)
    }
    assert strict == {"tree_complete"}
