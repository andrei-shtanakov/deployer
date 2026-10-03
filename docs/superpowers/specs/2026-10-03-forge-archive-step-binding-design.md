# Forge archive step binding — design ("bind a step's output only where the runner's own files prove it")

**Status:** DRAFT rev 2.2. Designed with the owner on 2026-10-03: approach A was chosen,
then six refinements were applied before writing (§3.3, §4, §5.2, §6, §7, §9.3). Rev 2
resolves the external review of rev 1 at `eadf587`
(`../../../../_cowork_output/deployer-forge-archive-step-binding-spec-review-2026-10-03.md`,
a dev-only workspace file: R1 the one-sided BOM in the boundary check, §4.3; R2
undecodable step files before ownership, §4.2; R3 a bad-numbering fixture that could
not reach its refusal, §9.2; and the capped subprocess lifecycle, §7.2). Next: the
owner checks the rev 2 diff, then a plan. Rev 2 was approved for planning; rev 2.1
makes discovery of step entries independent of name validation (§4.2), the review's
non-blocking detail 2. Rev 2.2 adds two contract points from the plan review: a runner
without the capped download attempts nothing (§8), and the boundary of what a corrupt
archive is detected by (§7.3). No code exists for this design.
**Item:** `todo://deployer/forge-step-level-log-binding`.
**Base:** `master` @ `d612ad6`. The schema 1.4 baseline (#113): no job-log block is bound
to a step. The recording `steps-1` (#112): `tests/fixtures/step-binding/`, with
`PROVENANCE.md` as **P** and `steps-1/observed.json` as **O**.

## Purpose

Today `diagnose` cites a failed step's own output as job-level evidence:
`source=None`, together with the note "cited evidence is job-level". #113 removed the
only binding the job log offered, because a step's own output can forge the runner's
group headers (P, `s5-spoof`). The job log alone carries no boundary that its steps
cannot print.

The per-attempt log archive (`actions/runs/{id}/attempts/{n}/logs`) can carry one.
When it holds per-step files, the runner wrote one file per step: `<dir>/<N>_<name>.txt`,
where `N` is the API step number. The step's output can make the text inside a file
anything at all; it cannot move where that file starts or ends. This design binds a log
segment to its `StepRef` only when that file structure is proven to belong to the job
whose log forge read. In every other case it keeps today's job-level evidence unchanged.

The guarantee is about the **container**: which step a stretch of log lines belongs to.
It says nothing about what those lines say.

## Non-goals

- Explaining why some archives have per-step files and others do not (P: two earlier
  runs had none). Whether they are present is a property of one response, observed
  each time.
- `read_attempt`, `reproduce` and `fix`. They read `job_text`, which this design keeps
  byte-identical (§5.3), and `fix/ci_eval` keeps its own section reader.
- Using the archive's text as evidence. The evidence text stays the job log that forge
  reads today (approach A, §2).
- "Nothing was fetched" as a `Completeness` state of its own (a sibling TODO).
- Binding without per-step files: by timestamps, by group headers or by order. Each
  one is either forgeable (P) or ambiguous to the second (the API's step times).

## 1. Scope and identity

- Only `fetch_failed_run` (the diagnose path) reads the archive, once per run, after
  the attempt is fixed. Every request names that attempt:
  `actions/runs/{id}/attempts/{n}/jobs` and `actions/runs/{id}/attempts/{n}/logs`.
  Each job log is read by a `job_id` from that same listing.
- A `StepRef` is always `(job_id from this attempt's listing, N)`, and `N` is checked
  only inside the job whose ownership is already established (§4).
- **Testable guarantee against mixing attempts:** every request carries the chosen
  attempt, and every `StepRef` uses that attempt's job ids. Comparing text detects an
  archive from another attempt only when the attempts' texts differ. If two attempts
  printed identical text, comparison cannot detect a swapped archive. The guarantee then
  rests on how the requests are built, and the tests assert those requests (§9.2).

## 2. Approach

**A — chosen: the archive gives boundaries over the job log.** Ownership is proven by
content (§4). The step files' line counts are then laid over the job log's own lines,
and each segment's blocks get `StepRef(job_id, N)`. Evidence text, `job_text`, the
order of evidence and every coordinate downstream stay those of the job log.

- B — rejected: use the archive's text as evidence. That is a second rendition, with
  different timestamps (P), and `diagnose` would no longer read the same text as
  `fix confirm`'s live job log.
- C — rejected: segment the job log and use the archive only to confirm. The job log's
  headers are forgeable (P, `s5`), so C adds a forgeable step that the archive makes
  unnecessary.

## 3. Comparing texts

### 3.1 Lines

Both sides are compared as `str`. The job log is the `str` forge already holds: `gh`'s
output decoded as UTF-8 with `errors="replace"`, exactly as `_Gh.logs` reads it today.
A step file is decoded as strict UTF-8; failing that makes the archive `refused`, since
no owner can be named for it yet (§4.2).
A text is split on `\n` only. When the text ends with `\n`, the final empty element is
not a line. One BOM (U+FEFF) at the very start of the text is dropped before splitting:
once per step file and once per job log. A BOM anywhere else is content.

### 3.2 The one normalisation

From each line, the runner timestamp prefix is removed when the line starts with one:
`^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z ` (exactly one space after it). Nothing else is
normalised. ANSI sequences, a trailing `\r`, other whitespace and replacement characters are
compared character for character.

Why this and no more: P shows that the two renditions differ in exactly the BOM and the
timestamps (the timestamps by fractions of a millisecond). Removing anything else could
merge two texts that differ in content, and that would weaken ownership.

### 3.3 Equality

A directory's text is the concatenation of its step files in increasing `N`. Each file
contributes its line list from §3.1–§3.2, and every step file must end with `\n` (§4.3).
A directory matches a job when that line list equals the job log's line list, element
for element. Whether the job log ends with `\n` does not matter: §3.1 removes
the final empty element. That is the only difference in line endings this rule absorbs.

## 4. Proving ownership

The order of steps is fixed. Uniqueness is decided before anything is excluded.

### 4.1 The population

The ownership question is "which job's log is this directory's text". It can only be
answered against **every** job of the attempt that ran, not only the failed jobs whose
logs forge reads today. A green job's unread log could be identical to a failed job's,
and then one directory would match both.

So, when the archive is `available` (§6), forge reads the logs of the attempt's other
jobs too, through the same `_Gh.logs`. This happens only then, so an archive without
per-step files costs no extra requests. A job whose conclusion is `skipped` never ran
and has no log; it is outside the population. If any job of the population is not
`present`, so its text is unknown, uniqueness is unprovable for the whole attempt.
Every job then reads `unverifiable` (§6) and nothing is bound. Restricting the
guarantee to the logs that happened to be read is not done silently, because it is
not done at all.

The extra logs are used only for this decision. Green jobs are not kept and add no
evidence, exactly as today.

### 4.2 Matching first

**Discovery is separate from validation.** Every top-level directory of the archive
(the first path component of any entry that has one) is examined. Its entries other
than `<dir>/system.txt` are its **step entries**. A directory with step entries is a
step directory; a directory holding only `system.txt` is not one. Top-level files
(`<i>_<name>.txt`) are ignored. No name pattern decides which entries count: a pattern
used to discover entries would silently drop the malformed names that the next rule
must see.

**Before ownership, every step directory must be readable.** No owner can be named for
a directory whose text cannot be built. Dropping that directory would let another one
look unique, which resolves uncertainty by exclusion. So any of the following makes the
whole archive `refused` and leaves every job unbound:

- a step entry whose name is not `<N>_<rest>.txt` with `N` canonical decimal: `0`, or a
  nonzero digit followed by digits. `03` and `+3` are not canonical, and neither is a
  name without `<N>_`. A directory whose only step entries are such names refuses the
  archive too: it is not quietly skipped as "no step directory";
- two entries in one directory with the same `N`, which would leave the concatenation
  order undefined;
- a step file that is not valid UTF-8.

Each directory's text (§3.3) is then compared with each population job's log. The
result is the full match relation, computed before any structural check.

A pair `(dir, job)` is a candidate only when `dir` matches exactly this one job and
this job matches exactly this one directory. Any other match makes **every** job it
touches `ambiguous`:

- one job with two matching directories;
- one directory matching two jobs, for example identical texts.

A job with no matching directory is `unmatched`. Directory names, entry order and
top-level file names prove nothing. They are never used to choose between candidates.

### 4.3 Structure, after matching

Each candidate pair is checked. If any check fails, the job is `malformed`. It is never
re-matched to another directory, and a directory rejected here frees nothing for
another job.

- Every `N` is a step number of this job in the API listing, and the job's API step
  numbers are themselves unique. A listing that repeats a number makes the job
  `malformed` (as in `ci_eval`'s ruling AB).
- Every step file ends with `\n`. Without it, the file's last line could merge with the
  next file's first line, and the boundary would be ambiguous.
- The job log's line boundaries agree with forge's own reading. Forge splits blocks with
  `str.splitlines`, which also breaks on `\r` alone, `\x0b`, `\x0c`, `\x1c`–`\x1e`,
  `\x85`, U+2028 and U+2029. The check compares **one view on both sides**: the job log
  with its leading BOM dropped (§3.1), split by `splitlines`, must equal the same
  BOM-dropped text split on `\n` (§3.1), with one trailing `\r` removed from each line.
  Dropping the BOM moves no line index, because the BOM sits inside line 0. So line `i`
  of this view is line `i` of forge's blocks. The view exists only for this check:
  evidence text, `_split_blocks` and the separate BOM item (`forge-log-leading-bom`) are
  untouched. On any difference, line `i` of the comparison is not line `i` of the
  evidence, and the job is `malformed` (`ci_eval` refuses the same breaks, ruling N2).
  Every job log in `steps-1` starts with a BOM, and all six pass this check; the rev 1
  form, with the BOM dropped on one side only, failed all six.

A pair that passes every check is `bound`.

### 4.4 Partial binding

None. Equality of the concatenation proves a job's whole partition at once, and no
independent proof exists for a single step. A job is bound completely or not at all.

## 5. Evidence

### 5.1 Blocks first, then boundaries

Forge's blocks are computed **exactly as today**, by `_split_blocks` over the whole job
log. They have the same lines, the same block edges, and the same dropping of
blank-only blocks. For a `bound` job, the step boundaries from §4 are then laid over
the job log's line indices. A block that crosses a boundary is cut at it. Each piece
keeps **all** of its lines, blank ones included: a piece is never dropped. Each piece
gets `source=StepRef(job_id, N)` for the file its lines come from.

`_split_blocks` must therefore report each block's job-log line indices. That is a
refactor of how it returns blocks, not of how it splits them.

### 5.2 Where evidence lives

All log evidence stays in `FailedJob.evidence`, in log order. Each stretch of text
appears exactly once: bound pieces carry their `StepRef`, and everything else is
`source=None`. `FailedStep.evidence` stays empty for the log, and annotations are
unchanged (`source=job_id`). `diagnose._evidence_pool` already gives a step verdict the
job evidence whose source is that step or no step, so `diagnose` itself does not
change. A verdict whose cited evidence is all step-bound loses the "job-level" note.
Unbound blocks still reach every step's pool, as today.

### 5.3 The `job_text` invariant

For one job log, `shape.job_text` is **byte-identical** whether the job is bound or the
archive is absent, refused or unavailable. This holds by construction. `job_text` joins
block texts with `\n`, a block's text is its lines joined with `\n`, and the pieces of a
cut block are consecutive and non-empty, with every line kept. Joining the pieces gives
back the original block exactly. `job_text` therefore never depends on `source`, and
neither do the coordinates in `fix` or in admission.

Tests pin the invariant directly, not only through recordings. A job log run through
both paths (bound, and without the archive) must give equal `job_text` for each of:
- blank lines at a step boundary;
- a step with empty output, so its file holds only a header group;
- an unclosed `##[group]` that runs across a boundary;
- a blank-only block that `_split_blocks` drops;
- a job log without a final `\n`;
- a CRLF log;
- a job log that starts with a BOM: the case of every recorded log. It is bound, and its
  `job_text` is unchanged.

If the splitter could not keep the invariant, the overlay must change, not the
invariant.

## 6. States (snapshot schema 1.5)

How available the archive was, and what binding did for each job, are recorded
separately. A missing field always means "not attempted": an older snapshot, or a code
path that does not read the archive.

**Run-level `archive`** (`None` = not attempted):

| State | Meaning |
|---|---|
| `available` | downloaded within limits, read without error, holds at least one step directory |
| `absent` | downloaded and read, holds no step directory (the shape of P's earlier runs) |
| `unavailable` | the download returned an HTTP error status (404, 410, any other) |
| `refused` | a limit was exceeded; the archive is corrupt, duplicated, encrypted or uses an unsupported method (§7); or a step directory is unreadable before ownership (§4.2) |

Each state carries a short `reason` (text) beside it, except `available`.

**Per-job `step_binding`** (`None` = not attempted):

| State | Meaning |
|---|---|
| `bound` | §4 proved ownership and structure; the log's segments carry `StepRef` |
| `no_archive` | the run's `archive` is not `available` |
| `unverifiable` | some population job's log is not `present` (§4.1) |
| `unmatched` | no step directory's text equals this job's log |
| `ambiguous` | this job is in a match that is not one-to-one (§4.2) |
| `malformed` | the candidate failed a structural check (§4.3), with a reason |

`available` with every job `unmatched` or `ambiguous` is a valid outcome, distinct from
`absent` and from `refused`. 1.5 is additive over 1.4: both fields default to `None`,
so 1.4 and older documents load as "not attempted", and 1.3's recorded step bindings
still load unrebound (#113).

## 7. Downloading and reading the archive

### 7.1 Limits (constants, no CLI flag)

| Limit | Value | Enforced |
|---|---|---|
| download | 64 MiB | while receiving (§7.2) |
| entries | 4096 | from the central directory, before any entry is read |
| one entry, uncompressed | 64 MiB | declared size checked first, then counted while decompressing |
| all read entries, uncompressed | 256 MiB | counted while decompressing |

The reference point is P: one six-job run is 41 KB compressed, 132 KB uncompressed and
50 entries. An archive holds every job's log twice (the top-level file and the step
files), so the uncompressed total is about twice the logs. Exceeding a limit makes the
archive `refused` with the limit named, and binding falls back to job-level. A limit is
never an error that stops diagnosis.

### 7.2 A download limit that limits the download

`SubprocessGh.api_bytes` buffers the whole response (`subprocess.run(capture_output=True)`),
so checking `len()` afterwards protects no memory. That is how `fetch_archive` works today,
and its docstring says so. The archive needs a capped read:

A new runner method, `api_bytes_capped(argv, *, timeout, max_bytes)`, owns the whole
lifecycle of one `gh api` process:

- **Start.** `Popen` with stdout and stderr as pipes and stdin as the null device, in
  `_gh_env()`. The program is `gh` by default and injectable, so that tests can run a
  real child process.
- **Two reader threads.** One reads stdout in 64 KiB chunks and appends to a buffer
  until the total passes `max_bytes`; it then stops appending and sets `over the cap`.
  The other reads stderr until end of file and keeps only the **last 64 KiB**, so a
  child that floods stderr can neither block on a full pipe nor grow memory. Neither
  thread decides anything.
- **One deadline.** The calling thread waits for the process with the time left until
  `start + timeout`. It also stops waiting as soon as `over the cap` is set. A blocking
  chunk read happens in a reader thread, never in the calling thread, so it cannot hold
  off the deadline.
- **Termination.** On `over the cap` or on the deadline: `terminate()`, a wait of up to
  2 s, then `kill()` and an unbounded `wait()`. The child is always reaped, and the pipes
  reach end of file once it is gone. Both reader threads are then joined with a bound;
  if one fails to end, that is reported as a status-less `GhError` too.
- **Results.** Over the cap: `over the cap`, which is not a `GhError` (§7.1, the archive
  is `refused`). Deadline: a status-less `GhError` naming the timeout. Nonzero exit: maps
  through `_gh_failure` with the retained stderr, exactly as `api_bytes` does. Otherwise:
  the bytes.

The runner protocol grows a `GhCappedBytesRunner`, and test fakes implement it for the
ownership and state tests. **The lifecycle itself is tested against real child
processes** (`sys.executable -c …`, no network, no GitHub), each with a bounded test
time:

- stdout over the cap: stops, the child is reaped, and the buffer holds at most cap plus
  one chunk;
- stderr floods (more than a pipe buffer) while stdout stays small: completes, with
  stderr truncated to its tail;
- a silent child that sleeps past the timeout: a status-less `GhError` within timeout
  plus the termination grace;
- a child that ignores `SIGTERM`: killed and reaped;
- a nonzero exit with a stderr message: `_gh_failure`'s mapping;
- after each case, the child has a return code: no zombie, no live process.

`fetch_archive` is not changed by this design.

### 7.3 Reading the ZIP

The archive is read in memory with `zipfile` and never extracted to disk.

| Condition | Result |
|---|---|
| `BadZipFile`, or a CRC or other error while reading an entry | `refused` (corrupt) |
| two entries with the same name in the central directory | `refused` (duplicate) |
| an entry with the encryption flag (bit 0) | `refused` (encrypted) |
| a compression method other than stored (0) or deflated (8) | `refused` (unsupported) |
| a step file that is not valid UTF-8, or a non-canonical or repeated `N` in a step directory | `refused` (unreadable before ownership, §4.2) |

Each entry is decompressed through `ZipFile.open` in bounded chunks, so a limit is
enforced against what actually decompresses, not against declared sizes.

**What corruption is detected, and what is not.** Every check that reads only the
central directory applies to **every** entry: the entry count, duplicate names, the
encryption flag, the compression method and the declared size. Decompression, with its
CRC check and limits, runs only on the **step entries** that binding uses. Top-level
`<i>_<name>.txt` files and `<dir>/system.txt` are never decompressed, so damage inside
one of them goes unnoticed. That is deliberate: binding never reads them, and the
archive's text is never evidence (§2). A test pins this behaviour.

### 7.4 Failures

As elsewhere in forge, an HTTP status is data about the run and a missing status is a
broken instrument:

- **A `GhError` with a status** makes the archive `unavailable`, with that status.
- **A `GhError` without a status** (timeout, `gh` did not start) propagates, as it does
  for the jobs listing and the job logs. An archive `unavailable` because of a timeout
  would hide a broken instrument as data about the run.

The extra population logs (§4.1) follow `_Gh.logs`' existing rule, and their state
decides `unverifiable`.

## 8. Diagnose, CLI and output

- `diagnose` does not change (§5.2). The verdict's `run` carries the new fields;
  `verdict_schema_version` does not change, because the run snapshot is nested and
  versioned on its own.
- No new CLI flag. The limits are constants (§7.1).
- **A runner without the capped download** (`api_bytes_capped`) attempts no archive:
  `archive` and every `step_binding` stay `None`, which means not attempted, exactly as
  in an older snapshot. This is a supported limited capability, not an error. The
  production runner, `SubprocessGh`, always has the capped download. Tests that exercise
  binding (integration, acceptance) must use a capped runner, so that the older fakes,
  which lack it, can never look like coverage of the new path.
- `--output-file` and stored snapshots carry `archive` and `step_binding`, so an
  operator reading a job-level verdict can see why binding did not happen.

## 9. Acceptance

### 9.1 On the existing recording `steps-1`

Each pre-registered line (`expected.json`) lands in the evidence of its pre-registered
`StepRef`. The `fallback_ok` lines count too: with the archive available, binding
applies to them as well.

| Job | What binding must give |
|---|---|
| `s1` | `MARK-s1-b` and `AssertionError: probe-s1` → step 4; `MARK-s1-a` → step 3 |
| `s2` | the named step (`Custom label s2`, header `Run printf … s2-b`) → step 4; `MARK-s2-c` → step 5 |
| `s3` | `AssertionError: probe-s3` → step 3; `exec format error` → step 4. Each failed step's verdict cites only its own line, and neither carries the job-level note |
| `s4` | every inner line of the composite → outer step 4; `MARK-s4-before` → 3; `MARK-s4-after` → 5 |
| `s5` | `MARK-s5-spoof` → step 3 (its true owner), not step 4 |
| `s6` | the two identically named steps → steps 3 and 4 by number |

Every job of `steps-1` is `bound`, and the run's `archive` is `available`. For each
job, `job_text` equals its no-archive `job_text` byte for byte (§5.3).

### 9.2 Synthetic, derived from `steps-1` and labelled as such

Each case is built from the recording by a named transformation, under
`tests/fixtures/step-binding/synthetic/`, with PROVENANCE stating the derivation.

| Case | Derivation | Expected |
|---|---|---|
| missing step file | one entry of `s2-named/` removed | `s2` `unmatched`; the others `bound` |
| identical job logs | `s6`'s log served for a second job id in the listing | both `ambiguous` |
| reordered by renumbering | `s1`'s `3_…` renamed `7_…`: the concatenation order becomes `[1, 2, 4, 7, 8, 9]` | `s1` `unmatched` (the text no longer matches; checked on the recording) |
| number not in the API | `s1`'s last file `9_…` renamed `99_…`: the order is kept, and 99 is not a step of `s1` | `s1` `malformed` (matches, then fails §4.3; checked on the recording) |
| API number repeated | `s1`'s listing gives a second step the number 4; the archive is unchanged | `s1` `malformed` |
| step file not UTF-8 | one byte of a `s3-two-failures/` file set to `0xFF` | `refused` (§4.2); every job unbound |
| unreadable beside a match | a non-UTF-8 copy of `s1`'s directory added under another name, while `s1`'s own stays intact and matching | `refused`; `s1` is not bound by excluding the unreadable directory |
| repeated or non-canonical `N` | `s2-named/3_…` copied as `03_…`; separately, a second `4_…` entry with another name | `refused` (§4.2) |
| `+N` name | `s2-named/3_…` copied as `+3_…` | `refused` (§4.2) |
| only malformed names | an extra directory `extra/` holding only `+1_a.txt` and `x.txt` | `refused` (§4.2), not skipped as "no step directory" |
| no per-step files | only top-level and `system.txt` entries kept | `absent`; every job `no_archive` |
| duplicate entry | one name written twice | `refused` (duplicate) |
| corrupt | central directory truncated | `refused` (corrupt) |
| encrypted | the encryption flag set on one entry | `refused` (encrypted) |
| unsupported method | one entry's method set to bzip2 (12) | `refused` (unsupported) |
| over limits | limits lowered in the test, not the data | `refused`, naming the limit |
| over the download cap | the fake runner streams more than the cap | `refused`; the fake records that reading stopped |
| no trailing `\n` | last byte of one step file removed | that job `malformed` |
| foreign line breaks | a `\x85` inserted into one log line, the step file edited to match | that job `malformed` |
| unreadable population log | one green job's log served as 502 | every job `unverifiable` |
| attempt mixing | `steps-1`'s jobs and logs with an archive whose step files carry another runner's `Worker ID` and temporary `HOME` lines, the lines that differ between real runs; a stand-in for another attempt's archive, labelled as such | every job `unmatched`; the requests asserted to name the chosen attempt |
| `job_text` invariant | the six cases of §5.3 | equal `job_text`, bound vs unbound |

### 9.3 A real repeated attempt (needs the owner's permission)

**Proposed run:** one `gh run rerun 37115427715` (polygon, the same orphan commit
`dce7182`, restored from the tag `evidence/polygon-steps-1` as the branch
`polygon/steps-1b` if a ref is needed), then recording attempt 2's jobs, logs and
archive with the `steps-1` recorder's method, as a new case `steps-1b`.

What it adds: attempt 2's archive binds to attempt 2's job ids, and attempt 1's
recording binds to attempt 1's.

What it cannot add: proof that a swapped archive is detected when the two attempts'
texts are identical (§1). That guarantee rests on the request tests of §9.2.

Without the permission, the repeated-attempt case stays synthetic only, and this spec
says so in its acceptance record.

## 10. Order of work (for the plan)

1. `_split_blocks` reports line indices; the `job_text` invariant tests (§5.3) are
   added first and pass while nothing is bound.
2. `api_bytes_capped` with its real-process lifecycle tests (§7.2); the ZIP reader with
   every refusal of §7.3 and §4.2, tested on the synthetic archives.
3. Ownership (§4) as a pure function over `(jobs, logs, archive entries)`, with the
   synthetic cases.
4. Wiring into `fetch_failed_run`: the population reads, the states, snapshot 1.5.
5. Acceptance on `steps-1` (§9.1); `steps-1b` if permitted.
6. README and TODO.
