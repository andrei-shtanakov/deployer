# Provenance: `other-line` (negative case, spec §8.A)

Base: `run-3` — see `../run-3/PROVENANCE.md` for how every unchanged file was obtained. Derived on 2026-09-24 by a throwaway script (not committed).

Changes from the base (every input file that differs):

- `local.stdout`: the recorded transcript cut after `STEP 7/13: RUN uv sync --frozen` (line 12, the only instruction with that normalised text), followed by one synthesised program line `error: Failed to build \`ci-build @ file:///app\``; `local.stderr` → `Error: building at STEP "RUN uv sync --frozen": while running runtime: exit status 1` (Podman 5.7.0's shape as recorded in the base). `local.exit` stays `1`.
