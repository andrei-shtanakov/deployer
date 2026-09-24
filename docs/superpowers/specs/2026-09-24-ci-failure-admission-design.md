# CI-failure admission — design ("proven deployer artifact defect")

**Status:** DRAFT rev 2.1. Designed with the owner on 2026-09-24 (four sections, each
approved with refinements); revised after the targeted consistency review of rev 1 at
`51b6507` (`../../../../_cowork_output/deployer-admission-spec-targeted-review-2026-09-24.md`,
a dev-only workspace file; every point it raised is resolved in this text). Next: the
owner's review, then a plan. No code exists for this design.
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
  an instruction (§4) may be part of the evidence; free text may not.
- Defect classes beyond the two of §3. A new class is a new spec.
- Authoring records for artifacts other than the Dockerfile.
- Automatic key rotation.

## 1. The verdict

`admission.verdict` is exactly one of:

- `admitted` — `ownership` is `confirmed`, `defect` and `link` are present and complete
  (§6.2), and `unmet` is empty.
- `insufficient_grounds` — `unmet` lists **at least one** concrete unmet or
  unconfirmed condition, each tagged with its condition number and a reason. `defect`,
  `link` and any value that could not be obtained (e.g. a hash of a file that does not
  exist) are absent: no evidence is invented to fill the schema.

A section that breaks these invariants is **malformed** (§7). When in doubt,
`insufficient_grounds`: a false admission is worse than a false refusal.

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

- `Dockerfile.current` — a one-line pointer naming the published set directory
  (`Dockerfile/<record_sha256>`). It is the **only** entry point: a set is published
  when, and only when, the pointer names it (§5.2).
- `Dockerfile/<record_sha256>/snapshot.json` — the **source snapshot**, taken before
  authoring wrote anything:
  - `format_version`;
  - `source_commit` — the commit authoring started from;
  - `tree` — the complete Git tree listing of `source_commit` (`path`, `mode`, `type`,
    `sha` per entry) and `tree_complete: true` (a truncated or partial listing is not
    written);
  - `facts` — the `ProjectFacts` authoring used, all derived from that commit.
  No secrets, no paths or environment of the authoring machine, no LLM responses.
- `Dockerfile/<record_sha256>/record.json` — `format_version`, `repo` (`owner/name`
  from `origin`), `artifact_path`, `artifact_sha256` (the bytes authoring wrote),
  `source_commit`, `snapshot_sha256` (of the snapshot file's bytes),
  `deployer_version`. It does **not** contain the SHA of the commit that will hold the
  set (that would be circular).
- `Dockerfile/<record_sha256>/record.json.sig` — `ssh-keygen -Y sign -n
  deployer-authoring` over the exact bytes of the record.

The whole record is signed; the snapshot is bound to it by `snapshot_sha256`; the
directory name is the record's own SHA-256.

### 2.3 Trust, outside the checked repository

- An `allowed_signers` file (OpenSSH format, principal `deployer-authoring`) and an
  optional revoked-keys file for `ssh-keygen -Y verify -r`.
- Default directory `~/.config/deployer/`; `DEPLOYER_TRUST_DIR` overrides it. The
  **resolved real path** must lie outside the repository being checked, symlinks
  followed; otherwise the trust set is refused and ownership is not confirmed. This is
  a diagnosis-side check (§2.4 step 0); authoring does not read the trust set.
- Nothing in the checked repository can add a trusted signer.
- Management, minimal: `deployer trust add <pubkey>`, `deployer trust replace <old>
  <new>` (add new, revoke old), `deployer trust revoke <pubkey>`. No automatic
  rotation; its absence does not by itself invalidate a signature.

### 2.4 Verification (diagnosis side, preparation layer)

From the tree restored at `head_sha` (R §1.4), without network or builds, in order;
the first failing step ends the check with its reason:

0. The trust directory's real path lies outside the checked repository (§2.3).
1. `Dockerfile.current` exists, names `Dockerfile/<hex64>`, and that directory holds
   the three files; each parses; `format_version` of record and snapshot is supported.
2. The directory name equals the SHA-256 of `record.json`'s bytes (a pointer to a
   half-replaced directory cannot pass).
