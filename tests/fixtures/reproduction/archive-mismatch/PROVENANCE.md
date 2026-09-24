# Provenance: `archive-mismatch` (negative case, spec §8.A)

Base: `run-1` — see `../run-1/PROVENANCE.md` for how every unchanged file was obtained. Derived on 2026-09-24 by a throwaway script (not committed).

Changes from the base (every input file that differs):

- `tree-listing.json`: gains a blob entry `docs/notes.md` (sha of `notes\n`) that `tree/` lacks.
- `local.stderr`: host paths anonymised to `/Users/example`.
