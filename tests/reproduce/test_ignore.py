"""§3.2 ignore-file selection per side and Docker's matching subset."""

from deployer.reproduce.ignore import (
    ci_ignore_file,
    excluded_by,
    load_rules,
    local_ignore_file,
)


def test_ci_side_prefers_the_dockerfile_specific_file(tmp_path):
    (tmp_path / ".dockerignore").write_text("a\n")
    assert ci_ignore_file(tmp_path, "Dockerfile") == ".dockerignore"
    (tmp_path / "Dockerfile.dockerignore").write_text("b\n")
    assert ci_ignore_file(tmp_path, "Dockerfile") == "Dockerfile.dockerignore"


def test_podman_prefers_containerignore(tmp_path):
    (tmp_path / ".dockerignore").write_text("a\n")
    (tmp_path / ".containerignore").write_text("")
    assert local_ignore_file(tmp_path, "Dockerfile", "podman") == ".containerignore"
    assert local_ignore_file(tmp_path, "Dockerfile", "docker") == ".dockerignore"


def test_no_ignore_file(tmp_path):
    assert ci_ignore_file(tmp_path, "Dockerfile") is None
    assert load_rules(tmp_path, None).patterns == []


def test_matching_last_match_wins_and_parents_exclude_children(tmp_path):
    (tmp_path / ".dockerignore").write_text("# c\ntests\n*.md\n!README.md\n**/*.pyc\n")
    rules = load_rules(tmp_path, ".dockerignore")
    assert excluded_by(rules, "tests/test_a.py") == 2
    assert excluded_by(rules, "notes.md") == 3
    assert excluded_by(rules, "README.md") is None
    assert excluded_by(rules, "docs/setup.md") is None  # `*` does not cross `/`
    assert excluded_by(rules, "src/a/b.pyc") == 5


def test_unsupported_pattern_is_named(tmp_path):
    (tmp_path / ".dockerignore").write_text("file[0-9]\n")
    assert load_rules(tmp_path, ".dockerignore").unsupported == "file[0-9]"
