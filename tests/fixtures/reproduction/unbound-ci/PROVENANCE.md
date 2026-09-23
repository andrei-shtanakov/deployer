# Provenance: `unbound-ci` (negative case, spec §8.A)

Base: `run-1` — see `../run-1/PROVENANCE.md` for how every unchanged file was obtained. Derived on 2026-09-24 by a throwaway script (not committed).

Changes from the base (every input file that differs):

- `snapshot.json`: the BuildKit error block `Dockerfile:11` … `--------------------` (8 lines, incl. the `>>>` line) removed from the job text.
