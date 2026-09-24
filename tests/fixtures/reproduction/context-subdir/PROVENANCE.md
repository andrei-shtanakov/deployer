# Provenance: `context-subdir` (negative case, spec §8.A)

Base: `run-1` — see `../run-1/PROVENANCE.md` for how every unchanged file was obtained. Derived on 2026-09-24 by a throwaway script (not committed).

Changes from the base (every input file that differs):

- `tree/.github/workflows/diagnosis-polygon.yml`: the build step's `run:` → `docker build --file ./Dockerfile app`; `tree-listing.json` entry sha recomputed.
- `snapshot.json`: the build step's name in `all_steps` and `steps[0]`, and the `##[group]Run …` header and echoed command in its evidence text, all → `docker build --file ./Dockerfile app`.
- `local.stderr`: host paths anonymised to `/Users/example`.
