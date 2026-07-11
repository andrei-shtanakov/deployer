# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`deployer` is a deploy-oriented AI-agent subproject within the `all_ai_orchestrators` lab ecosystem (ATP, arbiter, proctor-a, Maestro). It is currently a bare scaffold (`main.py` is a hello-world); the design intent lives in `docs/`:

- `docs/idea-deployer-subproject.md` — the founding design doc. Read it before any feature work.
- `docs/idea-mlops-layer.md` — related direction: MLOps seams (eval hooks, promotion gates, `deploy_target` intent) that should stay pluggable.

## Core design constraint: authoring ≠ execution

This is the non-negotiable architectural rule from the design docs:

- **The agent authors artifacts** — generates/fixes Dockerfiles, CI workflows, Helm charts, Terraform from project + `deploy_target` intent; diagnoses failed CI. Output is files in a PR.
- **Execution stays deterministic** — real CI/IaC applies the artifacts. MCP is used for read/plan/dry-run only; mutating actions are gated by arbiter policy plus human approval on prod. Never implement "agent runs `terraform apply` autonomously."

Ecosystem roles to reuse, not reinvent: arbiter = policy/guardrail gate for deploy actions; ATP = validation/smoke-test of built artifacts; Maestro = the deploy agent can be a workstream/spawner type.

## Environment and commands

Python 3.12+, managed exclusively with `uv` (never pip):

- Run: `uv run main.py`
- Add a dependency: `uv add package`
- Tests: `uv run pytest` (single test: `uv run pytest path/to/test.py::test_name`); async tests use anyio, not asyncio
- Format: `uv run ruff format .`
- Lint: `uv run ruff check . --fix`
- Type check: `pyrefly check` after every change (run `pyrefly init` once if not yet configured)

There are no tests, lint config, or dependencies yet — set them up alongside the first real code (pydantic is the expected modeling library per the parent lab conventions).

## Repo scope & boundaries

- **Этот репо:** `deployer` — git-корень `all_ai_orchestrators/deployer/`, remote `git@github.com:andrei-shtanakov/deployer.git`.
- **Соседи (READ-ONLY reference):** `../arbiter/`, `../atp-platform/`, `../dispatcher/`, `../Maestro/`, `../open-prose/`, `../proctor/`, `../prograph/`, `../prograph-vault/`, `../robin-runtime/`, `../robin-toolkit/`, `../spec-runner/`, `../spec-runner-vscode/`, `../steward/` — их код не редактировать.
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
- Полное правило (SSOT): `../prograph-vault/authored/rules/git-workflow.md`.
