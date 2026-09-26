# Provenance: derived run-1 fix bundles (F §10 level P, §11 stage 1b)

Spec: `docs/superpowers/specs/2026-09-25-ci-fix-authoring-design.md` (cited as
**F**). Each case directory here is a copy of the committed reproduction bundle
`../reproduction/run-1` plus files added to its tree and an authoring set made for
the test. Each case's own `PROVENANCE.md` names what it proves, what was added and
every file that differs from the base. The cases are the reviewed inputs of the
stage-5 end-to-end acceptance of `deployer fix` for `missing_copy_source`.

| Case | Added to `tree/` before signing | Expected |
|---|---|---|
| `copy-basename-unique` | `docs/guide/setup.md` | admitted; envelope passed; the prompt lists every eligible blob; the proposal is the model's choice (the bundle's labelled fake answer); without the test seam `no local confirmation: templates not enabled` |
| `copy-basename-ambiguous` | `docs/a/setup.md`, `docs/b/setup.md` | admitted; `fix method not established: 5 basename floor: 2 eligible files…`; the model not called |

## Two sources, never mixed

- **The original failure record is real and unchanged.** The CI job text (in
  `snapshot.json`), the local Podman 5.7.0 output (`local.*`) and `endpoint.json`
  are `run-1`'s bytes, byte for byte: CI run 35680991093, recorded as
  `../reproduction/run-1/PROVENANCE.md` describes. The real run failed on
  `COPY docs/setup.md ./setup.md` (line 11); its tree had no `docs/` at all.
- **The test modification is test-made and claims no history.** The added
  `setup.md` files never existed in the real commit; each says so in its own
  text. The `.deployer/authoring/` set, and the `.dockerignore` line `.deployer/`
  that publishing it requires, were produced for this test by
  `deployer.provenance.issue.issue` with the key below. Its `source_commit` is the
  run's `head_sha` and its snapshot tree is the modified tree's listing, so the set
  is consistent with the bundle, but the run itself never carried it. The
  `fake-model-answer.json` of `copy-basename-unique` is a hand-written **fake**
  model answer, not a model output.

## The test key — TEST ONLY, private half not committed

- One ed25519 key pair was generated on 2026-09-25 with `ssh-keygen -t ed25519
  -N ""` **for these fixtures only** (not the admission bundles' key). It protects
  nothing and must never be added to a real trust store (`~/.config/deployer/` or
  any `DEPLOYER_TRUST_DIR` used outside the test suite).
- **The private half is not committed.** Only the public half (`test-key.pub`) is,
  with the trust store built from it. Nothing at test time needs a private key:
  the replay only verifies.
- Fingerprint of the key the committed data was signed with:

<!-- keys:begin (written by make_fix_bundle.py) -->
- `test-key.pub`: `SHA256:MIxv7UF+vCZrMIUCaGfMc9lx/ZKppCPmxK1k+0/dpko` (deployer-fix-TEST-ONLY-never-trust)
<!-- keys:end -->

- `trust/allowed_signers` is the test trust store: the key under the principal and
  namespace `deployer-authoring` (written by `deployer.provenance.trust.add`).

## Regeneration and integrity

- `make_fix_bundle.py [--key PATH]` rebuilds every case, the public key, the trust
  store, the fingerprint block above and `CHECKSUMS.sha256`; its docstring
  describes the method. Given the key it is deterministic. Without it the tool
  makes a fresh key, and every signature, the public key, the trust file, the
  checksums and the fingerprint change. **Regenerating with a new key is a data
  change and needs the owner's review, like any other.**
- `CHECKSUMS.sha256` covers every file here except itself and the generator.
  `tests/fix/test_fix_bundle_integrity.py` checks it, the case list, each `tree/`
  against its `tree-listing.json` by Git blob SHA, each case's `PROVENANCE.md` and
  that no private key is present. Checksums prove integrity, not correctness:
  `expected.json` is checked against F §4.1/§10 by review and by the replay.

## `expected.json`

`base`; `trust` — where the replay points `DEPLOYER_TRUST_DIR` (`{"kind": "dir",
"path": <relative to this directory>}`); `admission` — A's verdict after the
replay; `defect`; `envelope` — `passed` or `stopped`; `model_called`; then either
`prompt_lists` (the eligible blobs the prompt must list), `model_answer` (the fake
answer's file, `fake: true`), `proposal` (`chosen_by: model`, transformation,
original and replacement instruction), `without_seam` (the stop when no template
row is enabled) and, in `copy-basename-unique` only, `without_seam_note` (a dated
annotation that `without_seam` predates #99 — see the note below), or `stop` (status,
reason, the detail's prefix).

**Note (2026-09-26).** `copy-basename-unique`'s `without_seam` (`no local
confirmation: templates not enabled`) was true when the bundle was recorded (#94),
before any template row was enabled. Since F3b (#99) the local rows are enabled on the
L-recordings, and since F4b the CI rows on the C-recordings, so production no longer
stops that way. `expected.json` is kept as recorded, not regenerated: `without_seam`
states the stop with no template row enabled. The case's own `PROVENANCE.md` carries
the same note, written by `make_fix_bundle.py`.
