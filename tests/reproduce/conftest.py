"""Shared fakes: every container call goes through deployer.runtime.container_run."""

import subprocess
from dataclasses import dataclass, field
from typing import Any

import pytest

from deployer import runtime as runtime_mod


def proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


@dataclass
class FakeContainers:
    """Answers by the longest matching argv prefix; records every call."""

    responses: dict[tuple[str, ...], Any] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, runtime: Any, args: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(args))
        for n in range(len(args), 0, -1):
            answer = self.responses.get(tuple(args[:n]))
            if answer is not None:
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        return proc(1, stderr=f"unexpected call {args}")


@pytest.fixture()
def fake_containers(monkeypatch: pytest.MonkeyPatch) -> FakeContainers:
    fake = FakeContainers()
    monkeypatch.setattr(runtime_mod, "container_run", fake)
    return fake
