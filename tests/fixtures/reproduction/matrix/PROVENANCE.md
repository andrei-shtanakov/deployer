# Provenance: `matrix` (negative case, spec §8.A)

Base: `run-1` — see `../run-1/PROVENANCE.md` for how every unchanged file was obtained. Derived on 2026-09-24 by a throwaway script (not committed).

Changes from the base (every input file that differs):

- `tree/.github/workflows/diagnosis-polygon.yml`: `strategy.matrix` (`os: [ubuntu-24.04]`) on job `build`; `tree-listing.json` entry sha recomputed. The snapshot's job name stays `build`.
- `local.stderr`: host paths anonymised to `/Users/example`.
