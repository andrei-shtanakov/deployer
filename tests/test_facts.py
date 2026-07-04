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


def test_malformed_pyproject_degrades_to_empty(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("this is not [valid toml")
    facts = analyze_project(tmp_path)
    assert facts.name is None
    assert facts.dependencies == []


def test_wrong_typed_values_are_not_invented(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = 42\ndependencies = "not-a-list"\nscripts = "nope"\n'
    )
    facts = analyze_project(tmp_path)
    assert facts.name is None
    assert facts.dependencies == []
    assert facts.entrypoints == {}


def test_pip_service_facts(pip_service: Path) -> None:
    facts = analyze_project(pip_service)
    assert facts.package_manager == "pip"
    assert facts.has_build_system is False
    assert facts.requirements_files == {"requirements.txt": []}


def test_uv_lock_wins_over_requirements(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / "requirements.txt").write_text("requests\n")
    facts = analyze_project(tmp_path)
    assert facts.package_manager == "uv"
    assert facts.requirements_files == {"requirements.txt": ["requests"]}


def test_no_manager_when_nothing_present(tmp_path: Path) -> None:
    assert analyze_project(tmp_path).package_manager is None


def test_requirements_parsing_normalizes(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "Flask==3.0.0\n"
        "psycopg2>=2.9  # db driver\n"
        "Python_LDAP~=3.4\n"
        "uvicorn[standard]<1.0 ; python_version >= '3.10'\n"
        "-r extra.txt\n"
        "--index-url https://example.com/simple\n"
        "\n"
        "# comment only\n"
    )
    facts = analyze_project(tmp_path)
    assert facts.requirements_files["requirements.txt"] == [
        "flask",
        "psycopg2",
        "python-ldap",
        "uvicorn",
        "-r extra.txt",
        "--index-url https://example.com/simple",
    ]


def test_multiple_requirements_files(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests\n")
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")
    facts = analyze_project(tmp_path)
    assert set(facts.requirements_files) == {
        "requirements.txt",
        "requirements-dev.txt",
    }


def test_has_build_system_detected(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[build-system]\nrequires = ["hatchling"]\n'
    )
    assert analyze_project(tmp_path).has_build_system is True


def test_unreadable_requirements_degrades_to_empty(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_bytes(b"\xff\xfe\x00bad")
    facts = analyze_project(tmp_path)
    assert facts.requirements_files == {"requirements.txt": []}


def test_bom_prefixed_requirements_parse_clean(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_bytes(b"\xef\xbb\xbfflask==3.0\n")
    facts = analyze_project(tmp_path)
    assert facts.requirements_files["requirements.txt"] == ["flask"]
