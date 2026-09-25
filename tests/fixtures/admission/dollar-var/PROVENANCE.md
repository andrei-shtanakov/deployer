# Provenance: `dollar-var` (admission case, A §8.3 (2) form, `COPY $VAR` (P: `(3) restoration not exact`))

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

`tree/Dockerfile` line 11 `COPY docs/setup.md ./setup.md` → `COPY $VAR ./setup.md`, made *before* the set was issued (the set covers the edited bytes). The recorded CI and local logs are the base's and still print `COPY docs/setup.md ./setup.md`: this case documents R's early refusal (restoration approximation), not a recorded `$VAR` build.

Trust store for the replay: `{"kind": "dir", "path": "trust"}` (see `expected.json`).

## Every file that differs from the base

- `tree-listing.json`: changed
- `tree/.deployer/authoring/Dockerfile.current`: added
- `tree/.deployer/authoring/Dockerfile/b79358936ad04ca97efe0279dfebb33c24a76cd688465040953289a4cb31e003/record.json`: added
- `tree/.deployer/authoring/Dockerfile/b79358936ad04ca97efe0279dfebb33c24a76cd688465040953289a4cb31e003/record.json.sig`: added
- `tree/.deployer/authoring/Dockerfile/b79358936ad04ca97efe0279dfebb33c24a76cd688465040953289a4cb31e003/snapshot.json`: added
- `tree/.dockerignore`: added
- `tree/Dockerfile`: changed

`tree-listing.json` is `git ls-tree -r -t --full-tree` of this `tree/`; its
`sha` stays the run's `head_sha`. `expected.json` and this file are new.
