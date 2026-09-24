# Provenance: `endpoint-remote` (negative case, spec §8.A)

Base: `run-1` — see `../run-1/PROVENANCE.md` for how every unchanged file was obtained. Derived on 2026-09-24 by a throwaway script (not committed).

Changes from the base (every input file that differs):

- `endpoint.json`: the default connection's URI → `ssh://core@10.0.0.5/run/user/501/podman/podman.sock` (the root connection's → `ssh://root@10.0.0.5/run/podman/podman.sock`).
- `local.stderr`: host paths anonymised to `/Users/example`.
