"""The archive reader (spec §4.2, §7.1, §7.3): bounded, in memory, all or nothing."""

import zipfile

import pytest

from deployer.logarchive import ArchiveLimits, ArchiveRefused, read_step_directories
from tests import step_binding_data as data


def _patch_central(
    blob: bytes, name: str, *, flag_or: int = 0, method: int | None = None
) -> bytes:
    """Set bits in, or the method of, ``name``'s central-directory record."""
    raw = bytearray(blob)
    target = name.encode()
    at = raw.find(b"PK\x01\x02")
    while at != -1:
        size = int.from_bytes(raw[at + 28 : at + 30], "little")
        if bytes(raw[at + 46 : at + 46 + size]) == target:
            flags = int.from_bytes(raw[at + 8 : at + 10], "little") | flag_or
            raw[at + 8 : at + 10] = flags.to_bytes(2, "little")
            if method is not None:
                raw[at + 10 : at + 12] = method.to_bytes(2, "little")
            return bytes(raw)
        at = raw.find(b"PK\x01\x02", at + 4)
    raise AssertionError(name)


def _refused(blob: bytes, limits: ArchiveLimits = ArchiveLimits()) -> str:
    result = read_step_directories(blob, limits)
    assert isinstance(result, ArchiveRefused), result
    return result.reason


S2_FILE = "s2-named/3_Run printf '%s-%s_n' MARK s2-a.txt"


def test_the_recording_reads_as_six_step_directories() -> None:
    result = read_step_directories((data.STEPS_1 / "attempt-1.zip").read_bytes())
    assert not isinstance(result, ArchiveRefused)
    numbers = {d.name: [f.number for f in d.files] for d in result}
    assert numbers["s1-plain-fail"] == [1, 2, 3, 4, 8, 9]
    assert numbers["s4-composite"] == [1, 2, 3, 4, 5, 10, 11]
    assert len(result) == 6


def test_an_archive_without_step_files_is_empty_not_refused() -> None:
    kept = [
        (n, b)
        for n, b in data.archive_entries()
        if "/" not in n or n.endswith("/system.txt")
    ]
    assert read_step_directories(data.zip_of(kept)) == ()


def test_directory_entries_are_ignored() -> None:
    entries = [("s1-plain-fail/", b""), *data.archive_entries()]
    result = read_step_directories(data.zip_of(entries))
    assert not isinstance(result, ArchiveRefused) and len(result) == 6


def test_a_duplicate_entry_name_refuses_the_archive() -> None:
    entries = data.archive_entries()
    assert "duplicate" in _refused(data.zip_of([*entries, entries[3]]))


def test_a_truncated_archive_is_corrupt() -> None:
    blob = data.zip_of(data.archive_entries())
    assert "corrupt" in _refused(blob[: len(blob) - 100])


def test_damage_inside_an_unread_entry_goes_unnoticed() -> None:
    """Spec §7.3 (rev 2.2): ``system.txt`` and top-level files are never
    decompressed, so a CRC mismatch inside one is not detected. Binding
    never reads them."""
    entries = [
        ("s1-plain-fail/system.txt", b"SYSTEM-unique-payload\n"),
        ("s1-plain-fail/3_x.txt", b"x\n"),
    ]
    blob = bytearray(data.zip_of(entries, method=zipfile.ZIP_STORED))
    at = blob.find(b"SYSTEM-unique-payload")
    blob[at] ^= 0x01
    result = read_step_directories(bytes(blob))
    assert not isinstance(result, ArchiveRefused)
    assert [f.number for d in result for f in d.files] == [3]


def test_a_crc_mismatch_is_corrupt() -> None:
    entries = [(S2_FILE, b"MARK-unique-payload\n")]
    blob = bytearray(data.zip_of(entries, method=zipfile.ZIP_STORED))
    at = blob.find(b"MARK-unique-payload")
    blob[at] ^= 0x01
    assert "corrupt" in _refused(bytes(blob))


def test_an_encrypted_entry_refuses_the_archive() -> None:
    blob = data.zip_of(data.archive_entries())
    assert "encrypted" in _refused(_patch_central(blob, S2_FILE, flag_or=0x1))


def test_an_unsupported_method_refuses_the_archive() -> None:
    blob = data.zip_of(data.archive_entries())
    assert "method" in _refused(_patch_central(blob, S2_FILE, method=12))


def test_a_step_file_that_is_not_utf8_refuses_the_archive() -> None:
    entries = data.edited(
        data.archive_entries(), "s3-two-failures/4_", lambda b: b"\xff" + b
    )
    assert "UTF-8" in _refused(data.zip_of(entries))


def test_an_unreadable_copy_beside_a_match_still_refuses() -> None:
    entries = data.archive_entries()
    copy = [
        ("copy/" + n.partition("/")[2], b"\xff" + b)
        for n, b in entries
        if n.startswith("s1-plain-fail/") and not n.endswith("system.txt")
    ]
    assert "UTF-8" in _refused(data.zip_of(entries + copy))


@pytest.mark.parametrize(
    "bad", ["03_x.txt", "+3_x.txt", "x.txt", "3_x.log", "sub/3_x.txt"]
)
def test_a_malformed_step_entry_name_refuses_the_archive(bad: str) -> None:
    entries = [*data.archive_entries(), (f"s2-named/{bad}", b"x\n")]
    assert "not <N>_<name>.txt" in _refused(data.zip_of(entries))


def test_a_directory_of_only_malformed_names_refuses_rather_than_vanishes() -> None:
    entries = [
        *data.archive_entries(),
        ("extra/+1_a.txt", b"a\n"),
        ("extra/x.txt", b"b\n"),
    ]
    assert "not <N>_<name>.txt" in _refused(data.zip_of(entries))


def test_a_repeated_number_in_one_directory_refuses_the_archive() -> None:
    entries = [*data.archive_entries(), ("s2-named/4_another name.txt", b"x\n")]
    assert "repeats" in _refused(data.zip_of(entries))


def test_the_entry_count_limit() -> None:
    blob = data.zip_of(data.archive_entries())
    assert "entries" in _refused(blob, ArchiveLimits(entries=10))


def test_the_one_entry_limit() -> None:
    blob = data.zip_of(data.archive_entries())
    assert "exceeds" in _refused(blob, ArchiveLimits(entry=1000))


def test_the_total_limit() -> None:
    blob = data.zip_of(data.archive_entries())
    assert "total" in _refused(blob, ArchiveLimits(total=20_000))
