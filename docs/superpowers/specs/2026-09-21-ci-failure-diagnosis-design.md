# Deterministic CI-failure diagnosis (2026-09-21)

Design for `todo://deployer/ci-failure-diagnosis`: read a **real failed GitHub
Actions run of the `ci.yml` this repo authored**, classify the cause, and emit a
verdict that cites evidence from the run.

Scope decided with the owner 2026-09-21 across four design sections. The slice
ends at the **diagnosis**. Authoring a fix from a diagnosis is
`todo://deployer/ci-fix-authoring` and is deliberately outside this spec, so the
founding doc's one-line promise ("diagnose failed CI ... generate/fix") is **not**
closed by this slice alone.

Externally reviewed (rev 1, REQUEST CHANGES, five findings):
`../../../../_cowork_output/deployer-ci-failure-diagnosis-spec-review-2026-09-21.md`.
All five were verified against the code and accepted; the owner ruled on the three
that change existing public behaviour. This revision incorporates them — §5.3 (R1),
§5.4 (R2), §6 (R3), §7 (R4), §4 (R5).

## Why this slice exists

Today `deployer` is closed on the artifact it produced itself: it authors
Dockerfile / compose / ci.yml and verifies them through L1/L2. It knows nothing
about how that artifact fails in a run it did not drive. This slice opens that
loop — with a real forge, not a simulation.

The direction is the founding doc's other half
(`docs/idea-deployer-subproject.md`: "diagnose failed CI"), and it became the
next applied slice once `todo://deployer/first-consumer-seam` shipped.

## Non-goals

- **Authoring the fix.** `todo://deployer/ci-fix-authoring`, blocked on this item.
  L1/L2 alone are not enough to claim "the CI is fixed": they verify the artifact
  this repo produced, not the run that failed.
- **Partial / addressed repair.** Repairing one failure while others stay
  unestablished needs a fix-addressing contract that does not exist (§5.3).
- **Reading a neighbour's repository.** A run from another repo is a handoff by
  construction. It stays unproven and is a later slice. Agreeing with a neighbour
  is deliberately **not** a blocker here.
- **Growing the CI generator.** Authoring gains exactly one axis (§6.1).
- **A model-authored diagnosis.** Approach A (deterministic) was chosen over B
  (LLM) and C (hybrid). The need for a next slice is judged from real
  `UNCLASSIFIED` outcomes, not from anticipation. In documentation the result is
  called **deterministic CI diagnosis**.

## 1. Source of the failed run

A real GitHub Actions run of the `ci.yml` this repo authored, dispatched against
an experiment ref in **this** repository.

Rationale: it is the only candidate where the run is genuine *and* needs nobody
else's consent. A synthetic corpus failure would continue the closed loop this
slice exists to open; a neighbour's run needs an owner and a handoff.

The synthetic corpus case (`corpus/synthetic/ci-build/`) stays as an extra test,
not as the acceptance path. A live run is for integration acceptance, **not** for
every test run — offline regression uses fixtures taken from the live runs.

## 2. Architecture

### 2.1 `src/deployer/forge.py` — the single forge chokepoint

One module is the only place that talks to the GitHub API, by the same argument
that makes `runtime.py` the only place that talks to the container subprocess:
one chokepoint is trivially substitutable in tests and impossible to bypass
unnoticed.

It **gets facts**; it does not interpret them.

- **Identity.** `repo`, run id, **attempt**, head SHA, run URL, job and step ids.
  A re-run must not mix evidence from different attempts. **When `--attempt` is
  omitted the chosen attempt is fixed once, before any jobs or logs are read**,
  and is recorded in the snapshot; nothing downstream re-resolves it.
- **Completeness is a state, not a boolean.** "no annotations", "logs
  unavailable" and "fetch error" are **three different** states. A partially
  collected snapshot is never presented as complete. Jobs and annotations are
  read **with pagination**.
- **Evidence provenance.** Logs and annotations keep their binding to the source
  (job, step). Where the API does not give an exact line→step binding, the
  adapter **does not invent one**; the binding is recorded as absent.
- **Subprocess boundary.** `gh api` is invoked with an argument vector (no
  shell), under a timeout, with explicit handling of `gh` failures and **no
  interactive authorization prompts**. Authentication uses the existing fleet
  profile discipline; this repo grows **no token-storage path of its own**.
- **Fixture.** Versioned serialization. Anonymisation on export preserves both
  the relations between records and the diagnostic markers.

