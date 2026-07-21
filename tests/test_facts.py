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


GUARD = 'if __name__ == "__main__":\n    main()\n'


def _py(tmp_path: Path, name: str, body: str = "") -> None:
    (tmp_path / name).write_text(f"def main() -> None:\n    pass\n\n{body}")


def test_script_entrypoint_main_py_with_guard(tmp_path: Path) -> None:
    _py(tmp_path, "main.py", GUARD)
    assert analyze_project(tmp_path).script_entrypoint == "main.py"


def test_script_entrypoint_single_other_guarded_file(tmp_path: Path) -> None:
    _py(tmp_path, "worker.py", GUARD)
    assert analyze_project(tmp_path).script_entrypoint == "worker.py"


def test_script_entrypoint_main_py_wins_over_other_candidates(
    tmp_path: Path,
) -> None:
    _py(tmp_path, "main.py", GUARD)
    _py(tmp_path, "worker.py", GUARD)
    assert analyze_project(tmp_path).script_entrypoint == "main.py"


def test_script_entrypoint_ambiguous_is_none(tmp_path: Path) -> None:
    _py(tmp_path, "alpha.py", GUARD)
    _py(tmp_path, "beta.py", GUARD)
    assert analyze_project(tmp_path).script_entrypoint is None


def test_script_entrypoint_no_guard_is_none(tmp_path: Path) -> None:
    _py(tmp_path, "app.py")  # no guard: filename convention must NOT win
    assert analyze_project(tmp_path).script_entrypoint is None


def test_script_entrypoint_ignores_nested_files(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(GUARD)
    assert analyze_project(tmp_path).script_entrypoint is None


def test_script_entrypoint_single_quotes_and_spacing(tmp_path: Path) -> None:
    _py(tmp_path, "main.py", "if __name__=='__main__' :\n    main()\n")
    assert analyze_project(tmp_path).script_entrypoint == "main.py"


def test_script_entrypoint_denylisted_setup_py_alone_is_none(
    tmp_path: Path,
) -> None:
    _py(tmp_path, "setup.py", GUARD)
    assert analyze_project(tmp_path).script_entrypoint is None


def test_script_entrypoint_denylist_does_not_create_ambiguity(
    tmp_path: Path,
) -> None:
    _py(tmp_path, "setup.py", GUARD)
    _py(tmp_path, "worker.py", GUARD)
    assert analyze_project(tmp_path).script_entrypoint == "worker.py"


def test_script_entrypoint_denylisted_manage_py_alone_is_none(
    tmp_path: Path,
) -> None:
    _py(tmp_path, "manage.py", GUARD)
    assert analyze_project(tmp_path).script_entrypoint is None


def test_unreadable_requirements_degrades_to_empty(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_bytes(b"\xff\xfe\x00bad")
    facts = analyze_project(tmp_path)
    assert facts.requirements_files == {"requirements.txt": []}


def test_bom_prefixed_requirements_parse_clean(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_bytes(b"\xef\xbb\xbfflask==3.0\n")
    facts = analyze_project(tmp_path)
    assert facts.requirements_files["requirements.txt"] == ["flask"]


def test_vcs_and_url_requirements_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "git+https://github.com/x/y.git\nhttps://example.com/pkg.whl\nflask\n"
    )
    facts = analyze_project(tmp_path)
    assert facts.requirements_files["requirements.txt"] == ["flask"]


def test_slow_build_corpus_case_has_entrypoint_fact() -> None:
    corpus_case = (
        Path(__file__).parent.parent / "corpus" / "synthetic" / "slow-build" / "project"
    )
    facts = analyze_project(corpus_case)
    assert facts.script_entrypoint == "main.py"
    assert facts.package_manager == "pip"
