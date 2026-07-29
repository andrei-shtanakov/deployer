# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`deployer` is a deploy-oriented AI-agent subproject within the `all_ai_orchestrators` lab ecosystem (ATP, arbiter, proctor, Maestro). It is now a working research bench: the `deployer` CLI authors deploy artifacts from deterministic project facts + a declarative `deploy_target`, then verifies them through static checks (L1) and optional sandboxed container build/run/healthcheck loops (L2).

Three artifact types are authored today, selected by the target's intent and returned by the model in one text response split on line-anchored sentinels (`=== Dockerfile ===`, `=== compose.yaml ===`, `=== ci.yml ===`): the Dockerfile always; a `compose.yaml` when the target declares pinned infra `dependencies`; a build-only `.github/workflows/ci.yml` when it declares `{"ci": {}}`.

Where the intent lives:

- `TODO.md` — team-level open work and the shipped ledger. **Read it first**; it is the only "what is left" view (roadmap phases 1–4 are complete, so the next direction is an open decision).
- `docs/superpowers/specs/2026-07-21-bench-remote-verify-design.md` — the bench/remote roadmap spec (phases 1–4, all shipped).
- `docs/idea-deployer-subproject.md` — the founding design doc. Read it before any feature work.
- `docs/idea-mlops-layer.md` — related direction: MLOps seams (eval hooks, promotion gates, `deploy_target` intent) that should stay pluggable.

Per-feature specs and plans live in `docs/superpowers/specs/` and `docs/superpowers/plans/`; every shipped feature has a pair there.

## Core design constraint: authoring ≠ execution

This is the non-negotiable architectural rule from the design docs:

- **The agent authors artifacts** — generates/fixes Dockerfiles, CI workflows, Helm charts, Terraform from project + `deploy_target` intent; diagnoses failed CI. Output is files in a PR.
- **Execution stays deterministic** — real CI/IaC applies the artifacts. MCP is used for read/plan/dry-run only; mutating actions are gated by arbiter policy plus human approval on prod. Never implement "agent runs `terraform apply` autonomously."

Ecosystem roles to reuse, not reinvent: arbiter = policy/guardrail gate for deploy actions; ATP = validation/smoke-test of built artifacts; Maestro = the deploy agent can be a workstream/spawner type.

## Environment and commands

Python 3.12+, managed exclusively with `uv` (never pip):

- Run authoring: `uv run deployer author <project-path> [--target target.json] [--no-docker]`
- Run verification: `uv run deployer verify <project-path>`
- Bench: `uv run deployer bench run|verify|promote|compare` — see README. `bench run` defaults to the offline `fixture` author; `--author anthropic` spends money and must be chosen explicitly.
- Runtime flags on `author`/`verify`/`bench run`: `--container-tool docker|podman`, `--container-host ssh://user@host` (ssh only), `--build-timeout`, `--health-timeout`.
- Add a dependency: `uv add package`
- Tests: `uv run pytest` (unit only — `addopts` excludes the `docker` and `llm` markers), `uv run pytest -m docker` for sandboxed container tests, `uv run pytest -m llm` for the token-spending ones; single test: `uv run pytest path/to/test.py::test_name`
- Format: `uv run ruff format .`
- Lint: `uv run ruff check . --fix`
- Type check: `uv run pyrefly check` after every change (run `uv run pyrefly init` once if not yet configured)

The shipped package lives under `src/deployer/`: `facts` (project scan), `models` (`DeployTarget`, reports), `hints` (curated apt table), `llm` + `author` (prompt and the authoring loop), `artifacts` (sentinel parse/render), `verify` (L1/L2), `runtime` (`ContainerRuntime`, the single container-subprocess chokepoint), `bench`, `cli`. Tests live under `tests/`; container- and LLM-dependent checks are marker-gated. The corpus and the committed golden baseline live under `corpus/`; raw bench runs land in gitignored `.deployer-runs/`.

The Anthropic API key comes from `./.env` (gitignored; real env vars win). Runtime flags are never read from `.env`.

## Working rhythm (used for every feature so far)

