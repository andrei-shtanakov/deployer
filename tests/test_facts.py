from pathlib import Path

import pytest

from deployer.facts import (
    TargetConfigError,
    analyze_project,
    validate_target_against_facts,
)
from deployer.models import DeployTarget, ProjectFacts


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


def test_poetry_lock_sets_poetry_manager(tmp_path: Path) -> None:
    (tmp_path / "poetry.lock").write_text("")
    facts = analyze_project(tmp_path)
    assert facts.package_manager == "poetry"
    assert facts.has_poetry_lock is True


def test_tool_poetry_without_lock_does_not_detect(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.poetry]\nname = "x"\n')
    facts = analyze_project(tmp_path)
    assert facts.package_manager is None
    assert facts.has_poetry_lock is False


def test_uv_lock_wins_over_poetry_lock(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / "poetry.lock").write_text("")
    assert analyze_project(tmp_path).package_manager == "uv"


def test_poetry_lock_wins_over_requirements(tmp_path: Path) -> None:
    (tmp_path / "poetry.lock").write_text("")
    (tmp_path / "requirements.txt").write_text("flask\n")
    facts = analyze_project(tmp_path)
    assert facts.package_manager == "poetry"
    assert facts.requirements_files == {"requirements.txt": ["flask"]}


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


def test_optional_dependencies_scanned_and_normalized(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n'
        "[project.optional-dependencies]\n"
        'My_GUI = ["gradio>=6.0"]\n'
        'inference = ["llama-cpp-python>=0.2"]\n'
    )
    facts = analyze_project(tmp_path)
    assert facts.optional_dependencies == {
        "my-gui": ["gradio>=6.0"],
        "inference": ["llama-cpp-python>=0.2"],
    }


def test_optional_dependencies_collision_yields_no_fact(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n'
        "[project.optional-dependencies]\n"
        'my_extra = ["a"]\n'
        'my-extra = ["b"]\n'
    )
    assert analyze_project(tmp_path).optional_dependencies == {}


def test_root_modules_respect_file_denylist(tmp_path: Path) -> None:
    for name in ("app.py", "main.py", "setup.py", "conftest.py"):
        (tmp_path / name).write_text("x = 1\n")
    assert analyze_project(tmp_path).root_modules == ["app.py", "main.py"]


def test_package_dirs_root_src_and_denylist(tmp_path: Path) -> None:
    for pkg in ("agents", "tests", ".hidden"):
        (tmp_path / pkg).mkdir()
        (tmp_path / pkg / "__init__.py").write_text("")
    ns_pkg = tmp_path / "nsapp"
    ns_pkg.mkdir()
    (ns_pkg / "handlers.py").write_text("x = 1\n")  # namespace pkg, no __init__
    (tmp_path / "data").mkdir()  # denylisted anyway
    (tmp_path / "assets").mkdir()  # no .py files -> not a source unit
    (tmp_path / "assets" / "logo.txt").write_text("")
    src_pkg = tmp_path / "src" / "foo"
    src_pkg.mkdir(parents=True)
    (src_pkg / "__init__.py").write_text("")
    facts = analyze_project(tmp_path)
    assert facts.package_dirs == ["agents", "nsapp", "src/foo"]


def test_validate_extras_ok_and_noop() -> None:
    facts = ProjectFacts(optional_dependencies={"gui": ["gradio>=6.0"]})
    validate_target_against_facts(DeployTarget(extras=["GUI"]), facts)
    validate_target_against_facts(DeployTarget(), ProjectFacts())


def test_validate_unknown_extra_raises() -> None:
    facts = ProjectFacts(optional_dependencies={"gui": []})
    with pytest.raises(TargetConfigError, match="inference"):
        validate_target_against_facts(DeployTarget(extras=["inference"]), facts)


def test_validate_pip_without_build_system_rejects_extras() -> None:
    facts = ProjectFacts(
        optional_dependencies={"gui": []},
        package_manager="pip",
        has_build_system=False,
    )
    with pytest.raises(TargetConfigError, match="build-system"):
        validate_target_against_facts(DeployTarget(extras=["gui"]), facts)


def test_layout_facts_empty_without_pyproject(tmp_path: Path) -> None:
    facts = analyze_project(tmp_path)
    assert facts.optional_dependencies == {}
    assert facts.root_modules == []
    assert facts.package_dirs == []


def test_package_dir_ignores_py_named_subdirectory(tmp_path: Path) -> None:
    trap = tmp_path / "app" / "mod.py"
    trap.mkdir(parents=True)  # a DIRECTORY named mod.py
    assert analyze_project(tmp_path).package_dirs == []


def test_src_not_listed_as_both_unit_and_parent(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "util.py").write_text("x = 1\n")
    pkg = src / "foo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    assert analyze_project(tmp_path).package_dirs == ["src/foo"]


