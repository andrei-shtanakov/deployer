# Provenance: `trust-symlink` (admission case, A §8.3 (1) step 0, via a symlink)

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

`tree/ci-trust/allowed_signers` added as in `trust-inside`; the trust dir is a symlink outside the tree that resolves to it.

Trust store for the replay: `{"kind": "symlink_into_tree", "path": "ci-trust"}` (see `expected.json`).

## Every file that differs from the base

- `tree-listing.json`: changed
- `tree/.deployer/authoring/Dockerfile.current`: added
- `tree/.deployer/authoring/Dockerfile/9f63aae2db8135568715c25c760cb286e0ced115fbe21d1fcfa85b993739cbc6/record.json`: added
- `tree/.deployer/authoring/Dockerfile/9f63aae2db8135568715c25c760cb286e0ced115fbe21d1fcfa85b993739cbc6/record.json.sig`: added
- `tree/.deployer/authoring/Dockerfile/9f63aae2db8135568715c25c760cb286e0ced115fbe21d1fcfa85b993739cbc6/snapshot.json`: added
- `tree/.dockerignore`: added
- `tree/ci-trust/allowed_signers`: added

`tree-listing.json` is `git ls-tree -r -t --full-tree` of this `tree/`; its
`sha` stays the run's `head_sha`. `expected.json` and this file are new.
