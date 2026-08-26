# TODO — deployer

Team-level open work for this repo. Implementation micro-steps live in
`docs/superpowers/plans/`; this file is the "what is left" view and is also read by
Robin's cross-mirror digest.

Syntax: `- [ ]` open, `- [x]` done. Optional plan-fields v2 inline tags —
`@owner:<principal>`, `@blocked_by:todo://<repo>/<id>`,
`@trigger:"<checkable condition>"`.
For `@owner:` the canonical values are `github:<login>`, `github-team:<org>/<team>`,
`repo:<manifest-key>`, and `TBD`; `<manifest-key>` is the canonical repository key in
the workspace manifest, and bare handle/role values are legacy. A missing tag means
"unknown" on purpose; inventing a trigger is worse than leaving it out.

`@id:<node-id>` is the canonical item identifier (ADR-ECO-005 PF-2B): lowercase grammar
`[a-z0-9][a-z0-9._-]{0,63}`, forming the URI `todo://deployer/<id>`. `@blocked_by`
transitionally accepts both legacy `<repo>#<slug>` and canonical `todo://<repo>/<id>`.

**Tags must sit on the checkbox line itself, and a quoted value must not wrap.** Robin
matches one line at a time (`robin-runtime/src/robin/plan_state.py:37,71`), so a tag on
a continuation line is invisible to it and a `@trigger:"…"` broken across lines parses
as the garbage value `"a`. Keep the first line self-contained and tagged; put the
elaboration on the lines below it.

## Status (2026-07-26)