def test_validate_entrypoint_root_module_ok() -> None:
    facts = ProjectFacts(root_modules=["app.py", "main.py"])
    validate_target_against_facts(DeployTarget(entrypoint="app.py"), facts)


def test_validate_entrypoint_scripts_name_ok() -> None:
    facts = ProjectFacts(entrypoints={"serve": "pkg.app:main"}, has_build_system=True)
    validate_target_against_facts(DeployTarget(entrypoint="serve"), facts)


def test_validate_scripts_entrypoint_needs_build_system() -> None:
    """A console script cannot exist in a --no-install-project image."""
    facts = ProjectFacts(entrypoints={"serve": "pkg.app:main"})
    with pytest.raises(TargetConfigError, match="build-system"):
        validate_target_against_facts(DeployTarget(entrypoint="serve"), facts)


def test_validate_filename_entrypoint_ok_without_build_system() -> None:
    """A root-module filename needs no installation; both-sources names
    resolve as the filename and stay valid."""
    facts = ProjectFacts(
        entrypoints={"app.py": "pkg.app:main"}, root_modules=["app.py"]
    )
    validate_target_against_facts(DeployTarget(entrypoint="app.py"), facts)


def test_validate_entrypoint_unknown_raises() -> None:
    facts = ProjectFacts(root_modules=["main.py"])
    with pytest.raises(TargetConfigError, match="app.py"):
        validate_target_against_facts(DeployTarget(entrypoint="app.py"), facts)


def test_validate_entrypoint_rejects_paths() -> None:
    facts = ProjectFacts(root_modules=["app.py"])
    for bad in ("src/app.py", "./app.py", "pkg\\mod.py"):
        with pytest.raises(TargetConfigError, match="bare name"):
            validate_target_against_facts(DeployTarget(entrypoint=bad), facts)


def test_validate_entrypoint_unset_is_noop() -> None:
    validate_target_against_facts(DeployTarget(), ProjectFacts())


_LEGACY_PYPROJECT = """\
[tool.poetry]
name = "legacy-app"
version = "0.1.0"

[tool.poetry.dependencies]
python = ">=3.12"
flask = "^3.0"
psycopg2 = { version = "^2.9", optional = true }

[tool.poetry.extras]
db = ["psycopg2"]

[tool.poetry.scripts]
legacy-app = "legacy_app.main:run"

[build-system]
requires = ["poetry-core"]
build-backend = "poetry.core.masonry.api"
"""


def test_legacy_poetry_metadata_fallback(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_LEGACY_PYPROJECT)
    (tmp_path / "poetry.lock").write_text("")
    facts = analyze_project(tmp_path)
    assert facts.name == "legacy-app"
    assert facts.dependencies == ["flask"]  # python and optional excluded
    assert facts.entrypoints == {"legacy-app": "legacy_app.main:run"}
    assert facts.optional_dependencies == {"db": ["psycopg2"]}
    assert facts.package_manager == "poetry"


def test_legacy_fallback_does_not_detect_poetry(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_LEGACY_PYPROJECT)
    facts = analyze_project(tmp_path)
    assert facts.name == "legacy-app"  # metadata visible
    assert facts.package_manager is None  # but no lock -> no strategy


def test_project_table_wins_over_legacy(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "pep621"\ndependencies = ["requests"]\n\n'
        '[tool.poetry]\nname = "legacy"\n\n'
        '[tool.poetry.dependencies]\nflask = "^3.0"\n'
    )
    facts = analyze_project(tmp_path)
    assert facts.name == "pep621"
    assert facts.dependencies == ["requests"]


def test_invalid_project_key_is_not_replaced_by_legacy(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = 42\n\n[tool.poetry]\nname = "legacy"\n'
    )
    assert analyze_project(tmp_path).name is None


def test_pep621_extras_collision_not_papered_over(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n'
        "[project.optional-dependencies]\n"
        'my_extra = []\n"my-extra" = []\n\n'
        '[tool.poetry.extras]\ndb = ["psycopg2"]\n'
    )
    assert analyze_project(tmp_path).optional_dependencies == {}


def test_legacy_extras_normalize_and_collide(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.extras]\nmy_extra = ["a"]\n"my-extra" = ["b"]\n'
    )
    assert analyze_project(tmp_path).optional_dependencies == {}


def test_validate_entrypoint_against_legacy_scripts(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_LEGACY_PYPROJECT)
    (tmp_path / "poetry.lock").write_text("")
    facts = analyze_project(tmp_path)
    validate_target_against_facts(DeployTarget(entrypoint="legacy-app"), facts)


def test_validate_extras_against_legacy_extras(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_LEGACY_PYPROJECT)
    (tmp_path / "poetry.lock").write_text("")
    facts = analyze_project(tmp_path)
    validate_target_against_facts(DeployTarget(extras=["db"]), facts)
    with pytest.raises(TargetConfigError):
        validate_target_against_facts(DeployTarget(extras=["gui"]), facts)
