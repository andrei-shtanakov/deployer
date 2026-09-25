# Provenance: `duplicate-path` (admission case, A §8.3 (1) step 6, a duplicate path)

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

the snapshot's first tree row appended again at the end, re-signed (new set directory).

Trust store for the replay: `{"kind": "dir", "path": "trust"}` (see `expected.json`).

## Every file that differs from the base

- `tree-listing.json`: changed
- `tree/.deployer/authoring/Dockerfile.current`: added
- `tree/.deployer/authoring/Dockerfile/11ccc110b088f830d07bd7aa9bce65d3dc77652a73a5f5ef0a2c9656f37192cc/record.json`: added
- `tree/.deployer/authoring/Dockerfile/11ccc110b088f830d07bd7aa9bce65d3dc77652a73a5f5ef0a2c9656f37192cc/record.json.sig`: added
- `tree/.deployer/authoring/Dockerfile/11ccc110b088f830d07bd7aa9bce65d3dc77652a73a5f5ef0a2c9656f37192cc/snapshot.json`: added
- `tree/.dockerignore`: added

`tree-listing.json` is `git ls-tree -r -t --full-tree` of this `tree/`; its
`sha` stays the run's `head_sha`. `expected.json` and this file are new.
