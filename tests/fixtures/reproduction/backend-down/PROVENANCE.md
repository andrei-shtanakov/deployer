# Provenance: `backend-down` (negative case, spec §8.A)

Base: `run-5` — see `../run-5/PROVENANCE.md` for how every unchanged file was obtained. Derived on 2026-09-24 by a throwaway script (not committed).

Changes from the base (every input file that differs):

- `local.stdout` → empty; `local.stderr` → Podman's `Error: Cannot connect to Podman. …` line (text from the task brief, Podman's documented message); `local.exit` → `125`.
