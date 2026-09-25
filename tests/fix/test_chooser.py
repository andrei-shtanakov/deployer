"""The model's contract for a `missing_copy_source` replacement (§4.1):
`build_prompt`'s determinism, `validate_answer`'s strict reading of the
model's JSON reply, and `AnthropicChooser`'s thin wrapper over a fake
client. The real Anthropic backend is never exercised here."""

import json

from deployer.admission.model import Defect
from deployer.fix.binding import Bound, bind_instruction
from deployer.fix.chooser import (
    AnthropicChooser,
    Choice,
    build_prompt,
    validate_answer,
)
from deployer.models import ProjectFacts

_DOCKERFILE = (
    b"FROM python:3.12-slim\n"
    b"COPY app.py other.py /app/\n"
    b"RUN pip install -r requirements.txt\n"
)
_ELIGIBLE = ("main.py", "src/app.py")
_LISTING_PATHS = {"main.py", "src/app.py", "app.py", "requirements.txt"}


def _bound() -> Bound:
    """The COPY instruction of `_DOCKERFILE`, bound to a defect on it."""
    defect = Defect(
        cls="missing_copy_source", file="Dockerfile", lines=(2, 2), object="app.py"
    )
    bound = bind_instruction(_DOCKERFILE, defect)
    assert isinstance(bound, Bound)
    return bound


def _facts() -> ProjectFacts:
    """A minimal, deterministic `ProjectFacts` for prompt/citation tests."""
    return ProjectFacts(name="demo", package_manager="pip")


def _rationale(kind: str, ref: str, explanation: str = "it matches") -> list[dict]:
    """One well-formed rationale entry citing a single fact."""
    return [{"facts": [{"kind": kind, "ref": ref}], "explanation": explanation}]


def _answer(source: str | None, plausible: list[str], rationale: list[dict]) -> str:
    """A raw JSON reply in the required shape."""
    return json.dumps(
        {"source": source, "plausible": plausible, "rationale": rationale}
    )


# --- build_prompt -----------------------------------------------------


def test_build_prompt_is_deterministic() -> None:
    """Two calls with identical inputs produce byte-identical prompts."""
    bound = _bound()
    facts = _facts()
    first = build_prompt(_DOCKERFILE.decode(), bound, "app.py", facts, _ELIGIBLE)
    second = build_prompt(_DOCKERFILE.decode(), bound, "app.py", facts, _ELIGIBLE)
    assert first == second


def test_build_prompt_key_lines() -> None:
    """The prompt states the absent source, the eligible list, the JSON
    contract, and the `plausible == [source]` rule, each as its own line."""
    bound = _bound()
    prompt = build_prompt(_DOCKERFILE.decode(), bound, "app.py", _facts(), _ELIGIBLE)
    lines = prompt.splitlines()
    assert "Absent source: app.py" in lines
    assert "- main.py" in lines
    assert "- src/app.py" in lines
    assert any('"source"' in line and '"plausible"' in line for line in lines)
    assert any(
        "plausible MUST equal exactly" in line and '["<source>"]' in line
        for line in lines
    )


def test_build_prompt_lists_project_facts_fields_from_model_fields() -> None:
    """The citable `"fact"` field names come from `ProjectFacts.model_fields`,
    not a hardcoded list — every declared field name appears in the prompt."""
    prompt = build_prompt(_DOCKERFILE.decode(), _bound(), "app.py", _facts(), _ELIGIBLE)
    for name in ProjectFacts.model_fields:
        assert f"- {name}" in prompt.splitlines()


# --- validate_answer: a valid answer -----------------------------------


def test_validate_answer_valid_bare_json() -> None:
    """A bare JSON object with a resolved source returns a `Choice`."""
    raw = _answer("main.py", ["main.py"], _rationale("path", "main.py"))
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert result == Choice(source="main.py", rationale=_rationale("path", "main.py"))


def test_validate_answer_valid_fenced_once() -> None:
    """A single fenced code block wrapping the JSON is accepted."""
    raw = (
        "```json\n"
        + _answer("main.py", ["main.py"], _rationale("fact", "name"))
        + ("\n```")
    )
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, Choice)
    assert result.source == "main.py"


