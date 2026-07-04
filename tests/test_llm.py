from deployer.llm import AnthropicAuthor, _extract_dockerfile
from deployer.models import (
    CheckResult,
    CheckStatus,
    DeployTarget,
    FailureKind,
    ProjectFacts,
    VerificationReport,
)


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


class _Messages:
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[dict] = []

    def create(self, **kwargs) -> _Response:
        self.calls.append(kwargs)
        return _Response(self._reply)


class _StubClient:
    def __init__(self, reply: str) -> None:
        self.messages = _Messages(reply)


def test_extract_strips_markdown_fences() -> None:
    fenced = "```dockerfile\nFROM python:3.12-slim\n```\n"
    assert _extract_dockerfile(fenced) == "FROM python:3.12-slim"
    assert _extract_dockerfile("FROM x\n") == "FROM x"


def test_generate_sends_facts_and_returns_dockerfile() -> None:
    client = _StubClient("FROM python:3.12-slim\n")
    author = AnthropicAuthor(client=client)
    facts = ProjectFacts(name="demo", python_version="3.12")
    result = author.generate(facts, DeployTarget())
    assert result == "FROM python:3.12-slim"
    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-4-8"
    assert "demo" in call["messages"][0]["content"]
    assert "temperature" not in call


def test_repair_includes_previous_dockerfile_and_failures() -> None:
    client = _StubClient("FROM python:3.12-slim\nCOPY main.py .\n")
    author = AnthropicAuthor(client=client)
    report = VerificationReport(
        results=[
            CheckResult(
                check_id="copy_sources",
                status=CheckStatus.FAILED,
                failure_kind=FailureKind.AUTHORING,
                message="COPY/ADD sources not found in project: nope.py",
            )
        ]
    )
    author.repair(ProjectFacts(), DeployTarget(), "FROM x\nCOPY nope.py .\n", report)
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "COPY nope.py ." in prompt
    assert "nope.py" in prompt
