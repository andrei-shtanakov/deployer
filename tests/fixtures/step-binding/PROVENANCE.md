# Step-binding recording — provenance

One real run on the polygon of `andrei-shtanakov/deployer`, recorded to ground
step-level log binding in forge (TODO `forge-step-level-log-binding`) before any
design is written. The owner permitted one push of `polygon/steps-1` and one dispatch
on 2026-10-03. No production code reads this data yet.

## What was recorded

- **The run:** 37115427715, attempt 1, `workflow_dispatch` on `polygon/steps-1`
  (orphan commit `dce7182`), 2026-10-03 10:08:50–10:08:59Z, `ubuntu-24.04`, runner
  2.337.0.
- **The workflow** (`steps-1/tree/.github/workflows/diagnosis-polygon.yml`) has six
  independent jobs, one case each, every step plain bash after a pinned
  `actions/checkout`:

  | Job | What it probes |
  |---|---|
  | `s1-plain-fail` | an unnamed `run:` passes, a multi-line one fails (`exit 3`) |
  | `s2-named` | an unnamed step, a failing step with `name:`, a named `if: always()` step after it |
  | `s3-two-failures` | two failing steps, the second through `if: always()`, each with a different diagnose shape |
  | `s4-composite` | a plain step, a local composite action (`steps-1/tree/.github/actions/probe/action.yml`: an inner `::group::`, then a failing inner step), an `if: always()` step |
  | `s5-spoof` | a failing step that prints `::group::` with the exact header of the next step, then the next step through `if: always()` |
  | `s6-dupes` | two steps with the same `name:`, the second fails |

- **Markers** are assembled at run time (`printf '%s-%s\n' MARK s1-a` prints
  `MARK-s1-a`), so the full line never appears in the command the runner echoes into
  the step's header. The diagnostic lines (`AssertionError: probe-<tag>`,
  `exec format error`) are built the same way.
- **How:** `record_steps.py`. `prepare` wrote the tree and `expected.json`. They were
  committed (`aa4cae2`) before `record` pushed, checked that the SHA heads no open PR,
  dispatched, waited, and read the run through `forge.list_runs_for_sha` and
  `forge.read_attempt` with a runner that records every `gh api` call verbatim.

## Files

| File | Written | Content |
|---|---|---|
| `steps-1/expected.json` | before the run | the owner of every exact output line: job, and the workflow step's index after the checkout; `fallback_ok` where job-level is acceptable and only another step is wrong; the cross-citation question for `s3` |
| `steps-1/gh-calls.json` | by the recorder | every `gh api` call forge made: argv, then stdout or the error. Run metadata, the jobs listing with step numbers and second-resolution times, every job log |
| `steps-1/attempt-1.zip` | by the recorder | `actions/runs/37115427715/attempts/1/logs` as downloaded, within seconds of 10:09:20Z |
| `steps-1/environment.json` | by the recorder | repo, branch, SHA, run id and URL, workflow path, UTC time |
| `steps-1/observed.json` | after the run, before any parser change | where each pre-registered line actually is (job-log line, per-step zip file), the header forms, the archive's composition |

Nothing recorded was trimmed, edited or redacted. `CHECKSUMS.sha256` covers every file
here except itself and the recorder.

## What it shows

- **Every pre-registered owner holds.** Each of the 22 lines occurs exactly once in
  its job log, and the per-step zip file that holds it is the pre-registered step's
  API number.
- **A named `run:` step's header is not its name.** `s2`'s step 4 is
  `Custom label s2` in the API; its header in the log is
  `##[group]Run printf '%s-%s\n' MARK s2-b` (`s2` job log line 110).
- **A step's own output can forge the next step's header.** In `s5` the job-log lines
  113 (printed by step 3) and 118 (the runner's header of step 4) are byte-identical
  after the timestamp. Forge's current `_bind_log` binds a `##[group]` block to the
  step whose name equals its title, so it gives `MARK-s5-spoof` (step 3's output) to
  step 4. The job log alone cannot tell the two apart.
- **A step's output follows its header group.** It comes after the `##[endgroup]` of
  the step's `Run …` group, up to the next header, and not inside the group. A failing
  step ends with `##[error]Process completed with exit code N.`
- **Composite actions** open their own `##[group]Run …` headers inside the outer step,
  between runner lines `##[start-action display=…;id=…]` and
  `##[end-action id=…;outcome=…;conclusion=…;duration_ms=…]`. The `display=` value is
  neither the API step name nor the header: `%` arrives percent-encoded, so the inner
  step whose header is `##[group]Run printf '%s-%s\n' MARK s4-inner-2` is announced as
  `display=Run printf '%25s-%25s\n' MARK s4-inner-2`, while the sibling with no `%`
  (`display=Run echo "::group::inner group s4"`) is not encoded.
- **This run's archive has per-step files.** For each job it holds:
  - one top-level `<i>_<job>.txt`, equal to the job log;
  - `<job>/system.txt`;
  - one `<job>/<API step number>_<sanitised step name>.txt` per step that ran, post
    steps included (as observed: `/` and `\` become `_`; `"` and `:` are dropped).

  Per job, the step files concatenated in step-number order equal the job log as text.
  That holds with each file's UTF-8 BOM dropped and every runner timestamp removed; the
  two renditions' timestamps differ by fractions of a millisecond. A second download at
  10:49:56Z was byte-identical.
- **Archive composition differs between recorded runs; the cause is unknown.** The
  archives of two earlier runs held only `0_<job>.txt` and `<job>/system.txt`, with no
  per-step files:
  - 36239375530, `workflow_dispatch`, downloaded on 2026-10-03, a week after the run;
  - 37109766941, `pull_request`, downloaded twice, about 80 and 100 minutes after the
    run, byte-identical both times.

  Those archives are not committed. Three runs cannot separate event, runner image,
  job shape and age, so a consumer must treat per-step files as sometimes present.
  Steps-1's archive did not change over 40 minutes, but that does not prove the files
  are durable.

## Not recorded

A truncated log could not be produced honestly at this cost. Its handling is to be
covered by a synthetic test, labelled as such.

## Synthetic cases

The step-binding tests also run on cases derived from `steps-1`. They are built at test
time by named functions in `tests/step_binding_data.py` (`renamed`, `without`, `edited`,
`foreign_runner`, `zip_of`), and no derived file is committed. Every derived case is a
transformation of the recorded entries or logs, named by its test. None of them is a
recording. `foreign_runner` replaces the `Worker ID` and temporary `HOME` lines, the
lines that differ between real runs, as a stand-in for another attempt's archive; it is
not one. Annotations were not recorded, because `read_attempt` does not read them, so
the acceptance replay serves them empty.
