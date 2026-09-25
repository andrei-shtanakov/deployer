# Provenance: `admit-run-5` (admission case, A §8.2 positive, `from_argument_count`)

Base: reproduction bundle `run-5` — see
`../../reproduction/run-5/PROVENANCE.md` for how every unchanged file was
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

none beyond the issued set.

Trust store for the replay: `{"kind": "dir", "path": "trust"}` (see `expected.json`).

## Every file that differs from the base

- `tree-listing.json`: changed
- `tree/.deployer/authoring/Dockerfile.current`: added
- `tree/.deployer/authoring/Dockerfile/9ea3d627d2e4cd7df2fb1ba275aa6fb02e3be21c5e6528bf0fbe52018a67929c/record.json`: added
- `tree/.deployer/authoring/Dockerfile/9ea3d627d2e4cd7df2fb1ba275aa6fb02e3be21c5e6528bf0fbe52018a67929c/record.json.sig`: added
- `tree/.deployer/authoring/Dockerfile/9ea3d627d2e4cd7df2fb1ba275aa6fb02e3be21c5e6528bf0fbe52018a67929c/snapshot.json`: added
- `tree/.dockerignore`: added

`tree-listing.json` is `git ls-tree -r -t --full-tree` of this `tree/`; its
`sha` stays the run's `head_sha`. `expected.json` and this file are new.