def test_validate_answer_valid_fenced_no_language_tag() -> None:
    """A fence with no language tag is accepted the same way."""
    raw = (
        "```\n"
        + _answer("main.py", ["main.py"], _rationale("path", "main.py"))
        + ("\n```")
    )
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, Choice)


# --- validate_answer: null source / several plausible -------------------


def test_validate_answer_null_source_no_replacement() -> None:
    """A null source with no plausible candidates is a clean stop."""
    raw = _answer(None, [], _rationale("path", "main.py"))
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert result == "fix method not established: the model found no replacement"


def test_validate_answer_null_source_one_plausible_still_no_replacement() -> None:
    """A single plausible candidate without a committed source is still
    read as no replacement established, not as an error."""
    raw = _answer(None, ["main.py"], _rationale("path", "main.py"))
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert result == "fix method not established: the model found no replacement"


def test_validate_answer_several_plausible_candidates() -> None:
    """Two distinct eligible plausible candidates with a null source is
    reported as ambiguity, not as "no replacement"."""
    raw = _answer(None, list(_ELIGIBLE), _rationale("path", "main.py"))
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert result == "fix method not established: several plausible candidates"


# --- validate_answer: malformed forms ------------------------------------


def test_validate_answer_prose_is_malformed() -> None:
    """Plain prose, not JSON at all, is malformed."""
    result = validate_answer("I choose main.py.", _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_prose_before_json_is_malformed() -> None:
    """Prose before the JSON object is malformed, even if the JSON itself
    would otherwise be valid."""
    raw = "Here is my answer: " + _answer(
        "main.py", ["main.py"], _rationale("path", "main.py")
    )
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_prose_after_json_is_malformed() -> None:
    """Prose after the JSON object is malformed."""
    raw = (
        _answer("main.py", ["main.py"], _rationale("path", "main.py"))
        + "\nHope that helps!"
    )
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_unclosed_fence_is_malformed() -> None:
    """A fence opened but never closed is malformed."""
    raw = "```json\n" + _answer("main.py", ["main.py"], _rationale("path", "main.py"))
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_text_outside_fence_is_malformed() -> None:
    """Text outside a fenced block, even trailing, is malformed."""
    raw = (
        "```json\n"
        + _answer("main.py", ["main.py"], _rationale("path", "main.py"))
        + "\n```\nthanks"
    )
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_two_json_objects_is_malformed() -> None:
    """Two concatenated JSON objects are not "exactly one"."""
    one = _answer("main.py", ["main.py"], _rationale("path", "main.py"))
    result = validate_answer(one + one, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_not_an_object_is_malformed() -> None:
    """A JSON value that isn't an object (e.g. a bare string) is malformed."""
    result = validate_answer('"main.py"', _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_unknown_extra_key_is_malformed() -> None:
    """An unknown extra top-level key is malformed."""
    data = json.loads(_answer("main.py", ["main.py"], _rationale("path", "main.py")))
    data["confidence"] = 0.9
    result = validate_answer(json.dumps(data), _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_missing_key_is_malformed() -> None:
    """A missing required key is malformed."""
    data = json.loads(_answer("main.py", ["main.py"], _rationale("path", "main.py")))
    del data["rationale"]
    result = validate_answer(json.dumps(data), _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_source_wrong_type_is_malformed() -> None:
    """A non-string, non-null `source` is malformed."""
    data = json.loads(_answer("main.py", ["main.py"], _rationale("path", "main.py")))
    data["source"] = 7
    result = validate_answer(json.dumps(data), _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_plausible_not_a_list_is_malformed() -> None:
    """`plausible` must be a list of strings."""
    data = {
        "source": "main.py",
        "plausible": "main.py",
        "rationale": _rationale("path", "main.py"),
    }
    raw = json.dumps(data)
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_plausible_duplicate_is_malformed() -> None:
    """A duplicate entry in `plausible` is malformed."""
    raw = _answer(None, ["main.py", "main.py"], _rationale("path", "main.py"))
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_plausible_outside_eligible_is_malformed() -> None:
    """A `plausible` entry outside `eligible` is malformed."""
    raw = _answer(None, ["not-eligible.py"], _rationale("path", "main.py"))
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_plausible_mismatch_with_source_is_malformed() -> None:
    """A non-null `source` whose `plausible` names a different path is
    malformed, not a "several candidates" stop."""
    raw = _answer("main.py", ["src/app.py"], _rationale("path", "main.py"))
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_source_outside_eligible_is_malformed() -> None:
    """A non-null `source` outside `eligible` is malformed."""
    raw = _answer("not-eligible.py", ["not-eligible.py"], _rationale("path", "main.py"))
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_empty_rationale_is_malformed() -> None:
    """An empty `rationale` list is malformed."""
    raw = _answer("main.py", ["main.py"], [])
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_rationale_extra_key_is_malformed() -> None:
    """A rationale entry with an unexpected extra key is malformed."""
    entry = _rationale("path", "main.py")[0]
    entry["confidence"] = "high"
    raw = _answer("main.py", ["main.py"], [entry])
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_rationale_missing_explanation_is_malformed() -> None:
    """A rationale entry missing `explanation` is malformed."""
    raw = _answer(
        "main.py", ["main.py"], [{"facts": [{"kind": "path", "ref": "main.py"}]}]
    )
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_empty_explanation_is_malformed() -> None:
    """An empty `explanation` string is malformed."""
    raw = _answer("main.py", ["main.py"], _rationale("path", "main.py", ""))
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_rationale_no_facts_is_malformed() -> None:
    """A rationale entry with an empty `facts` list is malformed."""
    raw = _answer("main.py", ["main.py"], [{"facts": [], "explanation": "why"}])
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_fact_citation_bad_kind_is_malformed() -> None:
    """A citation `kind` other than "path"/"fact" is malformed."""
    raw = _answer("main.py", ["main.py"], _rationale("guess", "main.py"))
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")


def test_validate_answer_path_citation_missing_from_listing_is_malformed() -> None:
    """A `"path"` citation not in `listing_paths` is malformed."""
    raw = _answer("main.py", ["main.py"], _rationale("path", "ghost.py"))
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")
    assert "ghost.py" in result


def test_validate_answer_fact_citation_unknown_field_is_malformed() -> None:
    """A `"fact"` citation naming a field `ProjectFacts` doesn't have is
    malformed."""
    raw = _answer("main.py", ["main.py"], _rationale("fact", "not_a_real_field"))
    result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
    assert isinstance(result, str) and result.startswith("malformed answer:")
    assert "not_a_real_field" in result


def test_validate_answer_fact_citation_known_field_accepted() -> None:
    """Every real `ProjectFacts` field name is a valid `"fact"` citation."""
    for name in ProjectFacts.model_fields:
        raw = _answer("main.py", ["main.py"], _rationale("fact", name))
        result = validate_answer(raw, _ELIGIBLE, _facts(), _LISTING_PATHS)
        assert isinstance(result, Choice), f"field {name!r} was rejected"


# --- AnthropicChooser -----------------------------------------------------


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    def __init__(self, blocks: list[_Block]) -> None:
        self.content = blocks


class _Messages:
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[dict] = []

    def create(self, **kwargs: object) -> _Response:
        self.calls.append(kwargs)
        return _Response([_Block(self._reply)])


class _StubClient:
    def __init__(self, reply: str) -> None:
        self.messages = _Messages(reply)


def test_anthropic_chooser_returns_text_of_first_content_block() -> None:
    """`choose` sends one message and returns the reply's text content."""
    client = _StubClient('{"source": "main.py"}')
    chooser = AnthropicChooser(client=client)
    result = chooser.choose("pick a source")
    assert result == '{"source": "main.py"}'
    call = client.messages.calls[0]
    assert call["model"] != ""
    assert call["messages"] == [{"role": "user", "content": "pick a source"}]
    assert len(client.messages.calls) == 1


def test_anthropic_chooser_uses_default_model() -> None:
    """With no model override, the client is called with `DEFAULT_MODEL`."""
    from deployer.llm import DEFAULT_MODEL

    client = _StubClient('{"source": null}')
    chooser = AnthropicChooser(client=client)
    chooser.choose("pick a source")
    assert client.messages.calls[0]["model"] == DEFAULT_MODEL


def test_anthropic_chooser_never_retries() -> None:
    """A malformed reply is returned as-is; `choose` does not call again."""
    client = _StubClient("not json at all")
    chooser = AnthropicChooser(client=client)
    result = chooser.choose("pick a source")
    assert result == "not json at all"
    assert len(client.messages.calls) == 1
