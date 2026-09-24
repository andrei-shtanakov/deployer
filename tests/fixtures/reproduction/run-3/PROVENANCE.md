# Provenance: `run-3` (base case, spec §8.A)

- Run: https://github.com/andrei-shtanakov/deployer/actions/runs/35680994771
- `head_sha`: `43d7c3975c8858ad8b1c9fad6518bbe674597b4b`
- `snapshot.json`: re-fetched read-only through `deployer.forge.fetch_failed_run`
  (`gh api` GETs only, no dispatch) on 2026-09-23 20:12 UTC with
  `uv run python tests/fixtures/reproduction/make_bundle.py snapshot --run-id 35680994771 --out tests/fixtures/reproduction/run-3`;
  anonymised as the 1.2 fixtures were (`andrei-shtanakov/deployer` → `example/project`,
  `/home/runner/work/deployer/deployer` → `/home/runner/work/project/project`); ids,
  SHAs and log text kept. Schema 1.3.
- `tree/`, `tree-listing.json`: `git archive` / `git ls-tree -r -t --full-tree` of
  `refs/keep/polygon-run-3` (`43d7c39`) from local git objects, via `make_bundle.py tree --ref refs/keep/polygon-run-3`.
- `local.stdout`, `local.stderr`, `local.exit`: a real local build on 2026-09-23 20:13 UTC,
  podman version 5.7.0 (podman-machine-default, applehv, arm64 host):
  `podman build --file tests/fixtures/reproduction/run-3/tree/Dockerfile --tag localhost/repro-probe --force-rm tests/fixtures/reproduction/run-3/tree`
  (stdout, stderr and `$?` redirected to the three files, verbatim; the probe image
  was removed afterwards with `podman rmi -f localhost/repro-probe`).
- `endpoint.json`: `podman system connection list --format json` on the same machine,
  wrapped as `{"tool": "podman", "connections": <that JSON>, "env": {}}`; the
  `Identity` paths' home directory is replaced by `/Users/example`.
- `expected.json`: Spec §8.A: exact; no §3 finding; both bound 15; `FAILED (failures=1)` equal; reproduced_with_differences. The recording matches.
- Changes: no change.
