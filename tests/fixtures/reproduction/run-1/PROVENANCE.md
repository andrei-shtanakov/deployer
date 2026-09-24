# Provenance: `run-1` (base case, spec §8.A)

- Run: https://github.com/andrei-shtanakov/deployer/actions/runs/35680991093
- `head_sha`: `d6e330fd8d85f761962d8a134f0ffdd0e914bf9b`
- `snapshot.json`: re-fetched read-only through `deployer.forge.fetch_failed_run`
  (`gh api` GETs only, no dispatch) on 2026-09-23 20:12 UTC with
  `uv run python tests/fixtures/reproduction/make_bundle.py snapshot --run-id 35680991093 --out tests/fixtures/reproduction/run-1`;
  anonymised as the 1.2 fixtures were (`andrei-shtanakov/deployer` → `example/project`,
  `/home/runner/work/deployer/deployer` → `/home/runner/work/project/project`); ids,
  SHAs and log text kept. Schema 1.3.
- `tree/`, `tree-listing.json`: `git archive` / `git ls-tree -r -t --full-tree` of
  `refs/keep/polygon-run-1` (`d6e330f`) from local git objects, via `make_bundle.py tree --ref refs/keep/polygon-run-1`.
- `local.stdout`, `local.stderr`, `local.exit`: a real local build on 2026-09-23 20:13 UTC,
  podman version 5.7.0 (podman-machine-default, applehv, arm64 host):
  `podman build --file tests/fixtures/reproduction/run-1/tree/Dockerfile --tag localhost/repro-probe --force-rm tests/fixtures/reproduction/run-1/tree`
  (stdout, stderr and `$?` redirected to the three files, verbatim; the probe image
  was removed afterwards with `podman rmi -f localhost/repro-probe`).
- `endpoint.json`: `podman system connection list --format json` on the same machine,
  wrapped as `{"tool": "podman", "connections": <that JSON>, "env": {}}`; the
  `Identity` paths' home directory is replaced by `/Users/example`.
- `expected.json`: Spec §8.A: exact; copy_sources failed docs/setup.md line 11; local exit 125 bound step_text 11; not_compared; reproduced_with_differences. The recording matches.
- Changes: `local.stderr`: host paths anonymised to `/Users/example` (a leftover
  home-directory path from the original recording, not caught with the rest).
