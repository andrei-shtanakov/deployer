# Provenance: admission bundles (A §8.2, §8.3 level P)

Spec: `docs/superpowers/specs/2026-09-24-ci-failure-admission-design.md` (cited as
**A**). Every case directory here is a copy of a committed reproduction bundle
(`../reproduction/run-1` or `../reproduction/run-5`) plus an authoring set made for
the test and exactly one mutation. Each case's own `PROVENANCE.md` names its base,
its mutation and every file that differs from the base.

## Two sources, never mixed

- **Real logs confirm the format.** The CI job text (in `snapshot.json`), the local
  Podman 5.7.0 output (`local.*`) and `endpoint.json` are the base bundles' bytes,
  unchanged: CI runs 35680991093 (`run-1`) and 35706782471 (`run-5`), recorded as
  `../reproduction/run-1/PROVENANCE.md` and `../reproduction/run-5/PROVENANCE.md`
  describe. They are what pins the template rows (A §4.1, §8.1).
- **The signed set is test-made and claims no historical authorship.** No real
  deployer run authored these Dockerfiles. The `.deployer/authoring/` set, and the
  `.dockerignore` line `.deployer/` that publishing it requires (A §5.2 step 3),
  were produced for this test by `deployer.provenance.issue.issue` with the key
  below. Its `source_commit` is the run's `head_sha` and its snapshot tree is the
  base `tree-listing.json`, so the set is consistent with the run, but the run
  itself never carried it.

## The test keys — TEST ONLY, private halves not committed

- Two ed25519 key pairs were generated on 2026-09-25 with `ssh-keygen -t ed25519
  -N ""` **for these fixtures only**: the signing key (public half `test-key.pub`)
  and `unknown-test-key` (public half `unknown-test-key.pub`), which exists only to
  make `unknown-key`'s signature and which no trust store lists. They protect
  nothing and must never be added to a real trust store (`~/.config/deployer/` or
  any `DEPLOYER_TRUST_DIR` used outside the test suite).
- **The private halves are not committed** (security hygiene in a public
  repository). Only the public halves are, with the trust stores built from them.
  Nothing at test time needs a private key: the replay only verifies.
- Fingerprints of the keys the committed data was signed with:

<!-- keys:begin (written by make_admission_bundle.py) -->
- `test-key.pub`: `SHA256:tRPwXXeQgjRTDqEXuhnXGKQFEsAwm14snOcsGryGpSs` (deployer-admission-TEST-ONLY-never-trust)
- `unknown-test-key.pub`: `SHA256:taS/c1R0R1dYWyi0Q34puV2jKoTI0Ii5t8i1JDaiHzo` (deployer-admission-TEST-ONLY-unknown-signer)
<!-- keys:end -->

- `trust/allowed_signers` is the shared test trust store: the signing key under the
  principal and namespace `deployer-authoring` (written by
  `deployer.provenance.trust.add`). `revoked-key/trust/` is that case's own store:
  the same `allowed_signers` plus a `revoked_keys` file listing the signing key.

## Regeneration and integrity

- `make_admission_bundle.py [--key PATH] [--unknown-key PATH]` rebuilds every case,
  both public keys, both trust stores, the fingerprint block above and
  `CHECKSUMS.sha256`; its docstring describes the method. Given the keys it is
  deterministic (ed25519 signatures, fixed `deployer_version`
  `0.0.0+admission-test`, no clock): with the original keys the output is
  byte-identical to the committed data. Without them (anyone but the keys'
  holder) the tool makes fresh keys, and every signature, public key, trust file,
  checksum and fingerprint changes. **Regenerating with new keys is a data change
  and needs the owner's review, like any other.**
- `CHECKSUMS.sha256` covers every file here except itself and the generator.
  `tests/admission/test_bundle_integrity.py` checks it, the case list, each
  `tree/` against its `tree-listing.json` by Git blob SHA, and each case's
  `PROVENANCE.md`. Checksums prove integrity, not correctness: `expected.json` is
  checked against A §8.2/§8.3 by review and by the replay test.

## `expected.json`

`base`; `trust` — where the replay points `DEPLOYER_TRUST_DIR`: `{"kind": "dir",
"path": <path relative to this directory>}`, `{"kind": "inside_tree", "path":
<path in the restored tree>}` or `{"kind": "symlink_into_tree", "path": ...}` (a
symlink outside the tree resolving to that path); `verdict`; `ownership`
(`status`, the failing `step` of A §2.4 or `null`); `unmet` — the exact list of
unmet conditions, each with substrings its reason must contain; `defect` (admitted
cases: `class`, `file`, `lines`); `accepted` — whether `accept_for_fix` accepts the
document against the case's own binding.
