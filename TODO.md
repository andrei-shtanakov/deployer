# TODO — deployer

Team-level open work for this repo. Implementation micro-steps live in
`docs/superpowers/plans/`; this file is the "what is left" view and is also read by
Robin's cross-mirror digest.

Syntax: `- [ ]` open, `- [x]` done. Optional inline tags — `@owner:<handle>`,
`@blocked_by:<repo>#<slug>`, `@trigger:"<checkable condition>"`. A missing tag means
"unknown" on purpose; inventing a trigger is worse than leaving it out (format:
`../_cowork_output/2026-07-26-plan-fields-and-todo-coverage-handoff.md`).

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
accepted on the homelab docker host. There is no open roadmap past Phase 4 — the first
item below is the one that unblocks the rest.

## Direction

- [ ] Choose the post-Phase-4 direction and commit a spec for it @owner:andrei —
  candidates: (a) further artifact types from the founding doc (Helm, Terraform),
  (b) CI-failure diagnosis (the founding doc's other half: read a failed run, author
  the fix), (c) ecosystem seams (arbiter gate / ATP smoke / Maestro workstream),
  (d) deepen the bench itself via the comparison arms below. Everything under
  "Ecosystem seams" hangs on this choice.

## Research bench

- [ ] Agent-with-tools comparison arm @owner:andrei — author with tool access vs
  today's facts-only prompt over the same corpus; the spec declares this arm, it was
  never built
- [ ] Baseline arm vs the official uv Dockerfile @owner:andrei — measures what the
  agent adds over the vendor template
- [ ] Second CI corpus case, then `actionlint_status` in bench compare @trigger:"a second ci corpus case exists"
- [ ] Install `hadolint` 2.12.0 on the bench machine — it is not on PATH here, so every
  golden so far is non-comparable on the hadolint axis
- [ ] Test-kind CI target (current `{"ci": {}}` authors build-image only) and the
  registry-push contour (deliberately default-deny today)
- [ ] Adopt the harness-eval discipline for the comparison arms above @owner:andrei —
  one intervention per run, condition-blind grading, an explicit taxonomy of invalid
  runs (idea #4 of `../prograph-vault/authored/notes/2026-07-22-ideas-from-ai-repos-research.md`);
  without it an arm measures model noise as well as the intervention

## Verification hardening

- [ ] Run-config seam: per-target persistence of build/health timeouts @trigger:"an external target again needs a non-default --build-timeout"
  — the recording half shipped in #10; the operator still has to pass the flag
- [ ] Token-boundary entrypoint match — L1 `entrypoint_in_command` compares substrings,
  so short names like `app` false-pass
- [ ] L1 rule: a run/service intent must COPY the entrypoint and `package_dirs`
- [ ] Unified pip-invocation parser — `pip --no-input install` still slips past the
  payload-based poetry/pip install-strategy rules
- [ ] Compose follow-ups: logs guard on a failed `up`, per-dependency env check
  (postgres case), shared pin-rule helper, per-section fences
- [ ] Failure classification channel: env failures carrying no markers ("exit status 1")
  and exit-125 non-transport CLI errors both classify AUTHORING
- [ ] `.envrc` missing from `CONTEXT_IGNORE` (`src/deployer/verify.py:57`) — direnv
  files can carry secrets into the build context
- [ ] Poetry: list-valued optional-dependency constraints (`src/deployer/facts.py:248`)
- [ ] Bench run-dir litter on config error; document `docker system prune` for bench
  hosts (failed builds leave containers plus dangling intermediates)
- [ ] podman-remote transport marker unverified @trigger:"a podman remote host is available"
  — the homelab host runs docker only
- [ ] Stage-split / image-size signal in `bench compare` @trigger:"a golden regresses to a single-stage image"
  (low urgency: the model now stage-splits unprompted)
- [ ] Small stuff, one PR: add E501 to ruff `extend-select`; COPY JSON-array form check;
  `healthcheck_path` validation; pids/cpu limits on container run; CLI `--no-docker`
  note wording; `no_progress` vs `budget_exhausted` coincidence; `DeployTarget.env`
  semantics at run stage (inert by design — decide whether to reject or document)

## Ecosystem seams

- [ ] arbiter policy gate in front of any mutating action the bench grows @blocked_by:deployer#post-phase-4-direction
- [ ] ATP smoke-test of built artifacts as a verification level above L2 @blocked_by:deployer#post-phase-4-direction
- [ ] Keep the MLOps seams of `docs/idea-mlops-layer.md` pluggable, not built @trigger:"a target needs eval hooks or promotion gates"
- [ ] Watch research-bench Stage B @trigger:"research-bench ships a stable VerificationProvider"
  — `../_cowork_output/plans/2026-07-25-stage-b-provider-design.md` (approved 2026-07-25):
  its `VerificationProvider` protocol, append-only verdicts and fail-closed `0/1/2` exit
  contract cover the same ground as L1/L2 here. If it stabilises, vendor a pinned copy
  rather than growing a second shape

## Cross-repo hygiene

- [ ] Neighbour docs still describe deployer as "MVP / Dockerfile authoring" @owner:andrei
  — `../prograph-vault/authored/registry/registry.md` and the 2026-07-22 ideas note.
  Both are read-only from here: write the correction as a handoff note, do not edit

## Shipped

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
