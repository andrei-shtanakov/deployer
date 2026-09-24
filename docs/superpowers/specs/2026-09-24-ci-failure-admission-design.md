# CI-failure admission — design ("proven deployer artifact defect")

**Status:** DRAFT, designed with the owner on 2026-09-24 (four sections, each approved
with refinements, all folded in below). Next: a targeted consistency review of this
document, then a plan. No code exists for this design.
**Item:** `todo://deployer/ci-failure-diagnosis` (its `@blocked_by` is left unchanged
by the owner). This design answers what closes the item.
**Base:** the reading layer (#72) and the reproduction slice (#76–#79, #81, #82) on
`master` @ `1e9bbf4`; spec
`docs/superpowers/specs/2026-09-22-ci-failure-reproduction-design.md` (cited as **R**,
e.g. R §6).

## Purpose

A single, narrow decision: **may a failed CI run enter `ci-fix-authoring`?** The
positive verdict is *"a defect of a deployer-authored artifact is proven"*. It needs
three things proven together:

1. **Ownership** — deployer authored the artifact as it exists in the failed run.
2. **Defect** — a specific defect of that artifact, from a closed catalogue.
3. **Link** — the CI failure and the local failure are both that defect, in that
   artifact, at that instruction.

Everything else is `insufficient_grounds` — "not enough grounds for an automatic
fix". It is not a claim that deployer is blameless, nor a classification of the
failure.

## Non-goals

- ENVIRONMENT / PROJECT classes and automatic routing (e.g. through dispatcher).
- New CI runs, new builds, network access at diagnosis time: admission is computed from
  results that already exist (the reading layer, R's reproduction, the restored tree).
- Log phrases as evidence on their own. A **structured** builder diagnostic bound to
  an instruction and an object (§4) may be part of the evidence; free text may not.
- Defect classes beyond the two of §3. A new class is a new spec.
- Authoring records for artifacts other than the Dockerfile.
- Automatic key rotation.

## 1. The verdict

`admission.verdict` is exactly one of:

- `admitted` — conditions (1), (2) and (3) are all proven; every required field of
  §6 is filled; `unmet` is empty.
- `insufficient_grounds` — `unmet` lists **at least one** concrete unmet or
  unconfirmed condition, each tagged with its condition number and a reason. `defect`
  and `link` may be absent or partial: no evidence is invented to fill the schema.

When in doubt, `insufficient_grounds`. A false admission is worse than a false refusal.

## 2. Condition (1): ownership

### 2.1 What is proven

That the Dockerfile at `head_sha` is, byte for byte, an artifact the **trusted
authoring system** produced for **this repository and path**, from a recorded source
state. The signature proves who issued the record and that the record matches the
artifact; it does **not** prove the artifact is correct.

A mismatch reads "ownership of the current version not confirmed" — never "deployer is
not responsible".

### 2.2 The authoring set (in the project repository)

Under `.deployer/authoring/`, committed with the artifact, for the Dockerfile only:

- `Dockerfile.snapshot.json` — the **source snapshot**, taken before authoring wrote
  anything:
  - `format_version`;
  - `source_commit` — the commit authoring started from;
  - `tree` — the complete Git tree listing of `source_commit` (`path`, `mode`, `type`,
    `sha` per entry) and `tree_complete: true` (a truncated or partial listing is not
    written);
  - `facts` — the `ProjectFacts` authoring used, all derived from that commit.
  No secrets, no paths or environment of the authoring machine, no LLM responses.
- `Dockerfile.record.json` — `format_version`, `repo` (`owner/name` from `origin`),
  `artifact_path`, `artifact_sha256` (the bytes authoring wrote), `source_commit`,
  `snapshot_sha256` (of the snapshot file's bytes), `deployer_version`. It does **not**
  contain the SHA of the commit that will hold the set (that would be circular).
- `Dockerfile.record.json.sig` — `ssh-keygen -Y sign -n deployer-authoring` over the
  exact bytes of the record.

The whole record is signed; the snapshot is bound to it by `snapshot_sha256`.

### 2.3 Trust, outside the checked repository

- An `allowed_signers` file (OpenSSH format, principal `deployer-authoring`) and an
  optional revoked-keys file for `ssh-keygen -Y verify -r`.
- Default directory `~/.config/deployer/`; `DEPLOYER_TRUST_DIR` overrides it. The
  **resolved real path** must lie outside the repository being checked, symlinks
  followed; otherwise the trust set is refused and ownership is not confirmed.
- Nothing in the checked repository can add a trusted signer.
- Management, minimal: `deployer trust add <pubkey>`, `deployer trust replace <old>
  <new>` (add new, revoke old), `deployer trust revoke <pubkey>`. No automatic
  rotation; its absence does not by itself invalidate a signature.

### 2.4 Verification (diagnosis side, preparation layer)

From the tree restored at `head_sha` (R §1.4), without network or builds:

1. The three files exist and parse; unknown `format_version` → not confirmed.
2. The signature verifies against the trust set (`ssh-keygen -Y verify`, namespace
   `deployer-authoring`, revocation list applied). A missing/invalid signature, an
   unknown or revoked key → not confirmed. The key fingerprint is reported **only**
   after a successful verification.
3. `artifact_sha256` equals the SHA-256 of the Dockerfile bytes at `head_sha`;
   `artifact_path` and `repo` equal the run's.
4. `snapshot_sha256` equals the SHA-256 of the snapshot file's bytes.
5. The snapshot is consistent: supported `format_version`, `tree_complete: true`, a
   well-formed listing (every entry has the four string fields; no duplicate paths),
   and `source_commit` equal in record and snapshot. Hash equality does not replace
   these checks.

Any failure → `unmet: (1) ownership not confirmed: <reason>`.

## 3. Condition (2): the defect catalogue (closed)

### 3.1 `missing_copy_source`

An authored COPY/ADD source that does not exist — with the proof of absence required:

- **Form:** a literal local path inside the closed alphabet of R §3.2 — no glob, no
  `$`, no `--from`, no remote source, no heredoc, no JSON escape. The instruction is not
  `--from`, and the document is fully read (R's `unread_reason` is `None`).
- **Absence in the source snapshot:** no listing entry equals the path, no entry lies
  under it (`path/…`), and no ancestor component of the path is a symlink (`120000`)
  or a submodule (`160000`) in the listing. The listing is complete (§2.4 step 5).
- **Absence at `head_sha`:** the same three conditions against the tree listing R
  fetched for `head_sha`.
- **Not created by a CI preparation step:** guaranteed by `restoration: exact` (R §1.3:
  only inert steps between checkout and the build), which §4 requires.

If the path existed in the snapshot and is gone at `head_sha`, the project changed:
`insufficient_grounds`. A missing file alone proves nothing.

### 3.2 `from_argument_count`

R §3.1 check 2 (`FROM takes one or three arguments`) reports a finding on an
instruction of the Dockerfile whose bytes equal the authored bytes (§2.4 step 3). The
other three syntax checks of R §3.1 are **not admissible** in this slice: they have no
verified template rows (§4.1).

## 4. Condition (3): the link

### 4.1 The template table (closed data)

A row fixes, for one defect class and one backend, a structured diagnostic that
establishes the **instruction**, the **violation** and its **object**. A row is a basis
for admission only once real recordings of **both** sides are verified and pinned (with
builder version where known, origin and fixture path). Synthetic examples may serve
negative tests; they never confirm a backend's format. **Adding a row widens automatic
admission and requires a change to this spec and a review, even when no code changes.**

| id | class | side | shape |
|---|---|---|---|
| `copy-missing/buildkit` | `missing_copy_source` | CI (BuildKit, default Dockerfile frontend) | a single `Dockerfile:<N>` block whose `>>>` lines are one COPY/ADD, and, for that build step, `failed to calculate checksum of ref …: "/<P>": not found` |
| `copy-missing/podman` | `missing_copy_source` | local (Podman/Buildah) | `Error: building at STEP "<COPY …>": checking on sources under "<ctx>": copier: stat: "/<P>": no such file or directory` |
| `from-args/buildkit` | `from_argument_count` | CI | `dockerfile parse error on line <N>: FROM requires either one or three arguments` |
| `from-args/podman` | `from_argument_count` | local | `Error: FROM requires either one argument, or three: …` with no `STEP` line printed |

Recordings (in the committed bundles of R §8.A; §8.1 pins them):

- `copy-missing/*`: `run-1` — CI run 35680991093; local Podman 5.7.0.
- `from-args/*`: `run-5` — CI run 35706782471; local Podman 5.7.0.
- CI builder version: **unknown** — the logs print no Docker/BuildKit version. The
  runner image (`ubuntu-24.04`, `20260907.300.1`) and `docker driver` are recorded as
  provenance context, not as a version. A recording confirms the observed format, not
  compatibility with every version.

### 4.2 Binding rules

- **Object from the template only:** `<P>` is taken from its template position, read
  relative to the build-context root (`/<P>` → `<P>`), normalised. It must equal one
  source of **that** instruction, and that source must be the one §3.1 proved absent.
  Several candidates, an unknown format or an ambiguous normalisation →
  `insufficient_grounds`.
- **Same instruction on both sides:** CI by the `Dockerfile:<N>` block's line span;
  local by the `STEP` text. If the Dockerfile has more than one instruction with the
  same normalised text, the local binding is ambiguous → `insufficient_grounds`.
- **`from-args/podman` states no line:** admission only when the Dockerfile has
  exactly **one** FROM instruction with a wrong argument count, and it is the
  instruction of §3.2's finding. Several candidates or insufficient output →
  `insufficient_grounds`. R's `parser_finding_keyword` binding finds the candidate; it
  is not the proof.
- **Both diagnostics match the same check:** for `from_argument_count`, the CI and
  local diagnostics must both match a `from-args/*` row; a parse error of another kind
  on the same line does not count.
- Message texts need not match literally; the rows define what is compared.

### 4.3 Differences (per class)

The comparison of R §7 must be `reproduced` or `reproduced_with_differences` on the
same span, and:

- `restoration` is **directly** `exact` (not merely equal on both sides).
- `backend` may differ only for a pair covered by verified rows of the same class on
  both sides. A `# syntax=` directive on either side is an unknown dialect → refusal.
- `ignore_file: same` means the **effective** file (by each backend's rule — Podman
  prefers `.containerignore`) is the same path **and** has the same content hash on
  both sides — or is **absent on both sides**.
- `host_arch` and `base_image_digests` in `unknown` are tolerated only **after** the
  defect is proven through the rows; they never replace that proof.
- Any other dimension, or one whose influence is unknown → `insufficient_grounds`.

## 5. The authoring side

### 5.1 When a set is issued

Only when all hold; otherwise authoring proceeds as before, writes **no** set, and warns
"ownership will not be confirmable":

- the project is a Git checkout with an `origin` remote;
- the working tree is clean **before authoring writes anything** — staged, unstaged and
  untracked changes all count, with no exception for files authoring is about to
  write (a hand-edited Dockerfile, compose or workflow is never silently allowed);
- a signing key is configured (`--signing-key` or `DEPLOYER_SIGNING_KEY`);
- the facts come only from `source_commit`: if a fact depends on an ignored or
  otherwise uncommitted file, no set is issued.

### 5.2 Order

1. Record `source_commit` and build the snapshot and facts — before any change.
2. Author the artifacts as today.
3. Ensure `.deployer/` is excluded from the build context: add it to the effective CI
   ignore file (Dockerfile-specific, else root `.dockerignore`, creating it if absent)
   and to `.containerignore` if one exists; then verify, with the same matcher the
   diagnosis uses (R §3.2), that **every** file of the prospective set — record,
   signature, snapshot — is excluded by both effective files. Not proven (e.g. an
   unmodelled pattern, a re-including negation) → no set, with the reason.
4. Build the record from the bytes authoring wrote and the snapshot it built, sign it.
   Authoring signs only its own result; there is no "sign this record" entry point.
5. Publish the set atomically (write to temporary names, then rename all three); a
   partially written set must never look complete.

### 5.3 Re-authoring

A new authoring run never leaves an old confirmation behind: if it does not issue a new
set for the Dockerfile, it removes the previous set with a warning.

## 6. Diagnosis integration

### 6.1 Two layers

- **Preparation** (I/O, no network, no builds): read the set from R's restored
  `source/`, verify the signature and hashes (§2.4), load the `head_sha` tree listing,
  read R's reproduction section and the try's evidence files. It produces **verified
  facts**.
- **Decision** (a pure function): verified facts → the `admission` section. No I/O.

### 6.2 The `admission` section

Added by `deployer diagnose --reproduce` when R's reproduction reached `attempted`
(otherwise the section may be absent). Verdict schema **1.3**, additive over R's 1.2
(1.3 is unused in the verdict schema; the snapshot schema's own 1.3 is a different
number).

- `verdict`: `admitted` | `insufficient_grounds`;
- `binding`: `repo`, `head_sha`, `artifact_path`, `artifact_sha256` — the state the
  admission holds for;
- `ownership`: `confirmed` | `not_confirmed`, reason; `key_fingerprint` only after a
  successful verification; `record_sha256`, `snapshot_sha256`;
- `defect` (optional): `class`, instruction (file, lines), object (source path or the
  FROM instruction);
- `link` (optional): per side the matched row id, the extracted object and a typed
  evidence reference into the try directory (R §6); the differences considered with
  the decision for each;
- `unmet`: list of `{condition: 1|2|3, reason}` — empty iff `admitted`.

The exit code is unchanged, including R's two exit-2 cases. Admission is read from the
document only.

## 7. The consumer contract

`deployer.admission.accept_for_fix(document, target) -> Accepted | Refused` is the
single entry point `ci-fix-authoring` will use. It refuses when:

- the `admission` section is absent, malformed, of an unknown schema version, or has an
  unknown `verdict`;
- the verdict is `insufficient_grounds`;
- the `binding` (repo, `head_sha`, path, artifact hash) differs from `target`;
- a referenced evidence file is missing or unreadable.

The consumer never re-derives admission from logs.

## 8. Acceptance — offline, no live runs, nothing paid

### 8.1 Template records

For each row of §4.1, a pinned record of both sides: the fixture path in the committed
bundle, run id, date, local builder version, and the CI builder version stated as
`unknown` with the runner image as context. Unit tests match every row against these
real recordings; synthetic variants appear only in negative tests.

### 8.2 Positive cases (derived, not historical)

`admit-run-1` and `admit-run-5`: derived from the committed `run-1` / `run-5` bundles,
plus an `.deployer/authoring/` set **created for the test** with a test key pair (public
key in a test trust directory outside the bundle tree). `PROVENANCE.md` separates the
two sources explicitly: the real logs confirm the diagnostic format; the signed set is
test-made and claims no historical authorship. For `run-1` the snapshot listing lacks
`docs/setup.md`. Both yield `admitted`. The existing bundles stay unchanged; the new
ones follow R §8's provenance and checksum discipline.

**End-to-end check:** authoring (with a fake author, no build) issues a set in a
temporary Git repository → the preparation layer verifies it → ownership confirmed.

### 8.3 Negative cases — one violated condition each

Each case violates exactly one condition, re-signing with the test key where needed so
the check reaches that condition (e.g. a different `source_commit` needs a recomputed
`snapshot_sha256` and a re-signed record). Each expects `insufficient_grounds` with the
named `unmet` entry.

| Group | Cases |
|---|---|
| Ownership | no set; invalid signature; unknown key; revoked key; artifact hash mismatch (hand edit); `snapshot_sha256` mismatch; `source_commit` differs between record and snapshot; incomplete listing; foreign `repo` or path; unknown `format_version` |
| Defect | source present in the snapshot (project change); source a glob / `$VAR` / `--from`; an ancestor is a symlink or submodule; a syntax finding other than FROM arguments |
| Link | CI output matching no row; object path ≠ the instruction's source; two identical COPY instructions; two FROM candidates; `# syntax=` on either side; `ignore_file` differs by path or content; `approximation`; an unknown dimension |

### 8.4 Authoring behaviour

Tested as authoring behaviour (warning, no new set, old set removed), not as a verdict:
dirty tree (staged / unstaged / untracked, including a hand-edited Dockerfile); a fact
from an uncommitted file; no signing key; exclusion not provable; re-authoring without a
new set removes the old one; trust directory inside the repo, directly or via a symlink.
The diagnosis of the resulting trees is checked separately (§8.3).

### 8.5 Consumer

`accept_for_fix` refuses: no `admission`; malformed section; unknown version; unknown
verdict; `insufficient_grounds`; `binding` ≠ target; a missing evidence file. It accepts
only `admit-run-1` / `admit-run-5` against their own targets.

## 9. Decisions (owner, 2026-09-24)

- Purpose: gate into `ci-fix-authoring`; one positive verdict; no ENVIRONMENT/PROJECT,
  no routing.
- Ownership: current bytes equal a signed authoring record; trust outside the repo;
  signed via `ssh-keygen -Y sign`; add/replace/revoke only.
- Storage: `.deployer/authoring/` in the project repo, read from the tree at `head_sha`;
  excluded from the build context by authoring, verified.
- Catalogue: `missing_copy_source` and `from_argument_count` only.
- Link: closed, recording-backed templates; `exact` required; differences allowed per
  class by an explicit list; unknown → refusal.
- Exit code unchanged; admission read from the document only; absent or invalid
  admission means no admission.
