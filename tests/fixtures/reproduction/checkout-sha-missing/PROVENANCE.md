# Provenance: `checkout-sha-missing` (negative case, spec §8.A)

Base: `run-1` — see `../run-1/PROVENANCE.md` for how every unchanged file was obtained. Derived on 2026-09-24 by a throwaway script (not committed).

Changes from the base (every input file that differs):

- `snapshot.json`: the job-level evidence block holding the `[command]/usr/bin/git log -1 --format=%H` + SHA line pair removed (the block was exactly that pair).
