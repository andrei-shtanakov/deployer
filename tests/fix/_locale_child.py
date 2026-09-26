"""Child process for ``test_binds_under_a_real_non_utf8_locale``.

Run in a separate interpreter because a process's locale is fixed at
startup (from ``LC_ALL``/``LANG``): the parent test's own locale cannot be
changed mid-run to exercise ``admission.prepare.decode_as_read_text``'s locale
dependence for real. All non-ASCII content is read from a file passed as
``sys.argv[1]``; the child's own command line stays pure ASCII, since a
non-ASCII argv is not reliable across CI locales.
"""

import locale
import sys

from deployer.admission.model import Defect
from deployer.fix.binding import Bound, bind_instruction

LOCALE_UNAVAILABLE = 3


def main() -> int:
    """Bind a defect naming a Latin-1-only byte, decoded the way R does."""
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error as exc:
        print(f"LOCALE_UNAVAILABLE: {exc}")
        return LOCALE_UNAVAILABLE
    encoding = locale.getpreferredencoding(False).upper()
    if "8859" not in encoding and "LATIN" not in encoding:
        print(f"LOCALE_UNAVAILABLE: got encoding {encoding!r}")
        return LOCALE_UNAVAILABLE
    with open(sys.argv[1], "rb") as handle:
        dockerfile = handle.read()
    # "café.txt" is what R reads byte 0xE9 as under Latin-1 (U+00E9,
    # LATIN SMALL LETTER E WITH ACUTE) — the same codepoint UTF-8 source
    # encodes this literal as, so no special handling is needed here.
    defect = Defect(
        cls="missing_copy_source",
        file="Dockerfile",
        lines=(2, 2),
        object="café.txt",
    )
    result = bind_instruction(dockerfile, defect)
    if isinstance(result, Bound):
        print("BOUND")
        return 0
    print(f"NOT_BOUND: {result}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
