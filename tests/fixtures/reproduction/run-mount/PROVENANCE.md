# Provenance: `run-mount` (negative case, spec §8.A)

Base: `run-3` — see `../run-3/PROVENANCE.md` for how every unchanged file was obtained. Derived on 2026-09-24 by a throwaway script (not committed).

Changes from the base (every input file that differs):

- `tree/Dockerfile` line 15 → `RUN --mount=type=bind,target=/src uv run --frozen python -m unittest discover -s tests`; `tree-listing.json` entry sha recomputed.
- `snapshot.json`: in the CI build log every `RUN uv run --frozen python -m unittest discover -s tests` → `RUN --mount=type=bind,target=/src uv run --frozen python -m unittest discover -s tests` — the `#16 [stage-0  9/10] RUN …` header, the ` > [stage-0  9/10] RUN …:` summary line and the `>>>` line 15 (3 occurrences). BuildKit's `process "/bin/sh -c uv run …"` line carries no `RUN` or flags and is unchanged.
- `local.stdout`: the `STEP 9/13: RUN …` line → the same text; `local.stderr`: the `Error: building at STEP "RUN …"` line → the same text (kept consistent with the STEP line; either binds line 15).
