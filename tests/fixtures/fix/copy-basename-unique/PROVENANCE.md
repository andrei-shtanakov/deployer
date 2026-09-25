# Provenance: `copy-basename-unique` (fix case, F §10 level P, §11 stage 1b)

Proves: the envelope passes, the prompt lists every eligible blob, and the proposal is the model's choice (here: the fake answer's).

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

- one regular file `tree/docs/guide/setup.md` added before the set was issued: the only file whose basename is the absent source's. Every other regular file of the tree stays eligible; the envelope does not choose.
- A `.deployer/authoring/` set, and the `.dockerignore` line `.deployer/` it
  requires, issued by `deployer.provenance.issue.issue` with the test-only key
  whose public half is `../test-key.pub` (private key not committed),
  `deployer_version` `0.0.0+fix-test`, `source_commit` = the run's
  `head_sha`, the snapshot tree = the modified tree's listing. No real
  deployer run authored this Dockerfile.
- `fake-model-answer.json` is a **FAKE model answer**, written by hand into the generator for the acceptance test's fake chooser. No model produced it; it shows the shape of an answer, not what a model would choose.
- `expected.json` records what `deployer fix` must do with this bundle.

## Every file that differs from the base

- `tree-listing.json`: changed
- `tree/.deployer/authoring/Dockerfile.current`: added
- `tree/.deployer/authoring/Dockerfile/5ee6ee07fe6fafd73cbb6ff2bc3bc71a58b0899e468036ea010a9b63cb3a1534/record.json`: added
- `tree/.deployer/authoring/Dockerfile/5ee6ee07fe6fafd73cbb6ff2bc3bc71a58b0899e468036ea010a9b63cb3a1534/record.json.sig`: added
- `tree/.deployer/authoring/Dockerfile/5ee6ee07fe6fafd73cbb6ff2bc3bc71a58b0899e468036ea010a9b63cb3a1534/snapshot.json`: added
- `tree/.dockerignore`: added
- `tree/docs/guide/setup.md`: added

`tree-listing.json` is `git ls-tree -r -t --full-tree` of this `tree/`; its
`sha` stays the run's `head_sha`. `expected.json` and this file are new.
