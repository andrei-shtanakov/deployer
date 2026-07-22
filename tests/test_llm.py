from deployer.llm import (
    SYSTEM_PROMPT,
    AnthropicAuthor,
    _context_blocks,
    _extract_dockerfile,
)
from deployer.models import (
    CheckResult,
    CheckStatus,
    DeployTarget,
    FailureKind,
    ProjectFacts,
    RunSpec,
    ServiceSpec,
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


def test_generate_includes_hints_and_system_packages() -> None:
    client = _StubClient("FROM python:3.12-slim\n")
    author = AnthropicAuthor(client=client)
    facts = ProjectFacts(
        package_manager="pip",
        requirements_files={"requirements.txt": ["psycopg2"]},
    )
    target = DeployTarget(system_packages=["curl"])
    author.generate(facts, target)
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "Suspected system dependencies" in prompt
    assert "libpq-dev" in prompt
    assert "Required system packages" in prompt
    assert "curl" in prompt


def test_generate_omits_empty_blocks() -> None:
    client = _StubClient("FROM python:3.12-slim\n")
    author = AnthropicAuthor(client=client)
    author.generate(ProjectFacts(), DeployTarget())
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "Suspected system dependencies" not in prompt
    assert "Required system packages" not in prompt


def test_system_prompt_carries_install_strategy_rules() -> None:
    from deployer.llm import SYSTEM_PROMPT

    assert "uv sync --frozen" in SYSTEM_PROMPT
    assert "--no-install-project" in SYSTEM_PROMPT
    assert "trust build errors over hints" in SYSTEM_PROMPT


def test_author_info_exposes_model_and_prompt_hash() -> None:
    import hashlib

    from deployer.llm import SYSTEM_PROMPT

    info = AnthropicAuthor(client=object()).info()
    assert info.backend == "anthropic"
    assert info.model_id == "claude-opus-4-8"
    assert info.prompt_sha256 == hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()


def test_system_prompt_carries_entrypoint_rule() -> None:
    from deployer.llm import SYSTEM_PROMPT

    assert "script_entrypoint" in SYSTEM_PROMPT
    assert "[project.scripts]" in SYSTEM_PROMPT


def test_run_intent_visible_but_oracle_redacted() -> None:
    target = DeployTarget(run=RunSpec(expect_stdout="secret-oracle-string"))
    rendered = _context_blocks(ProjectFacts(), target)
    assert '"run": {}' in rendered
    assert "secret-oracle-string" not in rendered


def test_build_only_target_renders_null_run() -> None:
    rendered = _context_blocks(ProjectFacts(), DeployTarget())
    assert '"run": null' in rendered


def test_service_target_rendering_unchanged() -> None:
    target = DeployTarget(service=ServiceSpec(port=8000))
    rendered = _context_blocks(ProjectFacts(), target)
    assert '"port": 8000' in rendered


def test_system_prompt_states_job_rule() -> None:
    assert "run" in SYSTEM_PROMPT and "job" in SYSTEM_PROMPT
    assert "exit 0" in SYSTEM_PROMPT


def test_prompt_includes_extras_and_layout_facts() -> None:
    facts = ProjectFacts(
        optional_dependencies={"gui": ["gradio>=6.0"]},
        root_modules=["app.py", "main.py"],
        package_dirs=["agents"],
    )
    target = DeployTarget(extras=["gui"])
    rendered = _context_blocks(facts, target)
    assert '"extras"' in rendered and '"gui"' in rendered
    assert "app.py" in rendered and "agents" in rendered


def test_prompt_hints_follow_requested_extras() -> None:
    facts = ProjectFacts(
        optional_dependencies={"inference": ["llama-cpp-python>=0.2.0"]}
    )
    with_extra = _context_blocks(facts, DeployTarget(extras=["inference"]))
    without = _context_blocks(facts, DeployTarget())
    assert "llama-cpp-python" in with_extra
    assert "llama-cpp-python" not in without


def test_system_prompt_states_extras_and_copy_rules() -> None:
    assert "--extra" in SYSTEM_PROMPT
    assert "root_modules" in SYSTEM_PROMPT and "package_dirs" in SYSTEM_PROMPT


def test_system_prompt_copy_rule_has_empty_facts_escape() -> None:
    assert "If both root_modules and package_dirs are empty" in SYSTEM_PROMPT


def test_system_prompt_entrypoint_precedence() -> None:
    assert "Never override a DeployTarget.entrypoint" in SYSTEM_PROMPT
    first = SYSTEM_PROMPT.index('deploy intent sets "entrypoint"')
    second = SYSTEM_PROMPT.index("[project.scripts]) is non-empty")
    third = SYSTEM_PROMPT.index("script_entrypoint is deterministic")
    assert first < second < third


def test_intent_json_renders_entrypoint() -> None:
    rendered = _context_blocks(
        ProjectFacts(root_modules=["app.py"]),
        DeployTarget(entrypoint="app.py"),
    )
    assert '"entrypoint": "app.py"' in rendered
