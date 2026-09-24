# Provenance of `docker build --check` fixtures

Controller ruling (2026-09-23): this machine has no Docker (Podman only) and
`ssh` to another host is not permitted for this task. The three files that the
brief asked to record live from a Docker >= Buildx 0.15 host (`parse-error.txt`,
`builder-unreachable.txt`, `lint-then-error.txt`) are therefore **not** live
recordings of `docker build --check`. Each is documented below with its real
source. The two synthetic lines must be replaced by real `docker build --check`
recordings from a Docker >= Buildx 0.15 host before this fixture set is treated
as a faithful sample of builder-check output.

## docs-lint.txt

- Source: verbatim from the Docker docs, "Build checks" page,
  https://docs.docker.com/build/checks/ (the `JSONArgsRecommended` example).
- Real recording, Docker's own documentation copy-paste; not run locally.
- Date captured into this repo: 2026-09-23.

## warning-prefixed-lint.txt

- Source: verbatim from the Docker docs, `docker buildx build` reference,
  https://docs.docker.com/reference/cli/docker/buildx/build/ (the
  `InvalidBaseImagePlatform` / `WARNING:`-prefixed example).
- Real recording, Docker's own documentation copy-paste; not run locally.
- Date captured into this repo: 2026-09-23.

## parse-error.txt

- Source: **real** BuildKit parse-error text, taken from an actual CI failure
  log, not a `--check` recording. Extracted with:
  `git show 9e89daa:docs/evidence/ci-failure-diagnosis/polygon-run-5/run-5.log-failed.txt`
  (this repo, commit `9e89daa`, `polygon-run-5` case).
- Extraction: took the lines after the `##[endgroup]` marker and before
  `##[error]`, and stripped the leading
  `build\tRun docker build --file ./Dockerfile .\t<timestamp> ` prefix GitHub
  Actions puts on each raw log line.
- Exit code: `1`, taken from the same log's
  `##[error]Process completed with exit code 1.` line; recorded as the file's
  first line, `# exit=1` (the reader test strips it).
- This is `docker build` output (no `--check`), not `docker build --check`
  output. Per the task brief this is acceptable here because BuildKit's parse
  error is a single frontend diagnostic line
  (`dockerfile parse error on line N: ...`) that is identical whether it
  surfaces through `docker build` or `docker build --check` — the frontend
  emits the same line either way. It also demonstrates the "parse diagnostic
  wins even with a `Dockerfile:1` excerpt block present" rule from the task
  brief: the file contains a `Dockerfile:1` / `>>> FROM ...` excerpt block
  ahead of the `ERROR: ... dockerfile parse error on line 1: ...` line, and
  the reader still returns row 2 (`state="error", line=1`) because the parse
  regex is checked over every line before any lint-block scanning happens.
- No live Docker >= Buildx 0.15 host was used for this file (real log source,
  not a recording of `--check`); nothing here is synthetic.
- Date extracted into this fixture: 2026-09-23.

## builder-unreachable.txt

- Source: **synthetic**. No Docker host was reachable to record a real
  `docker build --check` run against an unreachable daemon (no Docker on this
  machine, and ssh to another host was ruled out for this task).
- Content: `# exit=1` followed by the single synthetic line
  `ERROR: Cannot connect to the Docker daemon at tcp://127.0.0.1:1. Is the
  docker daemon running?`, modeled on Docker CLI's well-known daemon-
  unreachable message shape (as it would appear from
  `DOCKER_HOST=tcp://127.0.0.1:1 docker build --check ...`).
- **TODO: replace with a real recording** from a Docker >= Buildx 0.15 host
  by running the exact commands from the task brief's Step 1
  (`DOCKER_HOST=tcp://127.0.0.1:1 docker build --check -f Dockerfile.lint . >
  builder-unreachable.txt 2>&1; echo $?`), and record the real Docker/Buildx
  versions, host and date here.
- Date written (synthetic): 2026-09-23.

## lint-then-error.txt

- Source: **synthetic composite**, per the task brief's fallback instruction
  ("if `lint-then-error.txt` shows only lint ... make the mixed case instead
  by appending one real builder error line ... to a copy of `docs-lint.txt`").
  No Docker host was available to attempt the real recording at all (no
  Docker on this machine), so the fallback was used directly rather than
  after an observed real-recording failure.
- Content: `# exit=1`, then the full body of `docs-lint.txt` verbatim, then
  one appended synthetic error line:
  `ERROR: failed to solve: failed to read dockerfile: open Dockerfile: no
  such file or directory` (modeled on BuildKit's well-known
  "dockerfile not found" error shape).
- **TODO: replace with a real recording** from a Docker >= Buildx 0.15 host
  by running the exact commands from the task brief's Step 1
  (`docker build --check -f Dockerfile.linterr . > lint-then-error.txt
  2>&1; echo $?`), confirming it actually mixes a lint block with a builder
  error line (checks do not stat `COPY` sources, so this may need a
  different synthetic Dockerfile to force a real mixed case), and record the
  real Docker/Buildx versions, host and date here.
- Date written (synthetic): 2026-09-23.