`FailedRun` carries an obligation: the adapter **verifies** that the run is
finished and of a supported conclusion. An unfinished or successful run yields an
explicit refusal, not a snapshot.

### 2.2 `src/deployer/diagnose.py` — the pure classifier

A pure function from snapshot to verdict. It never touches the network, so it
behaves identically on a fixture and on a live snapshot **by construction**.

### 2.3 Layer boundary, kept in the types

The adapter's refusal (unfinished / successful run) is a **result of the previous
layer**, not a classifier outcome. The classifier has exactly **three** outcomes.

## 3. Outcomes

| Outcome | When | Carries |
|---|---|---|
| `CLASSIFIED` | a rule established the cause **and** can cite it | class + evidence with provenance |
| `UNCLASSIFIED` | the failure was read, no rule established a cause | observations; the ambiguity when rules conflict |
| `EVIDENCE_UNAVAILABLE` | logs/annotations unreadable, or the snapshot is incomplete | what exactly is missing |

`UNCLASSIFIED` and `EVIDENCE_UNAVAILABLE` are deliberately **distinct**: the
first says "I looked and do not know", the second says "I could not look".
Collapsing them is the defect class this fleet already has a name for —
unreadable looking clean.

`CLASSIFIED` admits only `AUTHORING`, `ENVIRONMENT` or `PROJECT`, always with
evidence. `UNKNOWN` is never a `CLASSIFIED` class; it is the `FailureKind` that
corresponds to `UNCLASSIFIED`.

**Partial snapshot precedence.** In this slice the outcome is
`EVIDENCE_UNAVAILABLE`; markers already found are preserved as observations. The
absence of *optional* annotations does not by itself mean incompleteness.

**Rule conflict is not resolved by iteration order.** Incompatible causes for
**one** failure yield `UNCLASSIFIED` with the ambiguity explained.

## 4. Unit of diagnosis: per failure, plus a run summary  *(R5)*

The input is a whole run, which may contain several **independent** failures.
Several independent failures are **not** a rule conflict, and must not be
collapsed into one.

- A verdict is produced **per failing job/step**.
- A **run summary** aggregates them. It lists the **distinct established causes**;
  it does not reduce them to a single class.
- Summary precedence, in order:
  1. evidence incomplete anywhere → **`EVIDENCE_UNAVAILABLE`**;
  2. else any unclassified failure present → **`UNCLASSIFIED`**;
  3. else → **`CLASSIFIED`**.
- **Established causes of individual failures are never lost**, whatever the
  summary says. A summary of `UNCLASSIFIED` still carries the `PROJECT` cause
  that was established for job A.

Fixtures: two independent failures (e.g. `PROJECT` in tests and `ENVIRONMENT`
fetching a dependency), and "known cause + unclassified failure" — both asserting
that **job order does not change the result**.

## 5. `FailureKind`, its consumers, and the report contract

Today (`src/deployer/models.py`) the enum has two members, and
`CheckResult.enforce_failure_taxonomy` requires a `FAILED` result to carry one —
so "failed, cause not established" is **currently inexpressible**, which is why
the code falls through to `AUTHORING`. Overloading `None` is not available: the
invariant gives `None` the meaning "this check did not fail".

Two members are added:

- **`PROJECT`** — the evidence points to a defect in the code or tests of the
  project under verification. Established on **positive evidence of a cause in the
  project**, never by the absence of `AUTHORING`/`ENVIRONMENT` markers. A failing
  test does not by itself prove a project defect nor a correct workflow: wrong
  dependencies or wrong invocation parameters produce the same symptom.
- **`UNKNOWN`** — the failure is established, the cause is not.

### 5.1 The two holes in `verify.py` are fixed in this slice

`todo://deployer/failure-classification-channel` describes **existing** behaviour
in the verification path, so closing it in the new classifier would close nothing.

- **`verify.py:976-980`** — `_classify()` returns `AUTHORING` for anything without
  an `ENVIRONMENT` marker. An unknown failure now yields `UNKNOWN`.
- **`verify.py:1437-1444`** — exit 125/126 **with** a transport marker yields
  `ENVIRONMENT`, anything else yields `AUTHORING`. The absence of a transport
  marker does not by itself establish another class: without positive evidence the
  result is `UNKNOWN`, while known causes keep their justified classification.

