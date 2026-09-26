# Recordings — provenance

These are real builds, recorded to back the template rows of `ci-fix-authoring`
(`docs/superpowers/specs/2026-09-25-ci-fix-authoring-design.md` §6.3, §9, §11 stage 3).
The owner permitted real runs on 2026-09-25. The data is for the owner to review, and
only the owner merges it.

## `local/` — Podman builds (L-recordings)

- **When and where:** 2026-09-25, on the author's machine: Podman 5.7.0, Buildah 1.42.0,
  a `podman-machine` VM (linux/arm64) with a Darwin/arm64 client. Each case's
  `environment.json` gives the exact values and a UTC timestamp.
- **How:** `tests/fixtures/recordings/record_local.py`. It builds through R's own
  `deployer.reproduce.build.run_build`, so the argv is the one production uses:
  `podman build --file … --tag … --force-rm <context>`. There is no `--no-cache` and
  nothing was pruned.
- **Kept verbatim:** `build.stdout`, `build.stderr` and `build.exit`, exactly as
  `run_build` returned them (text decoded with `errors="replace"`, as production decodes
  it). Nothing was trimmed, edited or redacted.
- **Written by the recorder, not the builder:** `argv.json`, where the absolute scratch
  path of the context is written as `<context>`, and `environment.json`.
- **The case question:** `checks.json`, the check kinds and corrected texts that each
  case is asked.
- **`expected.json`** holds the outcomes that the current hypothesis matchers
  (`deployer.fix.templates`, not yet enabled) give when replayed on these recordings,
  next to the hypothesis the plan wrote down before recording (`hypothesis`, `agrees`).
  It is an observation for review, not a claim that the matchers are right.
- **Trees are derived, not historical.** Each `tree/` holds the project files of
  `polygon/run-5` (commit `937d465`: `pyproject.toml`, `uv.lock`, `src/`, `tests/`), the
  case's `Dockerfile`, and `docs/guide/setup.md` where the case copies it. In
  `l1-copy-cold` that file carries a random nonce so the COPY layer misses the cache.
  `l2-copy-warm` is the same tree built again right after `l1`, before either tag was
  removed, so its COPY reads the cache.
- **Keys:** none are involved; nothing here is signed.

| Case | Build | What it shows |
|---|---|---|
| `l1-copy-cold` | exit 0 | corrected COPY built fresh: `STEP 7/12: COPY …`, `--> <id>`, then `STEP 8/12` |
| `l2-copy-warm` | exit 0 | the same COPY from cache: `--> Using cache <sha>`, `--> <id>`, then the next `STEP` |
| `l3-from-run5` | exit 0 | the corrected FROM `FROM python:3.12-slim AS extra` parsed and built |
| `l4-from-bad-later` | exit 125 | a bad FROM in the **second** stage fails before any `STEP` line (stdout empty). Stage `a` is unused and skipped, so this does **not** tell a whole-file check from a per-stage one — `l8`/`l9` do |
| `l5-stages-same-image` | exit 0 | Podman **skips a stage nothing depends on**: only `[2/2]` steps appear; multi-stage step lines carry a `[i/n] ` prefix |
| `l6-copy-later-failure` | exit 1 | the corrected COPY built, then `RUN false` failed (`Error: building at STEP "RUN false"`) |
| `l7-stages-both-built` | exit 0 | two stages both build the identical COPY: `[1/2] STEP 3/3: COPY …` and `[2/2] STEP 3/4: COPY …` |
| `l8-from-bad-after-built-stage` | exit 125 | stage `a` **builds** (`[1/2] STEP …`, `--> <id>`), then the bad FROM of the stage that needs it fails: the argument count is checked per stage, when the stage starts |
| `l9-from-bad-in-skipped-stage` | exit 0 | the bad FROM sits in a stage nothing depends on: the stage is skipped, **never checked**, and the build succeeds with the defect still in the file |

## What disagrees with the hypothesis

**The local FROM hypothesis is refuted** (`l8`, `l9`; source reading in `docs/fix-buildah-from-parse.md`, Buildah v1.42.0 / imagebuilder v1.2.19). Podman does not check every FROM's argument count before building: it checks each stage's FROM when that stage starts, and skips unused stages entirely. So "a build-stage step exists and no parse error" (§6.3) does not prove the corrected FROM is valid — `l9` builds with exit 0 while the bad FROM is still in the file. The hypothesis matcher happens to answer `not_confirmed` on `l9` only because it does not read the `[i/n] ` prefix ("no build-stage step"); on a single-stage file it would be right, on this file it is right by accident. The local FROM row stays disabled; what evidence could back it is a design decision for the owner.


