"""``SubprocessGh.api_bytes_capped`` against real child processes (spec §7.2).

No network: the "gh" is ``python -c <script>``. Each child records its pid in
``$PIDFILE`` first, so the test can prove it was reaped, not left a zombie.
"""

import io
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from deployer.forge import (
    _CAPPED_CHUNK,
    GhCappedBytesRunner,
    GhError,
    OverCap,
    SubprocessGh,
    _capped_result,
    _StderrTail,
    _StdoutReader,
)

PRELUDE = (
    "import os, sys, time, signal\n"
    "open(os.environ['PIDFILE'], 'w').write(str(os.getpid()))\n"
)


def _gh(script: str) -> SubprocessGh:
    return SubprocessGh(command=(sys.executable, "-c", PRELUDE + script))


@pytest.fixture()
def pidfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "pid"
    monkeypatch.setenv("PIDFILE", str(path))
    return path


def _assert_reaped(pidfile: Path) -> None:
    pid = int(pidfile.read_text())
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)  # already reaped: not our child any more


def test_subprocess_gh_is_a_capped_runner() -> None:
    assert isinstance(SubprocessGh(), GhCappedBytesRunner)


def test_bytes_come_back_whole(pidfile: Path) -> None:
    gh = _gh("sys.stdout.buffer.write(b'PK' + b'x' * 1000)")
    got = gh.api_bytes_capped(["x"], timeout=10, max_bytes=2000)
    assert got == b"PK" + b"x" * 1000
    _assert_reaped(pidfile)


def test_exactly_the_cap_is_not_over_it(pidfile: Path) -> None:
    gh = _gh("sys.stdout.buffer.write(b'x' * 5000)")
    assert gh.api_bytes_capped(["x"], timeout=10, max_bytes=5000) == b"x" * 5000
    _assert_reaped(pidfile)


def test_an_endless_stdout_stops_at_the_cap(pidfile: Path) -> None:
    gh = _gh(
        "while True:\n    sys.stdout.buffer.write(b'x' * 65536); sys.stdout.flush()"
    )
    start = time.monotonic()
    got = gh.api_bytes_capped(["x"], timeout=30, max_bytes=200_000)
    assert got == OverCap(200_000)
    assert time.monotonic() - start < 10
    _assert_reaped(pidfile)


def test_a_fast_exit_over_the_cap_is_still_over_it(pidfile: Path) -> None:
    gh = _gh("sys.stdout.buffer.write(b'x' * 100_001)")
    got = gh.api_bytes_capped(["x"], timeout=10, max_bytes=100_000)
    assert got == OverCap(100_000)
    _assert_reaped(pidfile)


def test_a_stderr_flood_neither_blocks_nor_grows(pidfile: Path) -> None:
    gh = _gh(
        "sys.stderr.buffer.write(b'e' * 2_000_000); sys.stdout.buffer.write(b'ok')"
    )
    assert gh.api_bytes_capped(["x"], timeout=10, max_bytes=100) == b"ok"
    _assert_reaped(pidfile)


def test_a_failing_exit_maps_its_status_from_the_stderr_tail(pidfile: Path) -> None:
    gh = _gh("sys.stderr.buffer.write(b'e' * 2_000_000 + b' (HTTP 404)'); sys.exit(1)")
    with pytest.raises(GhError) as caught:
        gh.api_bytes_capped(["x"], timeout=10, max_bytes=100)
    assert caught.value.status == 404
    assert len(str(caught.value)) < 70_000
    _assert_reaped(pidfile)


def test_a_silent_child_times_out(pidfile: Path) -> None:
    gh = _gh("time.sleep(60)")
    start = time.monotonic()
    with pytest.raises(GhError) as caught:
        gh.api_bytes_capped(["x"], timeout=0.5, max_bytes=100)
    assert caught.value.status is None and "timed out" in str(caught.value)
    assert time.monotonic() - start < 0.5 + 2 + 3
    _assert_reaped(pidfile)


def test_a_child_ignoring_sigterm_is_killed(pidfile: Path) -> None:
    gh = _gh("signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)")
    start = time.monotonic()
    with pytest.raises(GhError):
        gh.api_bytes_capped(["x"], timeout=1.0, max_bytes=100)
    elapsed = time.monotonic() - start
    # SIGTERM was ignored, so the full 2 s grace passed before the kill.
    assert 1.0 + 2 <= elapsed < 1.0 + 2 + 3
    _assert_reaped(pidfile)


def test_a_missing_program_is_a_status_less_error() -> None:
    gh = SubprocessGh(command=("/nonexistent/gh-binary",))
    with pytest.raises(GhError) as caught:
        gh.api_bytes_capped(["x"], timeout=5, max_bytes=100)
    assert caught.value.status is None


def test_the_stdout_buffer_holds_at_most_the_cap_plus_one_chunk() -> None:
    read_fd, write_fd = os.pipe()
    stop = threading.Event()

    def flood() -> None:
        try:
            while not stop.is_set():
                os.write(write_fd, b"x" * 100_000)
        except OSError:  # the reader closed its end
            pass
        finally:
            os.close(write_fd)

    writer = threading.Thread(target=flood, daemon=True)
    writer.start()
    reader = _StdoutReader(os.fdopen(read_fd, "rb", buffering=0), 150_000)
    reader.start()
    reader.join(5)
    stop.set()
    writer.join(5)
    assert not reader.is_alive() and reader.over.is_set()
    assert 150_000 < reader.size <= 150_000 + _CAPPED_CHUNK
    assert sum(map(len, reader.chunks)) == reader.size


def test_a_reader_error_surfaces_rather_than_partial_bytes() -> None:
    out = _StdoutReader(io.BytesIO(), 100)
    out.chunks.append(b"partial")
    out.error = OSError("read failed")
    err = _StderrTail(io.BytesIO())
    with pytest.raises(GhError, match="reading output failed") as caught:
        _capped_result("x", 10, 100, 0, "exited", out, err)
    assert caught.value.status is None
