# Provenance: `gitattributes` (negative case, spec §8.A)

Base: `run-1` — see `../run-1/PROVENANCE.md` for how every unchanged file was obtained. Derived on 2026-09-24 by a throwaway script (not committed).

Changes from the base (every input file that differs):

- `tree/.gitattributes` added with content `* export-subst\n`; `tree-listing.json` gains its blob entry (mode 100644, sha by `git_blob_sha`).
- `local.stderr`: host paths anonymised to `/Users/example`.