The hypothesis matchers read only `STEP k/n: …` lines without a prefix. On the
multi-stage recordings (`l5`, `l7`) they therefore report `not_confirmed` ("corrected
step line absent" / "no build-stage step"), not the expected `binding_ambiguous` /
`passed`. This is the conservative direction: nothing is falsely confirmed. But a
corrected instruction in a multi-stage Dockerfile could never be confirmed locally. Any
matcher change for the `[i/n] ` form is code and goes through the ordinary review (§9).
These recordings stay as they are.

Checksums: `CHECKSUMS.sha256` covers every file except itself and the recorder.

## `ci/` — polygon runs on GitHub Actions (C-recordings)

- **When and where:** 2026-09-25, on the polygon of `andrei-shtanakov/deployer`. Each
  case is an **orphan** commit on its own branch, `polygon/fix-c-<case>`, like the
  `polygon/run-*` branches. The branch holds:
  - the project files of `polygon/run-5` (`937d465`);
  - the case's `Dockerfile` and added files;
  - a `workflow_dispatch`-only `.github/workflows/diagnosis-polygon.yml`. Its one
    `ubuntu-24.04` job runs the pinned `actions/checkout`, then
    `docker build --file ./Dockerfile .`, the build line of `polygon/run-1`.

  `c6` has that build step twice. The branches stay on origin as evidence.
- **How:** `tests/fixtures/recordings/record_ci.py`. For each case it:
  - pushes the branch, refusing a branch that already exists;
  - checks that the SHA is not the head of any open PR (diagnosis spec §6.3);
  - runs `gh workflow run`;
  - reads the run through `deployer.forge.list_runs_for_sha` and `read_attempt`.

  These reads go through a runner that records **every `gh api` call verbatim** into
  `gh-calls.json`: the argv, plus stdout or the error. A replay serves those bytes back
  to forge's own parsing.
- **Written by the recorder:** `environment.json` (repo, branch, SHA, run id and URL,
  workflow path, UTC time) and `checks.json` (the questions: kind, corrected text,
  Dockerfile line span).
- **`expected.json`:** for each attempt and job, the outcome of the current hypothesis CI
  matchers (not yet enabled) over forge's reading, next to the plan's hypothesis.

| Case | Run | What it shows |
|---|---|---|
| `c1-copy-done` | 36147902215, success | `#14 [stage-0 7/9] COPY docs/guide/setup.md ./setup.md` then `#14 DONE`; COPY `passed` |
| `c2-copy-rerun` | the same run, re-run (`gh run rerun`) | Attempt 2 was read, but the read of its **job log failed with a transient `TLS handshake timeout`**. It was recorded as it happened, so this is a real case of an unavailable log. |
| `c2b-copy-rerun-reread` | the same run, read again (no new run) | both attempts read; attempt 2's job has a new job id and passes the same way |
| `c3-from-parsed` | 36148249976, success | `#8 [extra 1/8] FROM docker.io/library/python:3.12-slim@sha256:…` — BuildKit prints the normalised reference; FROM `passed` |
| `c4-copy-later-failure` | 36148351176, failure | the COPY is `#14 … DONE`, then `RUN false` fails. **BuildKit pads step numbers** once there are ten or more steps (`[stage-0  7/10]`, two spaces), and the hypothesis matcher misses that form: COPY `not_confirmed` |
| `c5-copy-recurred` | 36148493985, failure | the corrected source is still absent: `#12 [stage-0 7/9] COPY …` fails at the corrected span (the recurrence form) |
| `c6-copy-cached` | 36148650524, success | two builds in one job: the second shows `#11 [stage-0 7/9] COPY …` / `#11 CACHED`. The log is `binding_ambiguous` (several builds) |
| `c7-copy-done-padded` | 36149311829, success | Meant to show padding, but `LABEL` is **not** a build step, so there are still 9 steps and no padding. Kept as recorded. |
| `c8-from-bad-in-skipped-stage` | 36149389171, failure | A bad FROM in a stage nothing depends on fails **before any stage**: `dockerfile parse error on line 1`. BuildKit parses the whole file first, unlike Podman (`l9`), as §6.3/§7.3 assume. |
| `c9-copy-done-padded-run` | 36149545526, success | 10 steps, a passing padded `#14 [stage-0  7/10] COPY …` / `#14 DONE`. The hypothesis matcher misses it: `not_confirmed` |

### What disagrees with the hypothesis (CI)

- **Padded step numbers** (`c4`, `c9`): `_BK_STAGE_RE` accepts one space between the
  stage name and `k/n`. BuildKit right-aligns `k` to the width of `n`. A passing COPY in
  any build with ten or more steps reads as "corrected step header absent". This is the
  conservative direction, but most real Dockerfiles would never confirm. A matcher change
  is code, for review (§9).
- Everything else agrees. The CI FROM hypothesis (whole-file parse) holds on BuildKit
  (`c8`).
