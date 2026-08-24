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

- [ ] Seam audit of the Phase-4 artifacts @owner:github:andrei-shtanakov @id:seam-audit — producer/consumer pairs,
  artifact paths, status and error channels. Short and factual; its output is the
  shortlist the next item picks from. Covers the candidate consumers (Maestro, Robin,
  spec-runner) plus the two seams already listed below (arbiter, ATP)
- [ ] Define and implement the first production consumer seam for Phase-4 deploy artifacts @blocked_by:todo://deployer/seam-audit @id:first-consumer-seam
  — one executable vertical seam with a real consumer, not a survey. Spec first, per the
  usual rhythm. **Boundary check before committing to a consumer:** deployer may only
  build its own half. If the seam needs a change on the consumer's side, that is a
  handoff plus their PR, so prefer a consumer whose half already exists or whose owner
  has agreed — otherwise the "real consumer" is aspirational and the seam stalls half-built
- [ ] CI-failure diagnosis — read a failed run, author the fix @blocked_by:todo://deployer/first-consumer-seam @id:ci-failure-diagnosis
  — the founding doc's other half, and the next applied slice once a seam is proven
- [ ] Further artifact types: Helm, Terraform @blocked_by:todo://deployer/first-consumer-seam @id:further-artifact-types
  — deliberately last; wait until the extension contract is confirmed by a live consumer

## Research bench

Per the 2026-07-26 decision, bench work is now driven by regressions the seams surface
rather than pursued for its own sake. These items stay open and stay useful, but none of
them is the next thing to pick up.

- [ ] Agent-with-tools comparison arm @owner:github:andrei-shtanakov @id:agent-with-tools-arm — author with tool access vs
  today's facts-only prompt over the same corpus; the spec declares this arm, it was
  never built
- [ ] Baseline arm vs the official uv Dockerfile @owner:github:andrei-shtanakov @id:baseline-uv-dockerfile-arm — measures what the
  agent adds over the vendor template
- [ ] Second CI corpus case, then `actionlint_status` in bench compare @trigger:"a second ci corpus case exists" @id:second-ci-corpus-case
- [ ] Install `hadolint` 2.12.0 on the bench machine @owner:github:andrei-shtanakov @id:install-hadolint
  It is not on PATH here, so every golden so far is non-comparable on the hadolint axis
- [ ] Test-kind CI target and registry-push contour @owner:repo:deployer @id:ci-test-kind-target
  The current `{"ci": {}}` intent authors build-image only; registry push is deliberately default-deny today
- [ ] Adopt the harness-eval discipline for the comparison arms above @owner:github:andrei-shtanakov @id:harness-eval-discipline —
  one intervention per run, condition-blind grading, an explicit taxonomy of invalid
  runs (idea #4 of `../prograph-vault/authored/notes/2026-07-22-ideas-from-ai-repos-research.md`);
  without it an arm measures model noise as well as the intervention

## Verification hardening

- [ ] Run-config seam: per-target persistence of build/health timeouts @trigger:"an external target again needs a non-default --build-timeout" @id:run-config-timeout-persistence
  — the recording half shipped in #10; the operator still has to pass the flag
- [ ] Token-boundary entrypoint match — L1 `entrypoint_in_command` compares substrings, @owner:repo:deployer @id:entrypoint-token-boundary-match
  so short names like `app` false-pass — branch: pilot/entrypoint-token-boundary-match
- [ ] L1 rule: a run/service intent must COPY the entrypoint and `package_dirs` @owner:repo:deployer @id:l1-copy-entrypoint-rule
- [ ] Unified pip-invocation parser — `pip --no-input install` still slips past the @owner:repo:deployer @id:unified-pip-parser
  payload-based poetry/pip install-strategy rules
- [ ] Compose follow-ups: logs guard on a failed `up`, per-dependency env check @owner:repo:deployer @id:compose-follow-ups
  (postgres case), shared pin-rule helper, per-section fences
- [ ] Failure classification channel: env failures carrying no markers ("exit status 1") @owner:repo:deployer @id:failure-classification-channel
  and exit-125 non-transport CLI errors both classify AUTHORING
- [ ] `.envrc` missing from `CONTEXT_IGNORE` (`src/deployer/verify.py:57`) — direnv @owner:repo:deployer @id:envrc-context-ignore
  files can carry secrets into the build context
- [ ] Poetry: list-valued optional-dependency constraints (`src/deployer/facts.py:248`) @owner:repo:deployer @id:poetry-list-optional-deps
- [ ] Bench run-dir litter on config error; document `docker system prune` for bench @owner:repo:deployer @id:bench-run-dir-litter
  hosts (failed builds leave containers plus dangling intermediates)
- [ ] podman-remote transport marker unverified @trigger:"a podman remote host is available" @id:podman-remote-marker
  — the homelab host runs docker only
- [ ] Stage-split / image-size signal in `bench compare` @trigger:"a golden regresses to a single-stage image" @id:bench-stage-split-signal
  (low urgency: the model now stage-splits unprompted)
- [ ] Small stuff, one PR: add E501 to ruff `extend-select`; COPY JSON-array form check; @owner:repo:deployer @id:small-stuff-bundle
  `healthcheck_path` validation; pids/cpu limits on container run; CLI `--no-docker`
  note wording; `no_progress` vs `budget_exhausted` coincidence; `DeployTarget.env`
  semantics at run stage (inert by design — decide whether to reject or document)

## Ecosystem seams

- [ ] arbiter policy gate in front of any mutating action the bench grows @blocked_by:todo://deployer/seam-audit @id:arbiter-policy-gate-seam
- [ ] ATP smoke-test of built artifacts as a verification level above L2 @blocked_by:todo://deployer/seam-audit @id:atp-smoke-test-seam
- [ ] Keep the MLOps seams of `docs/idea-mlops-layer.md` pluggable, not built @trigger:"a target needs eval hooks or promotion gates" @id:mlops-seams-pluggable
- [ ] Watch research-bench Stage B @trigger:"research-bench ships a stable VerificationProvider" @id:watch-research-bench-stage-b
  — `../_cowork_output/plans/2026-07-25-stage-b-provider-design.md` (approved 2026-07-25):
  its `VerificationProvider` protocol, append-only verdicts and fail-closed `0/1/2` exit
  contract cover the same ground as L1/L2 here. If it stabilises, vendor a pinned copy
  rather than growing a second shape

## Cross-repo hygiene

- [ ] Neighbour docs still describe deployer as "MVP / Dockerfile authoring" @owner:github:andrei-shtanakov @id:neighbour-docs-correction
  — `../prograph-vault/authored/registry/registry.md` and the 2026-07-22 ideas note.
  Both are read-only from here: write the correction as a handoff note, do not edit

## Shipped

Merged work, plus the decisions that closed an open item without being code — those are
prefixed `Decision:` so the ledger does not imply shipped behaviour.

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
