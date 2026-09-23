# Provenance: `containerignore` (negative case, spec §8.A)

Base: `run-1` — see `../run-1/PROVENANCE.md` for how every unchanged file was obtained. Derived on 2026-09-24 by a throwaway script (not committed).

Changes from the base (every input file that differs):

- `tree/.dockerignore` (`tests\n`) and `tree/.containerignore` (empty) added; `tree-listing.json` gains both blob entries.
- `local.*` unchanged: the recorded build fails at line 11, which neither ignore file affects.
