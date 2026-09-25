"""The fix-wide reading-divergence checks (``fix/reading.py``): review N1/N2
and Ruling O's strict-form gate."""

import pytest

from deployer.admission.prepare import _as_r_reads
from deployer.fix.reading import heredoc_reason, join_reason, strict_form_reason
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


# --- Ruling O: the strict-form gate ----------------------------------------

_PLAIN = b"FROM python:3.12-slim\nRUN apt-get update && \\\n    apt-get install -y x\n"

# One refused file per rule, keyed by a short id: (bytes, reason fragment).
_REFUSED: dict[str, tuple[bytes, str]] = {
    "bom": (b"\xef\xbb\xbf" + _PLAIN, "byte-order mark"),
    "bom-escape": (b"\xef\xbb\xbf# escape=`\nFROM a\nRUN true \\\n", "byte-order"),
    "utf8": (b"FROM a\nRUN echo \xff\n", "invalid UTF-8"),
    "lone-cr": (b"FROM a\nRUN true \\\r \nRUN x\n", "carriage return"),
    "cr-only": (b"FROM a\rRUN x\r", "carriage return"),
    "vt": (b"FROM a\nRUN a\x0bb\n", "control character"),
    "ff": (b"FROM a\nRUN true \\\x0c\nRUN x\n", "control character"),
    "x1c": (b"FROM a\nRUN a\x1cb\n", "control character"),
    "x1f": (b"FROM a\nRUN a\x1fb\n", "control character"),
    "nul": (b"FROM a\nRUN a\x00b\n", "control character"),
    "esc": (b"FROM a\nRUN a\x1bb\n", "control character"),
    "nel": ("FROM a\nRUN a\u0085b\n".encode(), "control character"),
    "ls": ("FROM a\nRUN a\u2028b\n".encode(), "control character"),
    "ps": ("FROM a\nRUN a\u2029b\n".encode(), "control character"),
    "hd-plain": (b"FROM a\nRUN <<EOF\necho\nEOF\n", "'<<'"),
    "hd-dq": (b'FROM a\nRUN cat <<"EOF"\nx\nEOF\n', "'<<'"),
    "hd-sq": (b"FROM a\nRUN cat <<'EOF'\nx\nEOF\n", "'<<'"),
    "hd-dash-dq": (b'FROM a\nRUN cat <<-"EOF"\nx\nEOF\n', "'<<'"),
    "hd-digit": (b"FROM a\nRUN cat <<1EOF\nx\n1EOF\n", "'<<'"),
    "hd-space": (b'FROM a\nRUN cat <<" "\nx \\\n \nRUN x\n', "'<<'"),
    "hd-quoted": (b'FROM a\nLABEL x="<<EOF"\n', "'<<'"),
    "hd-comment": (b"FROM a\n# a << b\n", "'<<'"),
    "escape": (b"# escape=`\nFROM a\n", "line 1: a parser directive"),
    "syntax": (b"# syntax=docker/dockerfile:1\nFROM a\n", "parser directive"),
    "check": (b"# check=skip=all\nFROM a\n", "parser directive"),
    "unknown": (b"# foo=bar\n# escape=`\nFROM a\n", "line 1: a parser"),
    "late-directive": (b"# hello\n\n#escape = `\nFROM a\n", "line 3: a parser"),
    "nbsp-after-bs": ("FROM a\nRUN true \\\u00a0\nRUN x\n".encode(), "line 2:"),
}

# One accepted file per rule, keyed by a short id.
_ACCEPTED: dict[str, bytes] = {
    "plain": _PLAIN,
    "crlf": _PLAIN.replace(b"\n", b"\r\n"),
    "tab": b"FROM a\nRUN\ttrue \\\t\n\tx\n",
    "utf8": 'FROM a\nLABEL x="caf\u00e9"\n'.encode(),
    "nbsp-mid": 'FROM a\nLABEL x="a\u00a0b"\n'.encode(),
    "one-lt": b"FROM a\nRUN test 1 < 2\n",
    "comment": b"# build the app\nFROM a\n",
    "directive-late": b"FROM a\n# escape=`\nRUN x\n",
    "space-tab-after-bs": b"FROM a\nRUN true \\ \t\n  x\n",
    "empty": b"",
}


@pytest.mark.parametrize("case", list(_REFUSED), ids=list(_REFUSED))
def test_strict_form_refused(case: str) -> None:
    """Each rule of the gate refuses its form."""
    data, fragment = _REFUSED[case]
    reason = strict_form_reason(data)
    assert reason is not None and fragment in reason


@pytest.mark.parametrize("case", list(_ACCEPTED), ids=list(_ACCEPTED))
def test_strict_form_accepted(case: str) -> None:
    """The plain form, CRLF endings and tabs pass the gate."""
    assert strict_form_reason(_ACCEPTED[case]) is None


def test_strict_form_is_total() -> None:
    """Every single byte, alone or around a line, gives a reason or None."""
    for value in range(256):
        byte = bytes([value])
        for data in (byte, b"FROM a\n" + byte + b"\n", b"RUN x \\" + byte):
            reason = strict_form_reason(data)
            assert reason is None or isinstance(reason, str)
