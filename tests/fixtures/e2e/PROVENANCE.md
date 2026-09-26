# End-to-end acceptance evidence — provenance

This is the real end-to-end run of `ci-fix-authoring` (spec §11 stage 5, plan Task 26). It was made on 2026-09-26 under the owner's real-run permission of 2026-09-25, which included one paid model call per COPY case. The data is for the owner to review; only the owner merges it.

## What ran

| Step | `copy` (run-1-like) | `from` (run-5-like) |
|---|---|---|
| Base branch (orphan commit, polygon) | `polygon/fix-e2e-copy-base` @ `1b9ade2` | `polygon/fix-e2e-from-base` @ `63f37f3` |
| Failing CI run (`workflow_dispatch`) | 36239373873, failure | 36239375530, failure |
| `diagnose --reproduce` (real Podman) | admitted: `missing_copy_source`, `Dockerfile` 11–11, `docs/setup.md` | admitted: `from_argument_count`, `Dockerfile` 1–1 |
| `deployer fix` (real Podman local proof) | `locally_confirmed`; the model chose `COPY docs/guide/setup.md ./setup.md`; commit `8f147d3` | `locally_confirmed`; F1 `FROM python:3.12-slim AS extra`; commit `c5bb893` |
| `deployer fix publish` | `fix_proposed`, PR #101 (closed unmerged) | `fix_proposed`, PR #102 (closed unmerged) |
| CI on the fix commit (`workflow_dispatch`) | 36239524141, success | 36239525886, success |
| `deployer fix confirm` | **`ci_confirmed`**: `#14 [stage-0 7/9] COPY docs/guide/setup.md ./setup.md` / `#14 DONE` | **`ci_confirmed`**: `#7 [extra 1/8] FROM docker.io/library/python:3.12-slim@…` |

- **Model.** `claude-opus-4-8` (`deployer.llm.DEFAULT_MODEL`), real Anthropic API. It was called exactly once, for the `copy` case only; `from` makes no model call.
- **Environment.** Local Podman 5.7.0 / Buildah 1.42.0; GitHub Actions `ubuntu-24.04`.
- **Base branches.** Each is an orphan commit with the project files of `polygon/run-5` (`937d465`), the failing `Dockerfile`, and the dispatch-only polygon workflow with one `docker build --file ./Dockerfile .`. The `copy` base also has `docs/guide/setup.md`. On top of that commit, the authoring set was issued by the **production** `deployer.provenance.issue.preflight` + `issue` in a clean clone with `origin`, then committed. That is the path `deployer author --signing-key` uses, without the model authoring step, because the Dockerfile is the case input. The branches stay on origin as evidence.
- **Key.**
  - Signing used a fresh ed25519 key made for this run only, with fingerprint `SHA256:ZfXlikP8gMKmTktNso+16/DU+k8dx8nbswH8+kB9VGk`. Its public half is `e2e-key.pub`.
  - It was trusted through `deployer trust add` in a scratch trust store (`DEPLOYER_TRUST_DIR`); the user's store was not touched.
  - After the run the key was revoked there and the private half deleted.
- **Operator error, recorded.** The first `fix publish` of both cases ran without `DEPLOYER_TRUST_DIR`. The admission re-check correctly refused it ("no allowed_signers file"), and nothing was pushed. The re-run with the scratch store succeeded. Each `fix.json` `last_operation` holds the final operation; the refused attempt's log was overwritten by the re-run.
- **An earlier `diagnose` invocation** failed on a shell quoting slip on my side, before any request. It produced no data.

## Files (per case, verbatim copies from the run directories)

- `verdict.json`: the `diagnose --reproduce` output, with `admission` admitted.
- `reproduction/`: R's `source.json`, the CI log (`ci.log`), and the local build streams and manifest.
- `fix/fix.json`: the final fix document, `ci_confirmed`. It holds absolute local paths (the run directory, worktree and clone) as written.
  **These absolute paths are kept on purpose** (owner, #103): the evidence is verbatim, and the paths hold no secret, only the local username (the same as the GitHub login) and the directory layout. This is the first time absolute paths appear under `tests/`.
- `fix/build.stdout` and `fix/build.stderr`: the local proof build.
- `fix/fix-commit.patch`: `git format-patch` of the fix commit. It changes exactly one Dockerfile instruction and re-issues the authoring set.
- `diagnose.log`, `fix.log`, `publish.log`, `confirm.log`: the CLI output of each step.

Checksums: `CHECKSUMS.sha256` covers every file except itself.
