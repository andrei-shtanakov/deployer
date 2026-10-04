# Forge not-executed jobs — design ("a sibling cancelled before it ran is not a failure, and does not hide one")

**Status:** DRAFT rev 2.2. Designed with the owner on 2026-10-04. Approach: a closed set of
recognised signals, three separate decisions (the binding population, the verdict and
completeness), and five refinements (§2–§5, §8). Rev 2 resolves the external review of
rev 1 at `07f6c95`
(`../../../../_cowork_output/deployer-forge-not-executed-jobs-spec-review-2026-10-04.md`,
a dev-only workspace file):
- R1: the completeness exemption is per dimension and reaches the run-level
  explanations (§6).
- R2: the timestamps must be valid and in the recorded form (§2).
- R3: the boundary regression names the actual refusal, and `fix` gets an explicit
  fixture (§8).

It also takes up the review's non-blocking details: at least one kept job for §6.1, the
archive-not-attempted case in §4, and validating a stored `NotExecuted` in §3.3. Next:
the owner re-checks the changed sections, then a plan. Rev 2.1 closes the re-review's one
remaining P2: `NotExecuted` is loaded with strict validation of its own fields (§3.3),
because Pydantic's default lax mode turns `false` into `0` and `"404"` into `404` before
any predicate could see them. Rev 2.2 (planning): §9.1 states the provable form of "real
failures cite their own lines". No code exists for this design.
**Item:** `todo://deployer/step-binding-never-started-jobs`.
**Base:** `master` @ `45887b9` (#116), with the `steps-2` recording of PR #117
(`tests/fixtures/step-binding/`, `steps-2/observed.json` as **O2**, PROVENANCE as **P**).
The archive step-binding design (`2026-10-03-forge-archive-step-binding-design.md`,
rev 2.4) is cited as **B**.

## Purpose

A fail-fast matrix cancels a sibling that has not started yet. In the `steps-2`
recording (O2, `waiting-legs (never-starts)`) that job is kept, because `cancelled` is not
green. Its log does not exist, and `gh` reports a bare `gh: HTTP 404`, read since #116 as
`error`. The consequences today (O2, "through forge"):

- **Binding:** the job's log is not `present`, so every kept job reads `unverifiable`
  (B §4.1). The four jobs that ran lose the binding their archive could prove.
- **Verdict:** the job gets its own failure verdict, with nothing in it.
- **Completeness:** its `error` feeds the run-level worst-of, so the whole run reads
  `EVIDENCE_UNAVAILABLE`, although every job that ran was read completely.

This design recognises one supported, recorded form of "cancelled before execution" and
handles three decisions separately for a job of that form. It never hides the job, its
cancellation or its log read.

## Non-goals

- A universal proof that a job did not execute. Only the form in §2 is supported, and
  anything that contradicts it takes the ordinary path, without exclusion.
- The archive as evidence for or against execution. Per-step files expire (P), and a
  directory name proves nothing (B §4.2).
- `read_attempt` and the logic of `reproduce`, `fix` and `admission`. Their consumption of
  a diagnose snapshot is guarded by a regression only (§8).
- The dispatcher-like shape: a runner assigned, 0 steps, no log. It is not recognised and
  stays on the ordinary path.

## 1. Where the decision is made

In `fetch_failed_run`, once per kept job, from data forge already has: the job record of
the attempt's listing, and the job's own log read. There are no extra requests, and the
log is not read a second time. The result is recorded on the job (§3) and consumed by
`_bind_steps` (§4) and by `diagnose` (§5, §6).

## 2. The supported form (all required)

A kept job is **recognised as cancelled before execution** only when every one of these
holds, read from its record in the jobs listing exactly as served. Missing or ill-typed
values are never turned into matching ones by a default.

| Signal | Required value | As observed (O2) |
|---|---|---|
| `status` | the string `"completed"` | `completed` |
| `conclusion` | the string `"cancelled"` | `cancelled` |
| `runner_id` | the integer `0` (not `bool`, not a string) | `0` |
| `runner_name` | the string `""` (present and empty; `null` or absent does not match) | `""` |
| `steps` | a present, empty list `[]` | `[]` |
| `created_at`, `started_at` | both present, both strings in exactly the recorded form `YYYY-MM-DDTHH:MM:SSZ` (`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`) that also parse as a valid UTC instant, and equal | `2026-10-04T09:13:25Z` both |
| the job's own log read | HTTP status `404` (structural, §3.1) | `gh: HTTP 404` → 404 |

Any other value for any signal is a contradiction or a gap, and the job takes the
ordinary path. That includes a log that is present, 410, any other status, a status-less
failure (which propagates as today), a missing field, a non-empty or `null` runner name,
a step, or `started_at ≠ created_at`. Timestamps follow the same rule: equal empty
strings, equal unrecognised strings (`"unknown"`), `null`, a non-string value, a missing
member of the pair, any other form (fractional seconds, an offset in place of `Z`), or an
impossible date (`2026-02-30T…`) are gaps, not equal instants. The set is closed. Adding an equivalent (for
example `runner_name: null`) needs a recording that shows it.

The wording is deliberate: the form is the **supported basis** for treating the job as
cancelled before execution, because it is the form recorded on a real run (O2) and seen on
a real steward job (2026-10-03). It is not a claim about every way GitHub can cancel a job.

## 3. Representation

### 3.1 The HTTP status, structurally

`_Gh.logs` returns a `LogRead(text: str, state: LogsState, status: int | None)` in place of
the `(text, state)` tuple. `status` is the HTTP status of a failed read (`None` when the
read succeeded). It is set from `GhError.status`, which `_gh_failure` already parses (#116
for the bare form). It is never re-derived from a message or a reason, and the log is not
re-read. The existing `state` mapping is unchanged: 410 is `unavailable`, any other status
is `error`, and a status-less failure propagates. All three callers (`fetch_failed_run`,
`_bind_steps`, `_read_all_jobs`) take a `LogRead`, and only `fetch_failed_run` uses
`status`.

### 3.2 `NotExecuted` on the job (snapshot 1.6)

`FailedJob.not_executed: NotExecuted | None` (default `None`, which means not recognised
or not attempted). A recognised job carries every basis of the decision, as checked:

```
NotExecuted(
    status="completed", conclusion="cancelled",
    runner_id=0, runner_name="",
    steps=0, created_at=…, started_at=…,
    log_status=404,
)
```

The job stays in `FailedRun.jobs`, with its original `conclusion` (`cancelled`), its own
`completeness` as read (`logs: "error"`), its `all_steps` (empty) and its evidence (none
from a log). Nothing makes it look read. Schema 1.6 is additive: 1.5 and older load with
`None`.

### 3.3 Consuming a stored `NotExecuted`

**Strict loading, for `NotExecuted`'s fields only.** Plain `int` and `str` annotations are
not enough. Pydantic's default lax mode coerces on load, and was confirmed in this
project to turn `false` into `0`, `"0"` into `0` and `"404"` into `404`, after which no
predicate can see the original type. `NotExecuted` therefore declares:

- `runner_id`, `steps` and `log_status` as `Annotated[int, Strict()]`. This rejects
  `false`, `true`, `"0"`, `0.0` and `"404"`.
- `runner_name`, `created_at` and `started_at` as `Annotated[str, Strict()]`. This rejects
  `null` and numbers.
- `status` and `conclusion` as `Literal["completed"]` and `Literal["cancelled"]`.
  `Strict()` does not apply to a `Literal`, and the `Literal` already accepts only the
  exact string: `"Completed"` and `1` are rejected.

The strictness is scoped to these fields. Every other snapshot field keeps its existing
lax behaviour, so a stored `job_id: "8"` still loads as `8`, as today. A 1.6 document
whose `not_executed` holds an ill-typed value fails to load through `load_snapshot`, as
any malformed snapshot does. It is never silently coerced into a recognised job. The
behaviour was checked in this project's environment with the `TypeAdapter` that
`load_snapshot` uses.

A value that loads is still not trusted on its own: diagnose consumes it only through one
validating predicate, `is_not_executed(job)`, which returns true only when both of these
hold.

1. The stored basis satisfies §2 on its own values: `completed`, `cancelled`, `0`, `""`,
   0 steps, both timestamps valid in the recorded form and equal, and `log_status` 404.
2. It agrees with the job it sits on: `job.conclusion == "cancelled"`, `job.all_steps` is
   `[]`, `job.steps` is empty, `job.completeness.logs == "error"`, and no evidence comes
   from a log (only annotation evidence, `source == job_id`, may be present).

A stored `NotExecuted` that fails the predicate is ignored: the job takes the ordinary
path, and diagnose adds the run observation `job <id>: stored not_executed contradicts the
job; ignored`. An arbitrary non-null object is never enough.

## 4. Decision 1 — the binding population (B §4.1)

A recognised job is outside the population of the uniqueness check. It never ran, so no
step directory can be its, and its missing log no longer makes the others `unverifiable`.
Its own `step_binding` is a new state, `excluded`, with the reason "cancelled before
execution", **whatever the archive state**: `excluded` is set even when the archive is
absent, refused or unavailable, and even when no archive was attempted (a runner without
the capped download, B §8), because the basis does not depend on the archive. The
order of precedence for a recognised job is `excluded` over every archive-derived state
and over "not attempted".
For every other kept job the B rules are unchanged.

The steps of the decision are fixed:
1. Recognise (§2) on the kept jobs' records and reads.
2. Read the population's logs as in B §4.1, without the recognised jobs.
3. Prove ownership (B §4.2–§4.3) over the remaining population.

A recognised job is never matched to a directory.

## 5. Decision 2 — the verdict

`diagnose` creates **no failure verdict** for a recognised job. That job has no log evidence
to read, and an empty verdict would show a failure that did not happen. Annotation
evidence the job may carry (`source == job_id`) is kept in the snapshot unchanged, but is
not cited by any verdict. Instead the run gets
one observation per recognised job: `job <id> (<name>) was cancelled before execution
(runner 0, no steps, log 404)`. The verdicts of every other kept job are formed exactly
as before. That includes jobs cancelled mid-execution (O2 `long-1`, `long-2`): they ran,
they keep their verdicts and their binding, and the ordinary rules apply.

## 6. Decision 3 — completeness

The exemption covers **one dimension only: the recognised job's log read**, the expected
404. Recognition never inspects annotations, so they are not exempt.

- **The logs dimension.** The recognised job's `logs` state is left out of the run-level
  worst-of `FailedRun.completeness.logs` (forge) and out of diagnose's incompleteness
  check (`_incomplete`). Its own `FailedJob.completeness.logs` stays `error`.
- **The annotations dimension.** It is unchanged. A recognised job's annotations state
  still feeds `FailedRun.completeness.annotations` and the incompleteness check. An
  `error` there makes the run `EVIDENCE_UNAVAILABLE`, exactly as for any other job, and
  is reported as such. No standalone verdict is needed to report it.
- **Run-level explanations** follow the same policy. `_run_missing` (and `_missing` per
  job) does not report the recognised job's expected log `error` as lost evidence. It
  still reports a recognised job's annotations error, and every other job's losses,
  exactly as before. The §5 cancellation observation stays in every case.

Every other job's incompleteness counts exactly as before. A real failure of a job that
ran therefore stays visible: an `error` read of a job that is not recognised still makes
the run `EVIDENCE_UNAVAILABLE`, and is reported, without the excluded 404 coming back as
lost evidence.

### 6.1 Nothing left to evaluate

If **at least one job was kept and** every kept job is recognised, the failed run has no
job that ran and failed. The zero-kept-job path (nothing fetched) is untouched: it keeps
its existing outcome and observations, and `all([])` never reaches this rule. The result
is **conservative and explicit**:
- the outcome is `EVIDENCE_UNAVAILABLE`, never `UNCLASSIFIED` and never anything that reads
  as complete;
- the run observation says why: `every non-green job was cancelled before execution; no
  job log holds the failure`, beside the per-job observations of §5.

The empty-set note (`_EMPTY_SET_NOTE`) is not used for this case: the run did expose jobs,
and they were set aside for a stated reason.

## 7. Diagnose, CLI and documents

- The verdict document's `run` carries `not_executed` on each job. `verdict_schema_version`
  does not change, because the nested snapshot is versioned on its own. The new
  observations reach stdout as observations already do.
- No new CLI flag.
- The README's snapshot paragraph gains 1.6.

## 8. The boundary with reproduce, fix and admission

Their logic is unchanged. A diagnose snapshot can reach them (`diagnose --reproduce`, and
`fix` reading R's snapshot), so a regression pins that the new field gives them no
positive outcome they did not have:
- **reproduce.** Replay `steps-2` through `fetch_failed_run` and call
  `reproduce.precheck` on the five-job snapshot. Today the result is
  `Refusal(reason='checkout SHA not established')`. `precheck` checks each job's checkout
  SHA before it counts jobs, and the never-started job has none (confirmed by the
  external review's replay). The regression pins that the result is equal, the same
  `Refusal`, with and without `not_executed` on the job. `precheck`'s order is not
  changed to suit this design.
- **fix.** A raw `steps-2` snapshot is refused by reproduction, so it has no R binding to
  re-derive. The fixture is therefore the existing fix bundle
  `tests/fixtures/fix/copy-basename-unique`, whose R binding succeeds. In a labelled
  synthetic derivation, a recognised never-started sibling (with `not_executed` set from
  the `steps-2` values) is added to the stored run that `fix.author._build` reads. The
  regression runs the actual `_build` path through the existing author test harness. It
  asserts that the result, `(BuildConfig, workflow path, workflow SHA-256)` or the same
  refusal, is identical with and without the sibling and the field.

Keeping the job in `jobs` is the conservative choice, because no consumer sees fewer jobs
than GitHub listed.

## 9. Acceptance

### 9.1 On the real `steps-2` recording (replayed through `fetch_failed_run` and `diagnose_run`)

- `waiting-legs (never-starts)` is recognised. `not_executed` carries the eight values of
  §3.2 exactly as observed (O2), `step_binding` is `excluded` and `completeness.logs` is
  `error`.
- The four jobs that ran are `bound`, and the archive is `available`. Each `MARK-<leg>`
  line, assembled at run time as in the recording, lands in the evidence of its job's
  run step.
- The verdicts are exactly the step verdicts of the four jobs that ran: their composition
  is pinned by `(job, step)`, and `never-starts` has none.
  - The real failures stay visible: `parallel-legs (fail-fast)` and
    `waiting-legs (first-fails)` keep their own step verdicts. Their `MARK-<leg>` lines lie
    in their own step's evidence. Every block a verdict cites has the source of that
    verdict's own step, so no verdict cites another job's lines. Whether a rule observes
    anything in `MARK-…` is not part of this design (rev 2.2).
  - `long-1` and `long-2` (cancelled mid-execution) keep verdicts under the ordinary rules.
  - No verdict cites another job's evidence.
- The run outcome is `UNCLASSIFIED`. The run observations include the §5 line for
  `never-starts`, and nothing reports a lost log.
- `job_text` of every job is byte-identical to its pre-design value (B §5.3).

### 9.2 Synthetic, derived from `steps-2` (named transformations, labelled in PROVENANCE)

| Case | Derivation | Expected |
|---|---|---|
| dispatcher-like shape | `never-starts`' record with `runner_id` 1000028882 and a runner name | not recognised; every kept job `unverifiable`; the run `EVIDENCE_UNAVAILABLE` (today's behaviour) |
| `status` ≠ completed | `status: "in_progress"` | not recognised |
| `conclusion` ≠ cancelled | `conclusion: "failure"` | not recognised |
| `runner_id` ≠ 0 | `1` | not recognised |
| `runner_id` ill-typed | `"0"`, and separately `false` | not recognised |
| `runner_name` non-empty | `"GitHub Actions 1"` | not recognised |
| `runner_name` null / absent | `null`; the key removed | not recognised |
| steps present | one step | not recognised |
| steps absent | the key removed | not recognised |
| `started_at ≠ created_at` | one second later | not recognised |
| a timestamp absent | `started_at` removed | not recognised |
| the log present | a log text served | not recognised |
| the log 410 | `GhError(…, 410)` | not recognised (`unavailable` as today) |
| the log another status | `GhError(…, 502)` | not recognised |
| the log status-less | `GhError(…, None)` | propagates, as today |
| timestamps equal and empty | both `""` | not recognised |
| timestamps equal and malformed | both `"unknown"` | not recognised |
| timestamps null / ill-typed | both `null`; separately both `0` | not recognised |
| either timestamp missing | `created_at` removed; separately `started_at` removed | not recognised |
| another timestamp form | both `2026-10-04T09:13:25.000Z`; separately both `…+00:00` | not recognised |
| an impossible date | both `2026-02-30T09:13:25Z` | not recognised |
| every kept job recognised | the run's other kept jobs removed (at least one kept) | §6.1: `EVIDENCE_UNAVAILABLE` with the explanation; no verdicts |
| zero kept jobs | every job green | the existing nothing-fetched outcome, unchanged |
| a real error beside an exclusion | `parallel-legs (long-1)`'s log served as 502 | `never-starts` still recognised. The run is `EVIDENCE_UNAVAILABLE` for `long-1`'s read. The run observations hold the cancellation line for `never-starts` and `long-1`'s lost log, and **not** `never-starts`' 404 as lost evidence |
| a recognised job's annotations error | `never-starts`' annotations served as 502 | `never-starts` still recognised and `excluded`, no verdict. `FailedRun.completeness.annotations` is `error`. The run is `EVIDENCE_UNAVAILABLE`, with an observation naming `never-starts`' annotations fetch error beside the cancellation line |
| archive absent / refused / unavailable / not attempted | four variants: no step files; corrupt; HTTP 404; a runner without `api_bytes_capped` | `never-starts` `excluded` in each. The others get `no_archive`, or `None` when not attempted |
| a stored `not_executed` with an ill-typed value, through the real `load_snapshot` | a valid 1.6 snapshot with, one at a time: `runner_id: false`, `runner_id: "0"`, `steps: true`, `log_status: "404"`, `runner_name: null`, `created_at: 0`, `status: "Completed"` | `load_snapshot` raises a validation error for each. None of them loads as a recognised job. In the same document, an ordinary lax field (`job_id: "8"`) still loads as `8` |
| a stored `not_executed` that contradicts its job | a 1.6 snapshot whose recognised job is given `conclusion: "failure"` (or a log evidence block, or `log_status: 410`) | the predicate of §3.3 fails: the ordinary path, with the "contradicts the job; ignored" observation |

### 9.3 The boundary (§8)

As §8. `precheck` gives `Refusal('checkout SHA not established')` with and without the
field. `fix.author._build` on the `copy-basename-unique` derivation gives an identical
result with and without the recognised sibling.

## 10. Order of work (for the plan)

1. `LogRead` with a structural status (§3.1). It is pure plumbing, and every existing test
   stays green.
2. Recognition (§2) as a pure function over `(record, LogRead)`, and the validating
   predicate (§3.3), with every negative case of §9.2.
3. Snapshot 1.6 and wiring in `fetch_failed_run` and `_bind_steps` (§3.2, §4, run-level
   completeness).
4. Diagnose (§5, §6, §6.1).
5. Acceptance on `steps-2` (§9.1), the synthetic cases (§9.2), the boundary (§9.3); README
   and TODO.