Belonging to the verification path does not justify an automatic `AUTHORING`.
Tests assert the **correctness of the result**; they must not enshrine the
unreachability of `PROJECT`.

### 5.2 Consumers of the enum

Verified 2026-09-21. ~25 references are **producers**; a new member does not
affect them. Sites that branch on the value:

| Site | What it does |
|---|---|
| `verify.py:980` | fallthrough default — changed here |
| `verify.py:1443` | 125/126 fallthrough (branch opens at 1437) — changed here |
| `verify.py:1270`, `verify.py:1382` | `is FailureKind.AUTHORING` branches |
| `models.py:425` | `environment_failures()`; no complementary `authoring_failures()` |
| `author.py:175-188` | **semantic** consumer — see §5.3 |

`author.py` was missed in the first revision because the count was of *syntactic*
enum comparisons; the authoring loop consumes the taxonomy without naming it.

### 5.3 The authoring loop reacts to the new classes  *(R1, owner ruling)*

`author.py:175` breaks only on `report.environment_failures`; everything else
falls through to `author.repair(...)` at `:188`. The presumption "not environment
→ fix the artifact" is structural, so `UNKNOWN` and `PROJECT` would silently
enter automatic repair.

Ruled by the owner:

- **`UNKNOWN` → stop, reason `unknown_failure`. `PROJECT` → stop, reason
  `project_failure`.**
- **The presence of `AUTHORING` alongside does not permit repair in this slice.**
  Editing the shared artifact can also affect the unestablished cause, so "that
  failure was not addressed" cannot be promised. Partial repair would require a
  separate fix-addressing contract — a non-goal.
- **Repair is permitted only if *all* remaining failed checks are `AUTHORING`.**
- The existing limited retry for `ENVIRONMENT` is kept. Stop-reason priority after
  it: **`ENVIRONMENT` → `UNKNOWN` → `PROJECT`**.
- **All failures are preserved in the report** regardless of which stop reason was
  chosen.

Tested with a spy/mock author asserting the **number of `repair` calls and the
stop reason**, not only the enum in the result.

### 5.4 Report contract: schema 2.0  *(R2, owner ruling)*

`models.py:17` declares `SCHEMA_VERSION = "1.0"`, the enum is closed, and
`README.md:80-81` states that within a major only **added fields** are compatible
— a breaking change to an existing field is not. Widening the value set of
`failure_kind` is exactly that: `CheckResult.model_validate` on
`failure_kind="unknown"` or `"project"` fails with a pydantic enum error, so an
old reader hard-fails rather than degrading.

Ruled by the owner: **bump to `2.0`; the new reader supports majors 0/1/2.**

The new stop reasons from §5.3 are part of the same contract change.

Required checks before promote:
- an old reader **refuses** v2 (explicitly, not silently);
- the new reader reads v0 and v1 data;
- a new run is compared against the **old** golden, and every class change is
  explained.

## 6. The polygon  *(R3, owner ruling)*

`llm.py:115` instructs the model to trigger on push and pull_request;
`verify.py:613-615` rejects a workflow lacking either. A `workflow_dispatch`-only
artifact can therefore today be neither authored nor passed through L1 — the
first revision's §1 and §6 contradicted each other.

### 6.1 `trigger_mode` on `CISpec`

`CISpec` (`models.py:107`) is empty and forbids unknown fields. It gains one
explicit axis, **`trigger_mode`**, named for what it varies: the *way the workflow
is triggered*, not an alternative to build-only.

- `{"ci": {}}` keeps **exactly** its current behaviour (push + pull_request).
- `trigger_mode: manual` permits **only** `workflow_dispatch`; every other
  artifact constraint is retained.
- **L1 validates per mode**: the build-only contract keeps requiring push and
  pull_request, so existing users' checks are unchanged. The prompt gains the
  corresponding branch.
- The polygon workflow lives at its **own path**; the required `ci.yml` is not
  touched.

### 6.2 Bootstrap and acceptance order

GitHub requires a `workflow_dispatch` workflow to exist on the default branch to
be dispatchable ([docs][gh-dispatch]). That inverts the usual rhythm, so the order
is named:

1. **preparatory PR** — `trigger_mode` support plus a **safe** dispatch-only
   workflow at its own path on `master`. It does **not** close the slice;
2. authoring and placement of the failing content on the **experiment commit**;
3. four dispatches (§7.2);
4. evidence captured;
5. final PR.

**No direct push to `master` at any step.**

### 6.3 Isolation, verified 2026-09-21

