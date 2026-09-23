# Provenance: `several-failed-jobs` (negative case, spec §8.A)

Base: `run-1` — see `../run-1/PROVENANCE.md` for how every unchanged file was obtained. Derived on 2026-09-24 by a throwaway script (not committed).

Changes from the base (every input file that differs):

- `snapshot.json`: a second failed job appended — a copy of the first with `job_id` 106597702100 (every step ref and evidence source renumbered to it) and name `build-again`. It keeps its own `[command]/usr/bin/git log -1 --format=%H` + head_sha pair, since §1.2 #2 is checked per job before #3.
