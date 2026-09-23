# Provenance: `generating-step` (negative case, spec §8.A)

Base: `run-1` — see `../run-1/PROVENANCE.md` for how every unchanged file was obtained. Derived on 2026-09-24 by a throwaway script (not committed).

Changes from the base (every input file that differs):

- `tree/.github/workflows/diagnosis-polygon.yml`: a step `- run: make gen` between checkout and build; `tree-listing.json` entry sha recomputed.
- `snapshot.json`: `all_steps` gains `{number: 3, name: Run make gen, conclusion: success}` and every later step is renumbered (build 3→4, Post 6→7, Complete 7→8); `steps[0].ref.number` and its evidence `source.number` 3→4; a job-level evidence block `##[group]Run make gen\nmake gen\n##[endgroup]` (source step 3) inserted after the checkout step's block.