brainstorming → spec committed on the feature branch → the **user** runs an external spec review that lands as `../_cowork_output/deployer-<topic>-spec-review-<date>.md` (apply the valid findings before planning — every spec so far got real fixes) → writing-plans → subagent-driven development with per-task reviews and a final whole-branch review → LLM acceptance (`bench run --author anthropic` → `promote` → `compare`, diff reviewed before promoting) → PR → GitHub Copilot round → the user merges. The SDD ledger is `.superpowers/sdd/progress.md`.

Verify subagent acceptance claims against the plan's expected values yourself; a task agent has marked a spot-check PASS while its own output contradicted the plan.

## Repo scope & boundaries

- **Этот репо:** `deployer` — git-корень `all_ai_orchestrators/deployer/`, remote `git@github.com:andrei-shtanakov/deployer.git`.
- **Соседи (READ-ONLY reference):** `../arbiter/`, `../atp-platform/`, `../dispatcher/`, `../maestro/`, `../libretto/`, `../proctor/`, `../prograph/`, `../prograph-vault/`, `../robin-runtime/`, `../robin-toolkit/`, `../spec-runner/`, `../spec-runner-vscode/`, `../steward/` — их код не редактировать.
- Нужна правка у соседа → **стоп**: запиши handoff в `../prograph-vault/authored/notes/`
  (кросс-проектное) или `../_cowork_output/` (черновик), не трогай его файлы.
- Кросс-репные контракты — **вендорить пиненой копией внутрь**, не ссылаться наружу.
- Полное правило (SSOT): `../prograph-vault/authored/rules/repo-boundaries.md`.

## Git workflow (у репо есть remote)

- Ветка `<type>/<slug>` → push → `gh pr create`. **Прямые коммиты в `master` запрещены.**
- После открытия PR — прочитать ревью **GitHub Copilot**: валидные замечания исправлять
  новыми коммитами в ту же ветку; невалидные — ответить с обоснованием, **не применять
  вслепую**; итерировать, пока не останется открытых замечаний.
- **Не мержить.** Мерж делает пользователь.
- После мержа пользователем: `git switch master && git pull --ff-only`, затем удалить
  влитую ветку (`git branch -d <branch>`) и `git fetch --prune`; убрать прочие влитые ветки.
- Никогда не делать force-push в общие ветки; не трогать другие репо (см. scope выше).
- В PR, который закрывает пункт из `TODO.md`, переносить этот пункт в `## Shipped`
  и добавлять новые открытые пункты, если работа их породила. Файл читает
  кросс-репный дайджест — протухший `TODO.md` хуже отсутствующего. Синтаксис пунктов —
  чекбоксы `- [ ]`/`- [x]` плюс опциональные инлайн-теги `@owner:`/`@blocked_by:<repo>#<slug>`/
  `@trigger:"<проверяемое условие>"`; отсутствие тега значит «неизвестно» и это честнее
  выдуманного (формат: `../_cowork_output/2026-07-26-plan-fields-and-todo-coverage-handoff.md`,
  парсер Robin умеет теги с robin-runtime#27). **Тег обязан стоять на самой строке
  чекбокса, а значение в кавычках не переносится:** Robin матчит построчно
  (`robin-runtime/src/robin/plan_state.py:37,71`), поэтому тег на строке продолжения он
  не видит вовсе, а разорванный `@trigger:"…"` парсится в мусорное значение `"a`.
  Ради этого длина строки пункта важнее 88 символов.
- Полное правило (SSOT): `../prograph-vault/authored/rules/git-workflow.md`.

## Входящие запросы (inbox)

В начале работы проверь входящие: `gh issue list --label inbox --state open`.
Issue с лейблом `inbox` — запрос от соседнего репо, ещё **не** пункт плана.
Принять = завести пункт в `TODO.md` с указанным `slug:`; принял под другим
именем — поправь `slug:` в теле issue.
Отказать = `gh issue close --reason "not planned"`.
Нужна работа в соседнем репо — не редактируй его: заведи там issue
(`slug:` + `from:` + проза). Правило: ADR-ECO-006.