| Check | Result |
|---|---|
| Required checks | `test` and `governance / gate`; both rulesets target the **default branch only** |
| Merge gate on a red non-required check | `merge-pr.sh:530` admits `UNSTABLE` — the agent merge is not blocked |
| The "watcher" | a waiting discipline (`gh pr checks` + presence of expected checks), not a service; the only surface is a PR's checks |
| Fleet sensors reading run conclusions | one — robin `freshness.py`, pinned to `arch-evidence-freshness.yml` (line 26); this workflow is invisible to it |

**The one real gap.** Check runs attach to a **SHA**, not to a branch. If the
experiment SHA is the head of *any* open PR, its checks appear in that PR. The
condition is checked **on the SHA before every dispatch**; a dedicated experiment
commit makes it hold by construction but does not replace the check.

**Watcher exclusion is bound to the workflow path/ID and `workflow_dispatch`**,
not to a displayed name.

### 6.4 Controlled scenarios

Each scenario isolates **one** cause. The expected class is fixed **before** the
run and is not leaked to the diagnosis — not through the scenario name, not
through a comment.

- **`AUTHORING`** — a broken step in the authored artifact.
- **`ENVIRONMENT`** — a controlled unavailability of a dependency. A deliberately
  wrong registry address is **not** used: it can equally mean `AUTHORING`.
- **`PROJECT`** — a deliberate defect in the project's code/test with an explicit
  assertion message, **plus a control run of the same artifact without the defect,
  which must pass**.

`EVIDENCE_UNAVAILABLE` and incomplete evidence are exercised by **substituting the
adapter**. **Rule conflict is tested directly on a snapshot in the pure
classifier** — it needs no adapter substitution.

## 7. User-facing contract: `deployer diagnose`  *(R4)*

The diagnosis is reachable through the same entry point an operator uses; the
live acceptance call goes through it, not through a one-off script.

- **Input:** a run reference — a run URL, or `--repo` + `--run-id`. `--attempt`
  is explicit; when omitted the attempt is resolved **once** (§2.1).
- **Output:** a verdict document carrying its **own** `schema_version`, with the
  per-failure verdicts and the run summary of §4.
- **`--output-file`** writes the document; stdout carries the human-readable
  summary, stderr the diagnostics.
- **Exit codes distinguish** the three classifier outcomes and the adapter
  refusal. `EVIDENCE_UNAVAILABLE` must not be indistinguishable from success.
- **Fixture input is a test affordance, not a user contract** — stated explicitly
  so it is not relied on by operators.

## 8. Acceptance

### 8.1 Offline — the primary proof, no tokens, no network

- one fixture per class: `AUTHORING`, `ENVIRONMENT`, `PROJECT`;
- two independent failures, and "known cause + unclassified", both order-invariant (§4);
- `EVIDENCE_UNAVAILABLE` and incomplete snapshots via adapter substitution;
- rule conflict on a crafted snapshot in the pure classifier;
- adapter refusal on an unfinished run and on a successful run;
- **rule soundness**: similar messages with a different cause do not yield the same class;
- both `verify.py` holes: unknown failure → `UNKNOWN`; 125/126 without a transport
  marker → `UNKNOWN`;
- authoring loop (§5.3): spy author asserting repair-call count and stop reason,
  including a mixed `AUTHORING` + `UNKNOWN` report;
- schema 2.0 (§5.4): old reader refuses v2; new reader reads v0/v1.

Offline tests are what protect against an "always `AUTHORING`" classifier. The
live runs strengthen acceptance; they are not the only proof of discrimination.

### 8.2 Live — minimum four dispatches, once

Three failures (`AUTHORING`, `ENVIRONMENT`, `PROJECT`) **plus the `PROJECT`
control run that must pass**. Kept as evidence: run URL, commit SHA, failing
job/step, logs. These become the fixtures of §8.1.

### 8.3 Paid benchmark — inside the scope of acceptance

1. `bench run --author anthropic`;
2. **review the diff against the current golden and explain every class change**;
3. only then `promote`.

Refreshing the baseline does not by itself prove correctness.

## 9. Open items to verify before the acceptance run

- The `workflow_dispatch` default-branch requirement is documented
  ([docs][gh-dispatch]), so it is no longer a guess — but the **concrete bootstrap
  and dispatch still need live verification**, done by the first dispatch before
  anything is built on it.

[gh-dispatch]: https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow
