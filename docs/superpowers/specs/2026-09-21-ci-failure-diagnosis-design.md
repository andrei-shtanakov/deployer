# Deterministic CI-failure diagnosis (2026-09-21)

Design for `todo://deployer/ci-failure-diagnosis`: read a **real failed GitHub
Actions run of the `ci.yml` this repo authored**, classify the cause, and emit a
verdict that cites evidence from the run.

Scope decided with the owner 2026-09-21 across four design sections. The slice
ends at the **diagnosis**. Authoring a fix from a diagnosis is
`todo://deployer/ci-fix-authoring` and is deliberately outside this spec, so the
founding doc's one-line promise ("diagnose failed CI ... generate/fix") is **not**
closed by this slice alone.

Externally reviewed: *(pending — `../../../../_cowork_output/deployer-ci-failure-diagnosis-spec-review-2026-09-21.md`)*

## Why this slice exists

Today `deployer` is closed on the artifact it produced itself: it authors
Dockerfile / compose / ci.yml and verifies them through L1/L2. It knows nothing
about how that artifact fails in a run it did not drive. This slice opens that
loop — with a real forge, not a simulation.

The direction is the founding doc's other half
(`docs/idea-deployer-subproject.md`: "diagnose failed CI"), and it became the
next applied slice once `todo://deployer/first-consumer-seam` shipped and proved
the first producer→consumer seam.

## Non-goals

- **Authoring the fix.** `todo://deployer/ci-fix-authoring`, blocked on this item.
  L1/L2 alone are not enough to claim "the CI is fixed": they verify the artifact
  this repo produced, not the run that failed.
- **Reading a neighbour's repository.** A run from another repo is a handoff by
  construction (seam audit, finding "not recommended as the first seam"). It stays
  unproven and is a later slice. Agreeing with a neighbour is deliberately **not**
  a blocker here.
- **Growing the CI generator.** CI authoring is widened only as far as the
  controlled scenarios need.
- **A model-authored diagnosis.** Approach A (deterministic) was chosen over B
  (LLM) and C (hybrid). We do not promise a model or a hybrid now; the need for a
  next slice is judged from real `UNCLASSIFIED` outcomes, not from anticipation.
  In documentation the result is called **deterministic CI diagnosis**.

## 1. Source of the failed run

A real GitHub Actions run of the `ci.yml` this repo authored, dispatched against
a sandbox ref in **this** repository.

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

It **gets facts**; it does not interpret them. Given a run reference it returns a
typed snapshot whose every field is a forge fact.

Contract requirements:

- **Identity.** `repo`, run id, **attempt**, head SHA, run URL, job and step ids.
  A re-run must not mix evidence from different attempts.
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

`FailedRun` is the snapshot type, and the name carries an obligation: the adapter
**verifies** that the run is finished and of a supported conclusion. An
unfinished or successful run yields an explicit refusal, not a snapshot.

### 2.2 `src/deployer/diagnose.py` — the pure classifier

A pure function from snapshot to verdict. It never touches the network, so it
behaves identically on a fixture and on a live snapshot **by construction**, not
by discipline.

### 2.3 Layer boundary, kept in the types

The adapter's refusal (unfinished / successful run) is a **result of the previous
layer**, not a classifier outcome. The classifier has exactly **three** outcomes.
The boundary is preserved in the types rather than in prose.

## 3. The verdict model

| Outcome | When | Carries |
|---|---|---|
| `CLASSIFIED` | a rule established the cause **and** can cite it | class + evidence with provenance |
| `UNCLASSIFIED` | the failure was read, no rule established a cause | observations, and the ambiguity when rules conflict |
| `EVIDENCE_UNAVAILABLE` | logs/annotations unreadable, or the snapshot is incomplete | what exactly is missing |

`UNCLASSIFIED` and `EVIDENCE_UNAVAILABLE` are deliberately **distinct**: the
first says "I looked and do not know", the second says "I could not look".
Collapsing them is the defect class this fleet already has a name for —
unreadable looking clean.

`CLASSIFIED` admits only `AUTHORING`, `ENVIRONMENT` or `PROJECT`, always with
evidence. `UNKNOWN` is never a `CLASSIFIED` class; it is the `FailureKind` that
corresponds to `UNCLASSIFIED`.

**Precedence on a partial snapshot.** In this slice the outcome is
`EVIDENCE_UNAVAILABLE`; markers that were found are preserved as observations.
The absence of *optional* annotations does not by itself mean incompleteness.

**Rule conflict is not resolved by iteration order.** If the evidence yields
incompatible causes for one failure, the verdict is `UNCLASSIFIED` with the
ambiguity explained.

## 4. Evidence discipline

A class is established only together with a citation. A rule that cannot cite its
source does not establish a class — it contributes an observation. This makes an
invented cause **inexpressible in the type** rather than forbidden by agreement.

**A citation is necessary but not sufficient.** The type can forbid a class
without evidence; it cannot forbid a wrong inference *from* a cited line. Rule
soundness is therefore held by tests, including **similar messages with a
different cause** that must not produce the same class.

## 5. `FailureKind` extension

Today (`src/deployer/models.py`) the enum has two members, and
`CheckResult.enforce_failure_taxonomy` requires a `FAILED` result to carry one —
so "failed, cause not established" is **currently inexpressible**, which is why
the existing code falls through to `AUTHORING`. Overloading `None` is not
available: the invariant gives `None` the meaning "this check did not fail".

Two members are added:

- **`PROJECT`** — the evidence points to a defect in the code or tests of the
  project under verification. It is established on **positive evidence of a cause
  in the project**, never by the absence of `AUTHORING`/`ENVIRONMENT` markers. A
  failing test does not by itself prove a project defect nor a correct workflow:
  wrong dependencies or wrong invocation parameters produce the same symptom.
- **`UNKNOWN`** — the failure is established, the cause is not.

### 5.1 The two holes in `verify.py` are fixed in this slice

`todo://deployer/failure-classification-channel` describes **existing** behaviour
in the verification path, so closing it in the new classifier would close
nothing. Both sites are changed here, explicitly:

- **`verify.py:976-980`** — `_classify()` returns `AUTHORING` for anything without
  an `ENVIRONMENT` marker. An unknown failure now yields `UNKNOWN`.
- **`verify.py:1437-1444`** — exit 125/126 **with** a transport marker yields
  `ENVIRONMENT`, **anything else** yields `AUTHORING`. The absence of a transport
  marker does not by itself establish another class: without positive evidence the
  result is `UNKNOWN`, while known causes keep their justified classification.

