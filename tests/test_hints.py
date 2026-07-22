from deployer.hints import KNOWN_SYSTEM_DEPS, collect_hints
from deployer.models import ProjectFacts


def test_psycopg2_matched_from_pyproject_deps() -> None:
    facts = ProjectFacts(dependencies=["psycopg2>=2.9", "pydantic>=2.7"])
    hints = collect_hints(facts)
    assert [h.python_package for h in hints] == ["psycopg2"]
    assert "libpq-dev" in hints[0].build_packages
    assert "libc6-dev" in hints[0].build_packages
    assert "libpq5" in hints[0].runtime_packages


def test_psycopg2_binary_is_explicit_no_hint() -> None:
    assert "psycopg2-binary" in KNOWN_SYSTEM_DEPS  # encoded knowledge
    facts = ProjectFacts(dependencies=["psycopg2-binary==2.9.10"])
    assert collect_hints(facts) == []  # but never emitted as a hint


def test_matches_requirements_files_and_skips_directives() -> None:
    facts = ProjectFacts(
        requirements_files={
            "requirements.txt": ["uwsgi", "-r extra.txt", "flask"],
        }
    )
    hints = collect_hints(facts)
    assert [h.python_package for h in hints] == ["uwsgi"]


def test_normalization_and_dedup() -> None:
    facts = ProjectFacts(
        dependencies=["M2Crypto"],
        requirements_files={"requirements.txt": ["m2crypto"]},
    )
    hints = collect_hints(facts)
    assert [h.python_package for h in hints] == ["m2crypto"]


def test_sorted_output() -> None:
    facts = ProjectFacts(dependencies=["uwsgi", "psycopg2", "pygraphviz"])
    names = [h.python_package for h in collect_hints(facts)]
    assert names == sorted(names)


def test_wheel_covered_packages_absent_from_table() -> None:
    for name in ("lxml", "pillow", "cryptography", "numpy", "cffi"):
        assert name not in KNOWN_SYSTEM_DEPS


def test_every_gcc_entry_also_carries_libc6_dev() -> None:
    for hint in KNOWN_SYSTEM_DEPS.values():
        if "gcc" in hint.build_packages:
            assert "libc6-dev" in hint.build_packages, hint.python_package


def test_collect_hints_returns_copies() -> None:
    facts = ProjectFacts(dependencies=["psycopg2"])
    hints = collect_hints(facts)
    hints[0].build_packages.append("EVIL")
    assert "EVIL" not in KNOWN_SYSTEM_DEPS["psycopg2"].build_packages


def test_requested_extra_deps_fire_hints() -> None:
    facts = ProjectFacts(
        optional_dependencies={
            "inference": ["llama-cpp-python>=0.2.0"],
            "gui": ["gradio>=6.0"],
        }
    )
    names = [h.python_package for h in collect_hints(facts, ["inference"])]
    assert names == ["llama-cpp-python"]


def test_unrequested_extras_stay_silent() -> None:
    facts = ProjectFacts(
        optional_dependencies={"inference": ["llama-cpp-python>=0.2.0"]}
    )
    assert collect_hints(facts) == []
    assert collect_hints(facts, ["gui"]) == []
