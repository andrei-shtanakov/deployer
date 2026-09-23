# Provenance: `run-2` (base case, spec §8.A)

- Run: https://github.com/andrei-shtanakov/deployer/actions/runs/35680992960
- `head_sha`: `37242cc19934c1bffff50f2be5ce5d249c4dd987`
- `snapshot.json`: re-fetched read-only through `deployer.forge.fetch_failed_run`
  (`gh api` GETs only, no dispatch) on 2026-09-23 20:12 UTC with
  `uv run python tests/fixtures/reproduction/make_bundle.py snapshot --run-id 35680992960 --out tests/fixtures/reproduction/run-2`;
  anonymised as the 1.2 fixtures were (`andrei-shtanakov/deployer` → `example/project`,
  `/home/runner/work/deployer/deployer` → `/home/runner/work/project/project`); ids,
  SHAs and log text kept. Schema 1.3.
- `tree/`, `tree-listing.json`: `git archive` / `git ls-tree -r -t --full-tree` of
  `refs/keep/polygon-run-2` (`37242cc`) from local git objects, via `make_bundle.py tree --ref refs/keep/polygon-run-2`.
- `local.stdout`, `local.stderr`, `local.exit`: a real local build on 2026-09-23 20:13 UTC,
  podman version 5.7.0 (podman-machine-default, applehv, arm64 host):
  `podman build --file tests/fixtures/reproduction/run-2/tree/Dockerfile --tag localhost/repro-probe --force-rm tests/fixtures/reproduction/run-2/tree`
  (stdout, stderr and `$?` redirected to the three files, verbatim; the probe image
  was removed afterwards with `podman rmi -f localhost/repro-probe`).
- `endpoint.json`: `podman system connection list --format json` on the same machine,
  wrapped as `{"tool": "podman", "connections": <that JSON>, "env": {}}`; the
  `Identity` paths' home directory is replaced by `/Users/example`.
- `expected.json`: Spec §8.A: exact; no §3 finding; both bound 7-9; signature `E: Some index files failed to download. They have been ignored, or old ones used instead.` equal; reproduced_with_differences. The recording matches (the unroutable mirror 10.255.255.1 timed out after ~18 s; exit 100).
- Changes: no change.