Belonging to the verification path does not justify an automatic `AUTHORING`.
Tests assert the **correctness of the result** ("an unknown failure is
`UNKNOWN`"); they must not enshrine the unreachability of `PROJECT`.

### 5.2 Consumers of the enum, checked before extending

Verified 2026-09-21. ~25 references are **producers**
(`failure_kind=FailureKind.AUTHORING` at a concrete failure); a new member does
not affect them. Exactly four sites branch on the value:

| Site | What it does |
|---|---|
| `verify.py:980` | the fallthrough default — changed here |
| `verify.py:1443` | the 125/126 fallthrough (branch opens at 1437) — changed here |
| `verify.py:1270`, `verify.py:1382` | `is FailureKind.AUTHORING` branches |
| `models.py:425` | `environment_failures()`; there is no complementary `authoring_failures()`, so binary-ness is not baked into the aggregate |

`failure_kind` is serialized: `corpus/golden/golden.json` carries `failure_kinds`,
`bench.py:303` compares them, and corpus cases declare `expected_failure_kind`.
A new member therefore shows up as a **golden movement** — attributable, not
silent. That movement is part of acceptance (§7).

## 6. The polygon

A sandbox ref in this repository, with a dedicated `workflow_dispatch`-only
workflow.

- The workflow file lands on the default branch (required for dispatch — see
  §8), carries **no** `push`/`pull_request` triggers, and its job name differs
  from the required check context `test`.
- The deliberately failing content lives on the **experiment commit**, dispatched
  by ref.
- **Exclusion by the watcher is bound to the workflow path/ID and
  `workflow_dispatch`**, not to a displayed name.

### 6.1 Isolation, verified 2026-09-21

| Check | Result |
|---|---|
| Required checks | `test` and `governance / gate`; both rulesets target the **default branch only** |
| Merge gate on a red non-required check | `merge-pr.sh:530` admits `UNSTABLE` — the agent merge is not blocked |
| The "watcher" | not a service but a waiting discipline (`gh pr checks` + presence of expected checks); the only surface is a PR's checks |
| Fleet sensors reading run conclusions | one — robin `freshness.py`, pinned to `arch-evidence-freshness.yml` (line 26); this workflow is invisible to it |

**The one real gap.** Check runs attach to a **SHA**, not to a branch. If the
experiment SHA is the head of *any* open PR, its checks appear in that PR. The
condition is therefore checked **on the SHA before every dispatch**; giving the
experiment its own commit makes this hold by construction but does not replace
the check.

### 6.2 Controlled scenarios

Each scenario isolates **one** cause. The expected class is fixed **before** the
run and is not leaked to the diagnosis — not through the scenario name, not
through a comment.

- **`AUTHORING`** — a broken step in the authored artifact.
- **`ENVIRONMENT`** — a controlled unavailability of a dependency. A deliberately
  wrong registry address in the authored artifact is **not** used: it can equally
  mean `AUTHORING` and would be an ambiguous test.
- **`PROJECT`** — a deliberate defect in the project's code/test with an explicit
  assertion message, **plus a control run of the same artifact without the
  defect, which must pass**.

`EVIDENCE_UNAVAILABLE` and incomplete evidence are exercised by **substituting
the adapter**; they are not faked live. **Rule conflict is tested directly on a
snapshot in the pure classifier** — it needs no adapter substitution.

## 7. Acceptance

### 7.1 Offline — the primary proof, no tokens, no network

- one fixture per class: `AUTHORING`, `ENVIRONMENT`, `PROJECT`;
- `EVIDENCE_UNAVAILABLE` and incomplete snapshots via adapter substitution;
- rule conflict on a crafted snapshot in the pure classifier;
- adapter refusal on an unfinished run and on a successful run;
- **rule soundness**: similar messages with a different cause do not yield the
  same class;
- regression on both `verify.py` holes: an unknown failure is `UNKNOWN`; 125/126
  without a transport marker is `UNKNOWN`, not `AUTHORING`.

Offline tests are what protect against an "always `AUTHORING`" classifier. The
live runs strengthen acceptance; they are not the only way discrimination is
proven.

### 7.2 Live — minimum four dispatches, once

Three failures (`AUTHORING`, `ENVIRONMENT`, `PROJECT`) **plus the `PROJECT`
control run that must pass**. Kept as acceptance evidence: run URL, commit SHA,
failing job/step, logs. These become the fixtures of §7.1.

### 7.3 Paid benchmark — inside the scope of acceptance

Adding `UNKNOWN` moves the golden baseline. The order is fixed:

1. `bench run --author anthropic`;
2. **review the diff against the current golden and explain every class change**;
3. only then `promote`.

Refreshing the baseline does not by itself prove correctness.

## 8. Open items to verify before the acceptance run

- **Assumption, untested here:** GitHub requires a `workflow_dispatch` workflow to
  exist on the default branch to be dispatchable. If true, the workflow file lands
  on `master` (harmless: dispatch-only, name distinct from `test`) while the
  failing content lives on the experiment ref. Verified by the first dispatch,
  **before** anything is built on it.
