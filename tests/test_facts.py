from pathlib import Path

from deployer.facts import analyze_project


def test_analyze_hello_service(hello_service: Path) -> None:
    facts = analyze_project(hello_service)
    assert facts.name == "hello-service"
    assert facts.requires_python == ">=3.12"
    assert facts.python_version == "3.12"
    assert facts.dependencies == []
    assert facts.entrypoints == {"hello-service": "main:main"}
    assert facts.has_uv_lock is False


def test_analyze_empty_dir_yields_explicit_nones(tmp_path: Path) -> None:
    facts = analyze_project(tmp_path)
    assert facts.name is None
    assert facts.requires_python is None
    assert facts.python_version is None
    assert facts.dependencies == []
    assert facts.entrypoints == {}
    assert facts.has_uv_lock is False
