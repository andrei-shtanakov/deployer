# Provenance: `checkout-sha-other` (negative case, spec §8.A)

Base: `run-1` — see `../run-1/PROVENANCE.md` for how every unchanged file was obtained. Derived on 2026-09-24 by a throwaway script (not committed).

Changes from the base (every input file that differs):

- `snapshot.json`: the SHA line after `[command]/usr/bin/git log -1 --format=%H` in the job text changed from `d6e330fd…` to `37242cc19934c1bffff50f2be5ce5d249c4dd987` (run-2's head SHA); `head_sha` unchanged.
- `local.stderr`: host paths anonymised to `/Users/example`.