3. The signature verifies against the trust set (`ssh-keygen -Y verify`, namespace
   `deployer-authoring`, revocation list applied). A missing/invalid signature, an
   unknown or revoked key → not confirmed. The key fingerprint is reported **only**
   after a successful verification.
4. `artifact_sha256` equals the SHA-256 of the Dockerfile bytes at `head_sha`;
   `artifact_path` and `repo` equal the run's.
5. `snapshot_sha256` equals the SHA-256 of `snapshot.json`'s bytes.
6. The snapshot is consistent: `tree_complete: true`, a well-formed listing (every
   entry has the four string fields; no duplicate paths), and `source_commit` equal in
   record and snapshot. Hash equality does not replace these checks.

Any failure → `unmet: (1) ownership not confirmed: <step>: <reason>`.

## 3. Condition (2): the defect catalogue (closed)

### 3.1 `missing_copy_source`

An authored COPY/ADD source that does not exist — with the proof of absence required:

- **Form:** a literal local path inside the closed alphabet of R §3.2 — no glob, no
  `$`, no remote source, no heredoc, no JSON escape — on a COPY/ADD without `--from`,
  in a fully read document (R's `unread_reason` is `None`).
- **Absence in the source snapshot:** no listing entry equals the path, no entry lies
  under it (`path/…`), and no ancestor component of the path is a symlink (`120000`)
  or a submodule (`160000`) in the listing. The listing is complete (§2.4 step 6).
- **Absence at `head_sha`:** the same three conditions against the `head_sha` tree
  listing R stored in `source.json`.
- **Not created by a CI preparation step:** guaranteed by `restoration: exact` (R §1.3:
  only inert steps between checkout and the build), which §4.3 requires.

If the path existed in the snapshot and is gone at `head_sha`, the project changed:
`insufficient_grounds`. A missing file alone proves nothing.

### 3.2 `from_argument_count`

R §3.1 check 2 (`FROM takes one or three arguments`) reports a `failed` finding on an
instruction of the Dockerfile whose bytes equal the authored bytes (§2.4 step 4). The
other three syntax checks of R §3.1 are **not admissible** in this slice: they have no
verified template rows (§4.1).

## 4. Condition (3): the link

### 4.1 The template table (closed data)

A row fixes, for one defect class and one backend, a structured diagnostic that
establishes the **violation** and — where the builder prints one — its **object**. A
row is a basis for admission only once real recordings of **both** sides are verified
and pinned (with builder version where known, origin and fixture path). Synthetic
examples may serve negative tests; they never confirm a backend's format. **Adding a
row widens automatic admission and requires a change to this spec and a review, even
when no code changes.**

| id | class | side | shape |
|---|---|---|---|
| `copy-missing/buildkit` | `missing_copy_source` | CI (BuildKit, default Dockerfile frontend) | a single `Dockerfile:<N>` block whose `>>>` lines are one COPY/ADD, and, for that build step, `failed to calculate checksum of ref …: "/<P>": not found` |
| `copy-missing/podman` | `missing_copy_source` | local (Podman/Buildah) | `Error: building at STEP "<COPY …>": checking on sources under "<ctx>": copier: stat: "/<P>": no such file or directory` |
| `from-args/buildkit` | `from_argument_count` | CI | a line containing `dockerfile parse error on line <N>: FROM requires either one or three arguments` |
| `from-args/podman` | `from_argument_count` | local | `Error: FROM requires either one argument, or three: …` with no `STEP` line printed |

Recordings (in the committed bundles of R §8.A; §8.1 pins them):

- `copy-missing/*`: `run-1` — CI run 35680991093; local Podman 5.7.0.
- `from-args/*`: `run-5` — CI run 35706782471; local Podman 5.7.0.
- CI builder version: **unknown** — the logs print no Docker/BuildKit version. The
  runner image (`ubuntu-24.04`, `20260907.300.1`) and `docker driver` are recorded as
  provenance context, not as a version. A recording confirms the observed format, not
  compatibility with every version.

### 4.2 Binding rules, per class

**`missing_copy_source`:**

- **Instruction:** CI by the single `Dockerfile:<N>` block's line span; local by the
  `STEP` text of the row. If the Dockerfile has more than one instruction with the same
  normalised text, the local binding is ambiguous → `insufficient_grounds`.
- **Object from the row only:** `<P>` is taken from its row position, read relative to
  the build-context root (`/<P>` → `<P>`), normalised. It must equal one source of
  **that** instruction, and that source must be the one §3.1 proved absent. Several
  candidates or an ambiguous normalisation → `insufficient_grounds`.

**`from_argument_count`:** the builders print no object.

- **Instruction (CI):** the parse-error line `<N>` of `from-args/buildkit`.
- **Instruction (local):** `from-args/podman` states no line: admission only when the
  Dockerfile has exactly **one** FROM with a wrong argument count, and it is the
  instruction at `<N>`. Several such FROMs or insufficient output →
  `insufficient_grounds`. R's `parser_finding_keyword` binding locates the candidate;
  it is not the proof.
- **Object:** that FROM instruction itself (file, lines, text), taken from the
  Dockerfile once both sides are bound to it.
- **Same check on both sides:** both diagnostics must match a `from-args/*` row; a
  parse error of another kind on the same line does not count.

**Both classes:** an unknown format on either side (no row matches) →
`insufficient_grounds`. Message texts need not match literally; the rows define what
is compared.

### 4.3 Differences (per class)

The comparison of R §7 must be `reproduced` or `reproduced_with_differences` on the
same span, and:

- `restoration` is **directly** `exact` (not merely equal on both sides).
- `backend` may differ only for a pair covered by verified rows of the same class on
  both sides. A `# syntax=` directive on either side is an unknown dialect → refusal.
- `ignore_file: same` means the **effective** file (by each backend's rule — Podman
  prefers `.containerignore`) is the same path **and** has the same content hash on
  both sides — or is **absent on both sides**. The preparation layer computes both
  content hashes from the restored tree (R records only paths).
- `host_arch` and `base_image_digests` in `unknown` are tolerated only **after** the
  defect is proven through the rows; they never replace that proof.
- Any other dimension, or a required one whose influence is unknown →
  `insufficient_grounds`.

## 5. The authoring side

### 5.1 When a set is issued

Only when all hold; otherwise authoring proceeds as before, publishes **no** set, and
warns "ownership will not be confirmable":

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
   diagnosis uses (R §3.2), that **every** file of the prospective set — pointer,
   record, signature, snapshot — is excluded by both effective files. Not proven (e.g.
   an unmodelled pattern, a re-including negation) → no set, with the reason.
4. Build the record from the bytes authoring wrote and the snapshot it built, sign it.
   Authoring signs only its own result; there is no "sign this record" entry point.
5. Publish. **Set directories are immutable once written:**
   - if `Dockerfile/<record_sha256>/` does not exist, write the three files into a
     temporary sibling directory and rename it into place;
   - if it already exists (the same record issued again), it is **never written to**:
     it is reused only if its `record.json` and `snapshot.json` are byte-identical to
     the ones just built and its `record.json.sig` verifies against the **public half
     of the signing key in use** (authoring does not read the diagnosis trust set,
     §2.3); otherwise no set is published, with the reason;
   - then replace `Dockerfile.current` in **one** atomic rename, and only then remove
     other `Dockerfile/<…>/` directories.

   This guarantees the pointer never names a partially written directory. It does
   **not** guarantee that the Dockerfile is confirmed after an interruption: step 2 has
   already rewritten the Dockerfile, so an interruption before the pointer rename leaves
   an intact previous set whose signature and internal hashes still verify, but whose
   `artifact_sha256` no longer matches the new bytes — ownership of the current version
   is then **not confirmed** (§2.4 step 4), which is the honest answer.

### 5.3 Re-authoring

On normal completion, a new authoring run never leaves an old confirmation behind: if
it does not issue a new set for the Dockerfile, it removes `Dockerfile.current` first,
then the set directories, with a warning. After an abnormal interruption this removal
is not guaranteed; a stale pointer can then remain, and §2.4 step 4 still refuses to
confirm a Dockerfile whose bytes changed.

## 6. Diagnosis integration

### 6.1 Two layers

- **Preparation** (I/O, no network, no builds): run §2.4; load the `head_sha` listing
  from R's `source.json`; read R's reproduction section; compute the effective ignore
  files' content hashes (§4.3); and write the CI job text, already held in the
  snapshot (R's `shape.job_text`), to `ci.log` in the try directory so CI evidence has
  a file to reference like the local side's `build.stdout`/`build.stderr`. It produces
  **verified facts**.
- **Decision** (a pure function): verified facts → the `admission` section. No I/O.

### 6.2 The `admission` section

Added by `deployer diagnose --reproduce` when R's reproduction reached `attempted`
(otherwise the section is absent). Verdict schema **1.3**, additive over R's 1.2 (1.3
is unused in the verdict schema; the snapshot schema's own 1.3 is a different number).

- `verdict`: `admitted` | `insufficient_grounds`;
- `binding` (always): `repo`, `head_sha`, `artifact_path`, and `artifact_sha256` of the
  bytes at `head_sha` — the state the admission holds for;
- `ownership` (always): `status` `confirmed` | `not_confirmed`; `reason` when not
  confirmed; `key_fingerprint`, `record_sha256`, `snapshot_sha256` only when their
  value was obtained (the fingerprint only after a successful verification);
- `defect` (required when `admitted`): `class`; instruction (file, lines); object —
  the source path for `missing_copy_source`, the FROM instruction for
  `from_argument_count`;
- `link` (required when `admitted`): per side, the matched row id, the extracted object
  (`missing_copy_source` only), and a typed evidence reference — `ci.log` or
  `build.stdout`/`build.stderr` in the try directory (R §6 path bases) with the line
  numbers of the matched diagnostic; plus each difference considered and its decision;
- `unmet`: list of `{condition: 1|2|3, reason}` — empty iff `admitted`.

The exit code is unchanged, including R's two exit-2 cases. Admission is read from the
document only.

## 7. The consumer contract

`deployer.admission.accept_for_fix(document, try_dir, target) -> Accepted | Refused`
is the single entry point `ci-fix-authoring` will use. It refuses when:

- the `admission` section is absent, of an unknown schema version, or has an unknown
  `verdict`;
- the section is **malformed**: any §1 / §6.2 invariant broken (e.g. `admitted` with
  `ownership` not `confirmed`, without complete `defect`/`link`, or with a non-empty
  `unmet`; `insufficient_grounds` with an empty `unmet`);
- the verdict is `insufficient_grounds`;
- the `binding` (repo, `head_sha`, path, artifact hash) differs from `target`;
- a referenced evidence file is missing or unreadable, or its referenced lines are out
  of range.

The consumer never re-derives admission from logs.

## 8. Acceptance — offline, no live runs, nothing paid

Two test levels, named per case:

- **P (pipeline):** a committed bundle replayed through R and admission against fakes,
  as R §8.A does.
- **D (decision):** the pure decision function fed hand-built verified facts, for rules
  R's pipeline refuses earlier and would otherwise hide.

A case names its target condition and expected reason. Where conditions depend on each
other, a case is **one targeted mutation with its expected refusal**, not a claim that
no other condition is touched.

### 8.1 Template records

For each row of §4.1, a pinned record of both sides: the fixture path in the committed
bundle, run id, date, local builder version, and the CI builder version stated as
`unknown` with the runner image as context. Unit tests match every row against these
real recordings (quoted in the targeted review: `run-1/snapshot.json`, `run-1/local.*`,
`run-5/snapshot.json`, `run-5/local.*`); synthetic variants appear only in negative
tests.

### 8.2 Positive cases (derived, not historical)

`admit-run-1` and `admit-run-5` (level P): derived from the committed `run-1` / `run-5`
bundles, plus an `.deployer/authoring/` set **created for the test** with a test key
pair (public key in a test trust directory outside the bundle tree). `PROVENANCE.md`
separates the two sources explicitly: the real logs confirm the diagnostic format; the
signed set is test-made and claims no historical authorship. For `run-1` the snapshot
listing lacks `docs/setup.md`. Both yield `admitted`. The existing bundles stay
unchanged; the new ones follow R §8's provenance and checksum discipline.

**End-to-end check:** authoring (with a fake author, no build) issues a set in a
temporary Git repository → the preparation layer verifies it → ownership confirmed.

### 8.3 Negative cases, condition by condition

Re-signing with the test key where needed so the mutation reaches its step.

| Condition | Case | Level | Mutation → expected `unmet` |
|---|---|---|---|
| (1) step 0 | trust dir inside the repo; via a symlink | P | trust dir resolves into the checked repo → trust refused |
| (1) step 1 | no set; pointer names a missing directory; unknown `format_version` in the record; in the snapshot | P | → the named step-1 reason |
| (1) step 2 | directory name ≠ record hash (half-replaced) | P | → step 2 |
| (1) step 3 | well-formed but invalid signature; unknown key; revoked key | P | → step 3 |
| (1) step 4 | Dockerfile hand-edited after authoring (record unchanged); foreign `repo`; foreign `artifact_path` (re-signed) | P | → step 4 |
| (1) step 5 | snapshot edited, still parseable, record unchanged | P | → step 5 |
| (1) step 6 | `source_commit` differs (snapshot hash recomputed, re-signed); `tree_complete: false`; an entry missing a field; a non-string field; a duplicate path | P | → step 6 |
| (2) form | source `docs/*.md` (glob, not reaching `.git`); `COPY --from=x`; remote `ADD https://…`; heredoc COPY; JSON form with an escape; non-default `# escape=` | D | → form not admissible (the glob, heredoc and escape cases would also be caught earlier by R in P) |
| (2) form | `COPY $VAR` | P | → R makes it an approximation first: expected `(3) restoration not exact` (documents the early refusal) |
| (2) absence | path present in the snapshot (project change); an entry `path/…` in the snapshot; path present at `head_sha`; an ancestor is a symlink / a submodule in the snapshot; the same in the `head_sha` listing | D | → absence not proven, naming the listing |
| (2) syntax | a `failed` finding of another R §3.1 check | D | → check not admissible |
| (3) rows | CI output matching no row, structural block kept; local output matching no row | D | → unknown format, naming the side |
| (3) object | row object ≠ the instruction's source; two candidates for the object | D | → object not bound |
| (3) instruction | two identical COPY instructions | D | → local binding ambiguous (P expectation: R's comparison is already `inconclusive`) |
| (3) instruction | two FROMs with a wrong argument count | D | → several candidates (P expectation: R `inconclusive`) |
| (3) same check | a parse error of another kind on the FROM line | D | → not the same check |
| (3) differences | `# syntax=` on either side (on the `run-1` COPY case) | P | → unknown dialect |
| (3) differences | effective ignore file differs by path (`.containerignore` present); same path, different content | D | → `ignore_file` not same |
| (3) differences | `restoration: approximation`; backend pair without verified rows; a required dimension `unknown` other than `host_arch`/`base_image_digests` | D | → named difference not allowed |

### 8.4 Authoring behaviour

Tested as authoring behaviour (warning, no new set, old set removed), not as a verdict:
not a Git checkout; no `origin` remote; dirty tree (staged / unstaged / untracked,
including a hand-edited Dockerfile); a fact from an uncommitted file; no signing key;
exclusion not provable; re-authoring without a new set removes the old one (pointer
first). **Interrupted publication:**
- a previous set exists, the Dockerfile has been rewritten and the new directory
  written, but the pointer not yet renamed → the previous set's signature and internal
  hashes still verify, and ownership is **not confirmed** by `artifact_sha256`
  mismatch (§2.4 step 4);
- the pointer renamed but old directories not yet removed → the new set verifies and
  ownership is confirmed.

**Re-issuing the same record:** a second authoring run producing byte-identical output
(same `record_sha256`) finds the published directory, verifies and reuses it without
writing into it, and leaves the pointer unchanged; if that existing directory has been
tampered with, no set is published and the reason is reported. The diagnosis of the
resulting trees is checked separately (§8.3).

### 8.5 Consumer

`accept_for_fix` refuses: no `admission`; unknown version; unknown verdict;
`insufficient_grounds`; each §7 invariant violation (`admitted` with ownership not
confirmed; `admitted` without `defect` or `link`; `admitted` with a non-empty `unmet`;
`insufficient_grounds` with an empty `unmet`); `binding` ≠ target; a missing evidence
file; an unreadable one; referenced lines out of range. It accepts only `admit-run-1` /
`admit-run-5` against their own targets.

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
