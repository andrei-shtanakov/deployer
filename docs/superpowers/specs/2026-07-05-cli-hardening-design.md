# CLI Hardening — Design

Date: 2026-07-05
Status: approved by Andrei (brainstorming session)
Builds on: `2026-07-05-verify-timeouts-design.md` (merged as PR #3)

## Goal and motivation

Two gaps proven by the 2026-07-05 dogfood run against `locallogai-backend`:

- **`deployer verify` loses failure detail.** `_print_report` prints only the
  first line of a check message (`cli.py:60`) and `_cmd_verify` persists
  nothing, so when the L2 build failed, the actual compiler/uv error in the
  15-line stderr tail was unrecoverable. Every failed dogfood run without
  this is lost research data.
- **CLI argument errors surface as tracebacks.** `_load_target` (`cli.py:26`)
  raises bare `FileNotFoundError` / `ValidationError` on a bad `--target`
  path or malformed JSON; `_cmd_author` calls it (`cli.py:92`) even before
  its own `--max-iterations` validation. Neither subcommand checks that the
  project path is a directory. Backlog items from the MVP and PR #3 reviews.

## Exit-code semantics (the design line)

- **exit 2** — the caller invoked the tool wrong: bad flag values, project
  path not a directory, `--target` file missing/unreadable/malformed/invalid.
- **exit 1** — the verification concern failed: Dockerfile missing (state of
  the project under test, unchanged), checks failed, authoring did not
  succeed.
- **exit 0** — success.

Rejected alternative: treating a missing `--target` file as exit 1 alongside
the missing Dockerfile. The Dockerfile is the artifact under test; the target
file is an argument the caller supplied — different kinds of failure.

## Part 1: verify failure detail

### Full failure output (`_print_report`, cli.py:55)

FAILED checks print their full message: the existing one-line header
(`[FAIL] check_id: <first line>`) followed by the remaining message lines
indented with seven spaces — the width of the `[FAIL] ` prefix
(`[` + 4-char icon + `] ` = 7), so tail lines align under the check-id
column. WARNING and SKIPPED checks keep the current first-line-only form —
their tails are not diagnostic. PASSED is unchanged.

Side benefit: `author` calls the same `_print_report` (cli.py:111), so its
per-iteration failure output gains the full tails too — no extra work.

### Persisted report (`_cmd_verify`, cli.py:66)

After printing, `_cmd_verify` always writes the full report —
passed or failed (a green run is research data too: `image_size_bytes`,
check statuses) — to `<project>/.deployer/verify-report.json` via
`report.model_dump_json(indent=2)`, creating the directory with
`mkdir(parents=True, exist_ok=True)`. A final line prints the path,
mirroring `author`'s "run report: …" line. `_cmd_author`'s existing
`report_dir.mkdir(exist_ok=True)` (cli.py:113) also gains `parents=True`
(closes an MVP-review minor).

## Part 2: argument validation

### `_load_target` hardening (cli.py:26)

A new helper owns the error boundary and is used by both subcommands:

- `--target` file does not exist / unreadable (`OSError`) → exit 2
- content is not valid JSON or fails `DeployTarget` validation
  (`pydantic.ValidationError`) → exit 2

Each failure prints `error: <specific reason>` to stderr. Implementation
shape (decided, aligned with the file's existing `_timeout_error` idiom of
"helper returns the problem, caller prints"): `_load_target` returns
`DeployTarget | str`, where `str` is the error message; each command
function does `if isinstance(target, str): print error; return 2`. One
printing location per command, one error idiom per file; the contract is:
no bare traceback can escape from target loading.

### Validation order in both subcommands

All argument validation runs before any work, in this order:

1. `project.is_dir()` — `error: <path> is not a directory` → exit 2
2. `--max-iterations` (author only, existing) → exit 2
3. timeouts (existing `_timeout_error`) → exit 2
4. `--target` loading (above) → exit 2

This fixes `_cmd_author`'s current order, where `_load_target(args.target)`
(cli.py:92) runs before any validation. In `_cmd_verify` the
Dockerfile-existence check (exit 1) runs after all exit-2 validations.

### README

One short block documenting exit codes: 0 success, 1 verification/authoring
failed, 2 invalid invocation.

## Behavior compatibility

- Passing runs: output gains one trailing "report: …" line; check lines
  unchanged. Exit codes for existing valid invocations unchanged.
- `.deployer/verify-report.json` is a new artifact; `verify` previously
  wrote nothing. Existing `.deployer/` dirs are reused.

## Testing

- Multiline FAILED message → every line appears in stdout (capsys).
- WARNING multiline message → still first line only.
- `verify-report.json` written on both pass and fail; content round-trips
  via `VerificationReport.model_validate_json`.
- Bad `--target` (missing file, invalid JSON, JSON failing DeployTarget
  validation) → exit 2 + `error:` on stderr, for **both** subcommands.
- Project path not a directory → exit 2, both subcommands.
- Validation-order pin: non-dir project AND missing `--target` together →
  the dir error wins (guards the ordering Part 2 exists to fix).
- Missing Dockerfile in an existing project dir → still exit 1.
- Existing tests (flag forwarding, timeout validation) untouched.

## Out of scope (YAGNI)

- **Cross-run history**: `verify-report.json` holds the latest run only and
  is overwritten each invocation. This makes failure detail recoverable
  *within* a run, not *across* runs — deliberate; per-run archives belong
  to the future run-config/research seam if ever needed.
- No report format versioning, no `--report-path` flag.
- No expansion of WARNING/SKIPPED tails.
- No structured/JSON output mode for stdout.
- `author`'s report content unchanged (timeouts-in-AuthoringRun is the next
  branch: run-config seam).
