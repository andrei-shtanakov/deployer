"""Acceptance on the real ``steps-1`` recording (spec §9.1) and end-to-end
synthetic cases derived from it (§9.2), through ``fetch_failed_run``."""

from deployer.diagnose import diagnose_run
from deployer.forge import FailedRun, OverCap, RunRef, StepRef, fetch_failed_run
from deployer.reproduce.shape import job_text
from tests import step_binding_data as data

REPO = "andrei-shtanakov/deployer"
RUN = 37115427715
JOB_LEVEL_NOTE = "cited evidence is job-level (no step binding)"


class PlainReplay:
    """Serves the recorded ``gh api`` calls by path. No capped download, so
    ``fetch_failed_run`` attempts no archive. Annotations were not recorded
    (``read_attempt`` does not read them), so they are served empty: a
    labelled gap."""

    def __init__(self, archive: bytes, logs: dict[int, str] | None = None) -> None:
        self._by_path = {
            c["argv"][-1]: c["stdout"] for c in data.calls() if "stdout" in c
        }
        self._logs = logs or {}
        self._archive = archive
        self.paths: list[str] = []

    def api(self, argv: list[str], *, timeout: float) -> str:
        path = argv[-1]
        self.paths.append(path)
        if "/check-runs/" in path:
            return "[]"
        job_id = path.rsplit("/", 2)[-2] if path.endswith("/logs") else None
        if job_id is not None and int(job_id) in self._logs:
            return self._logs[int(job_id)]
        return self._by_path[path]


class Replay(PlainReplay):
    """``PlainReplay`` plus ``archive`` as the per-attempt log archive."""

    def api_bytes_capped(
        self, argv: list[str], *, timeout: float, max_bytes: int
    ) -> bytes | OverCap:
        self.paths.append(argv[-1])
        return self._archive


def _fetch(gh: PlainReplay) -> FailedRun:
    run = fetch_failed_run(RunRef(REPO, RUN), attempt=1, runner=gh)
    assert isinstance(run, FailedRun)
    return run


def _recorded() -> bytes:
    return (data.STEPS_1 / "attempt-1.zip").read_bytes()


def test_every_preregistered_line_lands_at_its_step() -> None:
    run = _fetch(Replay(_recorded()))
    assert run.archive is not None and run.archive.state == "available"
    by_name = {j.name: j for j in run.jobs}
    assert {j.step_binding.state for j in run.jobs if j.step_binding} == {"bound"}
    for job_name, line, number in data.preregistered():
        kept = by_name[job_name]
        holders = [e for e in kept.evidence if line in e.text.split("\n")]
        assert len(holders) == 1, (job_name, line)
        assert holders[0].source == StepRef(kept.job_id, number), (job_name, line)


def test_job_text_is_unchanged_by_binding() -> None:
    bound = {j.job_id: job_text(j) for j in _fetch(Replay(_recorded())).jobs}
    plain = {j.job_id: job_text(j) for j in _fetch(PlainReplay(_recorded())).jobs}
    assert bound == plain


def test_each_failed_step_of_s3_cites_only_its_own_line() -> None:
    run = _fetch(Replay(_recorded()))
    diagnosis = diagnose_run(run)
    (s3,) = [j for j in run.jobs if j.name == "s3-two-failures"]
    verdicts = {
        v.where.number: v
        for v in diagnosis.failures
        if isinstance(v.where, StepRef) and v.where.job_id == s3.job_id
    }
    cited = {n: "\n".join(e.text for e in v.evidence) for n, v in verdicts.items()}
    assert "AssertionError: probe-s3" in cited[3]
    assert "exec format error" not in cited[3]
    assert "exec format error" in cited[4]
    assert "AssertionError: probe-s3" not in cited[4]
    assert all(JOB_LEVEL_NOTE not in v.observations for v in verdicts.values())


def test_every_request_names_the_chosen_attempt() -> None:
    gh = Replay(_recorded())
    _fetch(gh)
    runs = [p for p in gh.paths if "/actions/runs/" in p]
    assert runs and all(f"/runs/{RUN}/attempts/1" in p for p in runs)


def test_another_runners_archive_binds_nothing() -> None:
    blob = data.zip_of(data.foreign_runner(data.archive_entries()))
    run = _fetch(Replay(blob))
    assert {j.step_binding.state for j in run.jobs if j.step_binding} == {"unmatched"}
    assert all(not isinstance(e.source, StepRef) for j in run.jobs for e in j.evidence)


def test_a_missing_step_file_unbinds_only_its_job() -> None:
    blob = data.zip_of(data.without(data.archive_entries(), "s2-named/3_"))
    run = _fetch(Replay(blob))
    states = {j.name: j.step_binding.state for j in run.jobs if j.step_binding}
    assert states.pop("s2-named") == "unmatched"
    assert set(states.values()) == {"bound"}


def test_identical_job_logs_bind_neither() -> None:
    ids = {name: r["id"] for name, r in data.job_records().items()}
    logs = data.job_logs()
    run = _fetch(Replay(_recorded(), {ids["s5-spoof"]: logs["s6-dupes"]}))
    states = {j.name: j.step_binding.state for j in run.jobs if j.step_binding}
    assert states["s5-spoof"] == "ambiguous" and states["s6-dupes"] == "ambiguous"
    assert states["s1-plain-fail"] == "bound"
