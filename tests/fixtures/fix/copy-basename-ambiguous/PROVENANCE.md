# Provenance: `copy-basename-ambiguous` (fix case, F §10 level P, §11 stage 1b)

Proves: the basename floor stops the envelope before the model is called.

Base: reproduction bundle `run-1` — see `../../reproduction/run-1/PROVENANCE.md`.
Built by `../make_fix_bundle.py` (deterministic given the key; see its
docstring).

## The original failure record (real, unchanged)

`snapshot.json` (the CI job text of run 35680991093), `local.stdout`,
`local.stderr`, `local.exit` and `endpoint.json` are `run-1`'s bytes, byte for
byte: a real CI run and a real Podman 5.7.0 build. They record the build
failing on `COPY docs/setup.md ./setup.md` (line 11). Neither the real run nor the
real build saw the files added below.

## The test modification (made for this test, claims no history)

- two regular files `tree/docs/a/setup.md` and `tree/docs/b/setup.md` added before the set was issued: two eligible blobs share the absent source's basename.
- A `.deployer/authoring/` set, and the `.dockerignore` line `.deployer/` it
  requires, issued by `deployer.provenance.issue.issue` with the test-only key
  whose public half is `../test-key.pub` (private key not committed),
  `deployer_version` `0.0.0+fix-test`, `source_commit` = the run's
  `head_sha`, the snapshot tree = the modified tree's listing. No real
  deployer run authored this Dockerfile.
- `expected.json` records what `deployer fix` must do with this bundle.

## Every file that differs from the base

- `tree-listing.json`: changed
- `tree/.deployer/authoring/Dockerfile.current`: added
- `tree/.deployer/authoring/Dockerfile/bc6446fc29269229836e6ac35c21b7bd194cf11a059d0e839e5f8dbff0eb230e/record.json`: added
- `tree/.deployer/authoring/Dockerfile/bc6446fc29269229836e6ac35c21b7bd194cf11a059d0e839e5f8dbff0eb230e/record.json.sig`: added
- `tree/.deployer/authoring/Dockerfile/bc6446fc29269229836e6ac35c21b7bd194cf11a059d0e839e5f8dbff0eb230e/snapshot.json`: added
- `tree/.dockerignore`: added
- `tree/docs/a/setup.md`: added
- `tree/docs/b/setup.md`: added

`tree-listing.json` is `git ls-tree -r -t --full-tree` of this `tree/`; its
`sha` stays the run's `head_sha`. `expected.json` and this file are new.
