# Run-completes check for job intents (Phase 4b)

Date: 2026-07-22
Status: approved (brainstorm 2026-07-22; corpus placement and prompt
redaction corrected in review)

## Motivation

The bench has a measurement blind spot for non-service cases: L2 stops at
`build`, so an image whose CMD is inert — `CMD ["python"]` exits 0
instantly and silently on EOF — counts as success. The Phase-4a
llm-baseline run showed the model *does* invent such CMDs when facts are
thin, and the ledger records the risk explicitly ("inert CMD for
non-service cases — bench blind spot"). As the corpus grows, a green rate
over unverified job images loses meaning.

Design goal: give job artifacts the same "really works" gate that service
artifacts get from `run_healthcheck`, without breaking the legitimate
build-only intent (artifacts that need runtime resources absent from the
sandbox, e.g. a mounted GGUF model).

## Intent model: service | run | build-only

`DeployTarget` gains a third explicit runtime surface next to `service`:

```python
class RunSpec(BaseModel):
    """Job intent: the container must run to completion successfully."""

    expect_stdout: str | None = None
```

- `run: RunSpec | None = None` on `DeployTarget`.
- `service` and `run` are mutually exclusive — a `model_validator`
  rejects targets that set both (surfaces as exit 2 on an invalid
  `target.json`, same as other validation errors).
- No `run` and no `service` → build-only, exactly today's behavior.
  The check is **opt-in**: auto-running every non-service target would
  produce false failures for legitimate build-only artifacts.
- No timeout field: per the verify-timeouts rule, the target says *what*,
  never *how*. The run deadline reuses the existing `--health-timeout`
  value (semantically the generic "runtime check timeout"; renaming the
  flag is deliberately deferred).

Assertion semantics (explicitly tiered — a bare `run: {}` is weaker but
still meaningful):

- `run: {}` asserts: the image's default command exits **0** before the
  timeout. Catches crashes (exit 127, tracebacks) and hangs.
- `run: {"expect_stdout": "..."}` additionally asserts the container's
  stdout contains the substring. This is what catches the inert
  `CMD ["python"]`, which exits 0 silently. Substring match, stdout only
  (not stderr) — the marker is an observable output contract in the
  spirit of `healthcheck_path`.

## Verify flow: the `run_completes` check

In `verify_docker`, after a passed build:

- `target.service is not None` → `run_healthcheck` (unchanged);
- `elif target.run is not None` → new `run_completes` check.

`_run_completes` mirrors `_run_healthcheck`'s sandbox posture but runs
the container in the **foreground**: `container run --name <uuid>
--network=none --memory <limit> <tag>` under a subprocess timeout of
`health_timeout`. stdout/stderr come straight from the captured process —
no separate `logs` call. Cleanup is a best-effort `rm -f` in `finally`.

Classification:

| outcome | status | failure_kind | feedback |
|---|---|---|---|
| exit 0, marker satisfied (or no marker) | PASSED | — | — |
| non-zero exit | FAILED | AUTHORING | tail of output + `_with_command_feedback` |
| exit 0 but marker absent | FAILED | AUTHORING | actual stdout tail + command feedback; **never quotes the expected marker** |
| subprocess timeout (hang) | FAILED | AUTHORING | notes the timeout + command feedback |
| transport markers in output | FAILED | ENVIRONMENT | as in `run_healthcheck` |
| `OSError` / runtime CLI failure | FAILED | ENVIRONMENT | as in `run_healthcheck` |

The AUTHORING messages carry the image's ENTRYPOINT/CMD (Phase-4a
lesson: repair cannot converge on a CMD mistake the error never names).

## Prompt: intent visible, oracle hidden

`llm.py` renders the target into both the generate and repair prompts via
the single chokepoint `_context_blocks` (`target.model_dump_json()`), so
adding `expect_stdout` to the model would leak the oracle automatically,
and the model could game the check with `CMD ["echo", "<marker>"]`.

- `_context_blocks` redacts: when `run` is set, the dumped intent shows
  `"run": {}` — the model sees the runtime surface *kind*, never the
  marker value.
- SYSTEM_PROMPT gains the matching rule: a `run` intent means a job
  image — the CMD must execute the project's entrypoint and exit 0
  (the `script_entrypoint` / `[project.scripts]` precedence rule already
  covers *which* command that is).
- Repair feedback for a missing marker shows the actual stdout tail and
  the image command but never the expected string; an honest CMD derived
  from the entrypoint fact satisfies the marker naturally.
- The verifier-side artifacts (`verify-report.json`,
  `authoring-run.json`) keep the full target including the marker —
  research artifacts, not LLM input.

## Corpus changes

Only **no-build-system** becomes the job case:

- `project/main.py` gets an `if __name__ == "__main__":` guard around
  its print. This makes `script_entrypoint` fire, so the honest CMD
  (`python main.py`) is derivable from deterministic facts — the case
  stays fair for the LLM author.
- New `target.json`:
  `{"run": {"expect_stdout": "hello from no-build-system"}}`.
- `fixture.Dockerfile` already runs `python main.py` — fixture bench
  stays green without edits.

Deliberately unchanged:

- **uv-minimal** stays build-only: it has no root `main.py` (src
  layout), its fixture CMD (`python -c "import uv_minimal; ..."`) is not
  derivable from current facts — a run-check there would be an unfair
  test, not a measurement.
- **slow-build** stays a service case (its `main.py` is an HTTP server
  and `service` excludes `run`).
- **locallogai-backend** (external) stays build-only until extras
  support lands.
- A dedicated `uv-script-job` case exercising `[project.scripts]` as the
  job entrypoint is future coverage, out of this change's scope.

## Golden impact

The committed LLM golden's `checks` lists gain `run_completes` for the
job case, so `bench compare` against the current golden will flag a
divergence — that is a change of measured subject, not a regression
(same situation as the Phase-3→4a promote). After a green
`--author anthropic` run, re-promote the golden.

## Acceptance

- `uv run pytest` and `uv run pytest -m docker` green; corpus smoke
  (`bench verify`) green over all 6 cases.
- `bench run --author fixture` → 6/6 matched (fixture Dockerfile passes
  `run_completes`).
- Docker-marked negative: for the no-build-system project, a Dockerfile
  with `CMD ["python"]` **fails `run_completes` because the stdout
  marker is absent** (exit code alone would pass), and the failure
  message names the image command but not the marker.
- Manual research run `--author anthropic` → 6/6 matched, then
  `bench promote`; `bench compare <run> golden` clean afterwards.
- Prompt-side: unit test asserts the rendered context for a
  marker-bearing target contains `"run"` but not the marker string.

## Testing strategy

- Unit: `RunSpec` validation (service+run mutual exclusion → error);
  `_run_completes` classification matrix over a mocked subprocess
  (exit 0, exit 0 + marker hit/miss, non-zero exit, timeout, transport
  marker, OSError); redaction in `_context_blocks` (generate and repair
  paths); marker never appears in the failure message.
- Docker-marked: job fixture success path; inert-CMD failure path with
  command feedback (the acceptance negative above).
- Bench: corpus smoke unchanged; fixture run stays 6/6.

## Deferred

- `uv-script-job` corpus case (`[project.scripts]` as job entrypoint).
- Renaming `--health-timeout` to a generic runtime-check timeout.
- A stays-up mode for non-HTTP daemons (no corpus case needs it yet).
- Extras in `deploy_target` and the source-layout fact (separate
  Phase-4b items).
