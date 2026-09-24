"""§4.1: exactly one accepted shape; every other build line is named."""

import pytest

from deployer.reproduce.buildline import BuildConfig, Unsupported, parse_build_line


def test_polygon_build_line():
    assert parse_build_line("docker build --file ./Dockerfile .") == BuildConfig(
        dockerfile="Dockerfile", build_args=(), platform=None, tag=None
    )


def test_all_accepted_flags():
    line = "docker build -f docker/app.Dockerfile --build-arg A=1 --build-arg=B=2 "
    line += "--platform linux/amd64 -t x:1 ."
    assert parse_build_line(line) == BuildConfig(
        dockerfile="docker/app.Dockerfile",
        build_args=(("A", "1"), ("B", "2")),
        platform="linux/amd64",
        tag="x:1",
    )


@pytest.mark.parametrize(
    ("line", "what"),
    [
        ("docker build --file ./Dockerfile . && echo ok", "shell chain"),
        ("docker build . || true", "shell chain"),
        ("docker build .; ls", "shell chain"),
        ("docker build . | tee log", "shell operator |"),
        ("docker build . > log", "shell operator >"),
        ("docker build -t $TAG .", "variable expansion"),
        ("docker build -t $(git rev-parse HEAD) .", "variable expansion"),
        ("docker build app", "context app"),
        ("docker build --secret id=x .", "flag --secret"),
        ("docker build --no-cache .", "flag --no-cache"),
        ("docker build --weird .", "unknown flag --weird"),
        ("docker build -f ../Dockerfile .", "dockerfile path ../Dockerfile"),
        ("docker build -f /abs/Dockerfile .", "dockerfile path /abs/Dockerfile"),
        ("docker buildx build .", "buildx build"),
        ("docker build . extra", "context . extra"),
    ],
)
def test_unsupported_lines_are_named(line, what):
    assert parse_build_line(line) == Unsupported(what)


@pytest.mark.parametrize("line", ["echo hi", "docker version", "make build", ""])
def test_not_a_build_line(line):
    assert parse_build_line(line) is None
