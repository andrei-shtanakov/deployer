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
| `l4-from-bad-later` | exit 125 | a bad FROM in the **second** stage fails before **any** `STEP` line (stdout empty): Podman parses the whole file before building a stage |
| `l5-stages-same-image` | exit 0 | Podman **skips a stage nothing depends on**: only `[2/2]` steps appear; multi-stage step lines carry a `[i/n] ` prefix |
| `l6-copy-later-failure` | exit 1 | the corrected COPY built, then `RUN false` failed (`Error: building at STEP "RUN false"`) |
| `l7-stages-both-built` | exit 0 | two stages both build the identical COPY: `[1/2] STEP 3/3: COPY …` and `[2/2] STEP 3/4: COPY …` |

## What disagrees with the hypothesis

The hypothesis matchers read only `STEP k/n: …` lines without a prefix. On the
multi-stage recordings (`l5`, `l7`) they therefore report `not_confirmed` ("corrected
step line absent" / "no build-stage step"), not the expected `binding_ambiguous` /
`passed`. This is the conservative direction: nothing is falsely confirmed. But a
corrected instruction in a multi-stage Dockerfile could never be confirmed locally. Any
matcher change for the `[i/n] ` form is code and goes through the ordinary review (§9).
These recordings stay as they are.

Checksums: `CHECKSUMS.sha256` covers every file except itself and the recorder.
