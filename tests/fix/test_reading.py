"""The fix-wide reading-divergence checks (``fix/reading.py``), review N1/N2."""

import pytest

from deployer.admission.prepare import _as_r_reads
from deployer.fix.reading import heredoc_reason, join_reason
from deployer.reproduce.dockerfile import parse


@pytest.mark.parametrize(
    "line",
    [b"RUN true \\\x0c", b"RUN true \\\x0b", b"RUN true \\ \x0c", b"RUN true \\\x0c\t"],
    ids=["ff", "vt", "space-ff", "ff-tab"],
)
def test_n2_backslash_then_other_blank_refused(line: bytes) -> None:
    """R continues the line; Buildah (``\\\\[ \\\\t]*$``) does not."""
    reason = join_reason(b"FROM a\n" + line + b"\nRUN x\n")
    assert reason is not None and reason.startswith("line 2:")
    assert "other than space or tab" in reason


@pytest.mark.parametrize(
    "data",
    [
        b"FROM a\r\nRUN true \\\r\n  x\r\n",
        b"FROM a\nRUN true \\ \t\n  x\n",
        b"FROM a\rRUN true \\\r  x\r",
        b"FROM a\nRUN true\x0c\n",
    ],
    ids=["crlf", "space-tab", "cr", "ff-no-backslash"],
)
def test_n2_modelled_continuations_still_pass(data: bytes) -> None:
    """CRLF, CR and space/tab after the backslash read as before."""
    assert join_reason(data) is None


@pytest.mark.parametrize(
    "data",
    [
        b"FROM a\nRUN true # x<<EOF\nFROM b\n",
        b"FROM a\nRUN <<EOF\necho\nEOF\n",
        b"FROM a\nCOPY <<EOF /x\nhi\nEOF\n",
    ],
    ids=["comment", "run", "copy"],
)
def test_n1_any_heredoc_refused(data: bytes) -> None:
    """Any instruction opening a heredoc by R's rule refuses the file."""
    reason = heredoc_reason(parse(_as_r_reads(data)).instructions)
    assert reason == "a heredoc at line 2 is not modelled"


def test_n1_no_heredoc_passes() -> None:
    """A quoted ``<<`` opens nothing; a file without one passes."""
    data = b'FROM a\nLABEL x="<<EOF"\nRUN echo "<<EOF"\n'
    assert heredoc_reason(parse(_as_r_reads(data)).instructions) is None
