# Provenance: `local-success` (negative case, spec §8.A)

Base: `run-2` — see `../run-2/PROVENANCE.md` for how every unchanged file was obtained. Derived on 2026-09-24 by a throwaway script (not committed).

Changes from the base (every input file that differs):

- `local.exit` → `0`, `local.stderr` → empty, `local.stdout` → a successful build's transcript. **Synthesised, not a live recording**: the live-action authorisation for this task covered exactly the four base builds, so no fifth build was run. The transcript reuses run-2's recorded STEP 1-3 lines and continues with the remaining STEP lines in Podman 5.7.0's shape, ending in `COMMIT` / `Successfully tagged`. The replay reads only the exit code for this case (§7.3 state 3 is decided before any binding).
