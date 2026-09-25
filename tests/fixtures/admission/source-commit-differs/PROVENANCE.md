# Provenance: `source-commit-differs` (admission case, A §8.3 (1) step 6, `source_commit` differs)

Base: reproduction bundle `run-1` — see
`../../reproduction/run-1/PROVENANCE.md` for how every unchanged file was
obtained. Built by `../make_admission_bundle.py` (deterministic; see its
docstring).

## Two sources, kept apart

- **Real logs confirm the format.** `snapshot.json` (the CI job text),
  `local.stdout`, `local.stderr`, `local.exit` and `endpoint.json` are the
  base bundle's, byte for byte: a real CI run and a real Podman 5.7.0 build.
  They confirm what the builders print, nothing about authorship.
- **The signed set is test-made; it claims no historical authorship.** The
  `.deployer/authoring/` set (and the `.dockerignore` exclusion it needs)
  did not exist in the real run. It was issued for this test by
  `deployer.provenance.issue.issue` with the test-only key whose public
  half is `../test-key.pub` (private key not committed),
  `deployer_version` `0.0.0+admission-test`, `source_commit` = the run's
  `head_sha`, the snapshot tree = the base `tree-listing.json`. No real
  deployer run authored this Dockerfile.

## The mutation

snapshot `source_commit` set to `0000000000000000000000000000000000000000`, its hash recomputed into the record, re-signed (new set directory).

Trust store for the replay: `{"kind": "dir", "path": "trust"}` (see `expected.json`).

## Every file that differs from the base

- `tree-listing.json`: changed
- `tree/.deployer/authoring/Dockerfile.current`: added
- `tree/.deployer/authoring/Dockerfile/4fb326cf57dadb3a9c9a1043578e9e3c74b7f549392457af5e22697430627b50/record.json`: added
- `tree/.deployer/authoring/Dockerfile/4fb326cf57dadb3a9c9a1043578e9e3c74b7f549392457af5e22697430627b50/record.json.sig`: added
- `tree/.deployer/authoring/Dockerfile/4fb326cf57dadb3a9c9a1043578e9e3c74b7f549392457af5e22697430627b50/snapshot.json`: added
- `tree/.dockerignore`: added

`tree-listing.json` is `git ls-tree -r -t --full-tree` of this `tree/`; its
`sha` stays the run's `head_sha`. `expected.json` and this file are new.