Phases 1–4 of `docs/superpowers/specs/2026-07-21-bench-remote-verify-design.md` are
shipped and merged (PRs #10–#22). Bench: 11 synthetic corpus cases across three
artifact types (Dockerfile / compose.yaml / ci.yml), LLM-authored golden at success
rate 1.0 with every case converging in one iteration. Remote L2 over `ssh://` is
accepted on the homelab docker host. The post-Phase-4 direction is decided (below); the
seam audit is the item that unblocks the rest.

## Direction

**Decided 2026-07-26 (owner): ecosystem seams first, not more artifact types.** deployer
can already author and verify Docker / compose / CI artifacts; before widening the
surface to Helm or Terraform it is worth pinning down how those artifacts are actually
consumed. Widening first risks building the next format in isolation and discovering the
contract was wrong only after it has users. The order below is deliberate — each step is
tagged with what blocks it, so the sequencing survives without anyone re-reading this
paragraph.

- [ ] Seam audit of the Phase-4 artifacts @owner:github:andrei-shtanakov @id:seam-audit — producer/consumer pairs, @epic:eco.dark-factory
  artifact paths, status and error channels. Short and factual; its output is the
  shortlist the next item picks from. Covers the candidate consumers (Maestro, Robin,
  spec-runner) plus the two seams already listed below (arbiter, ATP)
- [ ] Define and implement the first production consumer seam for Phase-4 deploy artifacts @blocked_by:todo://deployer/seam-audit @id:first-consumer-seam @epic:eco.dark-factory
  — one executable vertical seam with a real consumer, not a survey. Spec first, per the
  usual rhythm. **Boundary check before committing to a consumer:** deployer may only
  build its own half. If the seam needs a change on the consumer's side, that is a
  handoff plus their PR, so prefer a consumer whose half already exists or whose owner
  has agreed — otherwise the "real consumer" is aspirational and the seam stalls half-built
- [ ] CI-failure diagnosis — read a failed run, author the fix @blocked_by:todo://deployer/first-consumer-seam @id:ci-failure-diagnosis @epic:eco.dark-factory
  — the founding doc's other half, and the next applied slice once a seam is proven
- [ ] Further artifact types: Helm, Terraform @blocked_by:todo://deployer/first-consumer-seam @id:further-artifact-types @epic:eco.dark-factory
  — deliberately last; wait until the extension contract is confirmed by a live consumer

## Research bench

Per the 2026-07-26 decision, bench work is now driven by regressions the seams surface
rather than pursued for its own sake. These items stay open and stay useful, but none of
them is the next thing to pick up.

- [ ] Agent-with-tools comparison arm @owner:github:andrei-shtanakov @id:agent-with-tools-arm — author with tool access vs @epic:eco.research-bench
  today's facts-only prompt over the same corpus; the spec declares this arm, it was
  never built
- [ ] Baseline arm vs the official uv Dockerfile @owner:github:andrei-shtanakov @id:baseline-uv-dockerfile-arm — measures what the @epic:eco.research-bench
  agent adds over the vendor template
- [ ] Second CI corpus case, then `actionlint_status` in bench compare @trigger:"a second ci corpus case exists" @id:second-ci-corpus-case @epic:eco.research-bench
- [ ] Install `hadolint` 2.12.0 on the bench machine @owner:github:andrei-shtanakov @id:install-hadolint @epic:eco.research-bench
  It is not on PATH here, so every golden so far is non-comparable on the hadolint axis
- [ ] Test-kind CI target and registry-push contour @owner:repo:deployer @id:ci-test-kind-target @epic:eco.research-bench
  The current `{"ci": {}}` intent authors build-image only; registry push is deliberately default-deny today
- [ ] Adopt the harness-eval discipline for the comparison arms above @owner:github:andrei-shtanakov @id:harness-eval-discipline — @epic:eco.research-bench
  one intervention per run, condition-blind grading, an explicit taxonomy of invalid
  runs (idea #4 of `../prograph-vault/authored/notes/2026-07-22-ideas-from-ai-repos-research.md`);
  without it an arm measures model noise as well as the intervention

## Verification hardening

- [ ] Run-config seam: per-target persistence of build/health timeouts @trigger:"an external target again needs a non-default --build-timeout" @id:run-config-timeout-persistence @epic:eco.dark-factory
  — the recording half shipped in #10; the operator still has to pass the flag
- [ ] L1 rule: a run/service intent must COPY the entrypoint and `package_dirs` @owner:repo:deployer @id:l1-copy-entrypoint-rule @epic:eco.dark-factory
- [ ] Unified pip-invocation parser — `pip --no-input install` still slips past the @owner:repo:deployer @id:unified-pip-parser @epic:eco.dark-factory
  payload-based poetry/pip install-strategy rules
- [ ] Compose follow-ups: logs guard on a failed `up`, per-dependency env check @owner:repo:deployer @id:compose-follow-ups @epic:eco.dark-factory
  (postgres case), shared pin-rule helper, per-section fences
- [ ] Failure classification channel: env failures carrying no markers ("exit status 1") @owner:repo:deployer @id:failure-classification-channel @epic:eco.dark-factory
  and exit-125 non-transport CLI errors both classify AUTHORING
- [ ] Poetry: list-valued optional-dependency constraints (`src/deployer/facts.py:248`) @owner:repo:deployer @id:poetry-list-optional-deps @epic:eco.dark-factory
- [ ] Bench run-dir litter on config error; document `docker system prune` for bench @owner:repo:deployer @id:bench-run-dir-litter @epic:eco.dark-factory
  hosts (failed builds leave containers plus dangling intermediates)
- [ ] podman-remote transport marker unverified @trigger:"a podman remote host is available" @id:podman-remote-marker @epic:eco.dark-factory
  — the homelab host runs docker only
- [ ] Stage-split / image-size signal in `bench compare` @trigger:"a golden regresses to a single-stage image" @id:bench-stage-split-signal @epic:eco.dark-factory
  (low urgency: the model now stage-splits unprompted)
- [ ] Small stuff, one PR: add E501 to ruff `extend-select`; COPY JSON-array form check; @owner:repo:deployer @id:small-stuff-bundle @epic:eco.dark-factory
  `healthcheck_path` validation; pids/cpu limits on container run; CLI `--no-docker`
  note wording; `no_progress` vs `budget_exhausted` coincidence; `DeployTarget.env`
  semantics at run stage (inert by design — decide whether to reject or document)

## Ecosystem seams

- [ ] arbiter policy gate in front of any mutating action the bench grows @blocked_by:todo://deployer/seam-audit @id:arbiter-policy-gate-seam @epic:eco.governance-plane
- [ ] ATP smoke-test of built artifacts as a verification level above L2 @blocked_by:todo://deployer/seam-audit @id:atp-smoke-test-seam @epic:eco.dark-factory
- [ ] Keep the MLOps seams of `docs/idea-mlops-layer.md` pluggable, not built @trigger:"a target needs eval hooks or promotion gates" @id:mlops-seams-pluggable @epic:eco.dark-factory
- [ ] Watch research-bench Stage B @trigger:"research-bench ships a stable VerificationProvider" @id:watch-research-bench-stage-b @epic:eco.dark-factory
  — `../_cowork_output/plans/2026-07-25-stage-b-provider-design.md` (approved 2026-07-25):
  its `VerificationProvider` protocol, append-only verdicts and fail-closed `0/1/2` exit
  contract cover the same ground as L1/L2 here. If it stabilises, vendor a pinned copy
  rather than growing a second shape

## Cross-repo hygiene

- [ ] Neighbour docs still describe deployer as "MVP / Dockerfile authoring" @owner:github:andrei-shtanakov @id:neighbour-docs-correction @epic:eco.ops
  — `../prograph-vault/authored/registry/registry.md` and the 2026-07-22 ideas note.
  Both are read-only from here: write the correction as a handoff note, do not edit

## Shipped

Merged work, plus the decisions that closed an open item without being code — those are
prefixed `Decision:` so the ledger does not imply shipped behaviour.

- [x] Drop the pilot-DAG branch-precondition task — #38, closed by #44 @owner:github:andrei-shtanakov @id:pilot-dag-drop-branch-precondition @epic:eco.ops
  The stopgap task guarded Mode 1 silently ignoring `branch_prefix` (inbox #34);
  `git.run_branch` (maestro#216 phase A) made isolation a runtime guarantee and
  #38 dropped the task. The box stayed open because the wait was tagged in the
  transitional `maestro#216` form, which the sensor does not wake — found by
  the 2026-08-26 waits-graph snapshot; #44 removed the orphaned task-0 comment
  and closes the item.
- [x] `.envrc` kept out of the build context — #40 @owner:repo:deployer @id:envrc-context-ignore
  `.env.*` never covered `.envrc` (fnmatch needs a dot after `env`), so direnv
  files reached the context — and with `--container-host` left the machine.
  Fixed with the literal `.envrc`, never a wildcard: the test pins two
  survivors, `envrc.py` against `*envrc*` and `.envrc.example` against
  `.envrc*` (the second added after #40's review caught that the first alone
  covered only the exotic slip).
  **The acceptance run of the Dark Factory contour** (dispatcher slice 0, run
  `01M0T5HA1PW0J0GWTCGMZVFWW0`, request issued from the UI). Unlike the first
  pass (#36, isolation FAILED), branch isolation here was a guarantee of the
  runtime: `git.run_branch` (maestro#216 phase A) created
  `pilot/envrc-context-ignore` from `master` before the run was published, and
  no git command was run by hand before the launch. `master` never moved.
  Red/green reproduced by hand against the red commit; 476 passed.
- [x] Token-boundary entrypoint match — #36 @owner:repo:deployer @id:entrypoint-token-boundary-match
  `_check_entrypoint_in_command` split the haystack on characters that cannot appear
  in a filename or a `[project.scripts]` name and now requires whole-token equality,
  so `app` no longer passes against `CMD ["python", "application.py"]`; the docstring
  was rewritten with the behaviour rather than left describing the substring rule.
  **First backlog item carried end-to-end by the Dark Factory contour** (dispatcher
  slice 0, run `01M0SE57CPBY1GEQ8MGVY5B6GX`, request issued from the UI). Two results,
  deliberately kept apart: task execution and verification PASSED (red/green
  reproduced by hand, 475 passed); branch isolation FAILED — Mode-1 maestro committed
  straight to `master` and the commits were recovered by hand, `master` returned to
  `f12c15f`. `auto_push: false`, so the protected remote was never touched. That
  failure, not this fix, is the pilot's most valuable output — upstream contract in
  maestro#216, precondition in #35.
- [x] Pilot DAG hardening (inbox deployer#34, slug `pilot-dag-branch-precondition`, #35):
  first task verifies the branch precondition (clean checkout on `pilot/<slug>`,
  never `master`/`main`) instead of relying on `branch_prefix`, which Mode-1 maestro
  silently ignores (upstream: maestro#216); `branch_prefix` comment made honest;
  `logs/` added to `.gitignore` (maestro writes run logs into the worktree)
- [x] Decision (2026-07-26, owner): post-Phase-4 direction is ecosystem seams, ahead of
  further artifact types; closed the "choose the direction" item and replaced it with the
  four sequenced items now under "Direction" (#24). Rationale lives there
- [x] Plan file: `TODO.md` created, `CLAUDE.md` caught up with Phase 4 (#23)
- [x] MVP: facts → prompt → authoring loop → L1/L2 verification (#1)
- [x] Facts v2: requirements.txt, hints table, `system_packages` (#2)
- [x] Timeout forwarding (#3), CLI hardening and exit-code taxonomy (#4)
- [x] Phase 1/1.5: `ContainerRuntime`, remote `ssh://` L2, run metadata (#10),
  remote acceptance on the homelab host (#15)
- [x] Phase 2: corpus + `bench run`/`bench verify` (#11)
- [x] Phase 3: golden store, `bench promote`/`bench compare` (#12)
- [x] Phase 4a entrypoint fact (#13), 4b run-completes (#16), extras + source layout
  (#17), entrypoint intent (#18), QoL bundle (#19)
- [x] Phase 4b-5 Poetry (#20), 4c compose (#21), 4d CI workflow (#22)
