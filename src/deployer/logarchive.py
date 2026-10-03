"""Reading the per-attempt log archive: in memory, bounded, all or nothing.

Design: ``docs/superpowers/specs/2026-10-03-forge-archive-step-binding-design.md``
§4.2, §7.1, §7.3. Discovery is separate from validation: every top-level
directory's entries other than ``system.txt`` are its step entries, whatever
their names, and any one that cannot be read before ownership is known —
a malformed name, a repeated number, text that is not UTF-8 — refuses the
whole archive. Excluding it could make another directory look unique.

Corruption boundary (§7.3): central-directory checks apply to every entry;
decompression and CRC run only on the step entries.
"""

import io
import re
import zipfile
import zlib
from collections import Counter
from dataclasses import dataclass

from deployer.stepbinding import StepDirectory, StepFile

MIB = 2**20
_CHUNK = 64 * 1024
_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_STEP_NAME_RE = re.compile(r"(0|[1-9][0-9]*)_[^/]*\.txt", re.DOTALL)
_SYSTEM = "system.txt"


@dataclass(frozen=True)
class ArchiveLimits:
    """Spec §7.1: constants, lowered only by tests."""

    download: int = 64 * MIB
    entries: int = 4096
    entry: int = 64 * MIB
    total: int = 256 * MIB


@dataclass(frozen=True)
class ArchiveRefused:
    """The archive cannot be read whole; ``reason`` says why (state ``refused``)."""

    reason: str


def read_step_directories(
    blob: bytes, limits: ArchiveLimits = ArchiveLimits()
) -> tuple[StepDirectory, ...] | ArchiveRefused:
    """The archive's step directories, or why the archive is refused.

    An empty tuple means the archive holds no step directory (``absent``).
    """
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            return _read(archive, limits)
    except (zipfile.BadZipFile, zlib.error, EOFError, ValueError) as exc:
        return ArchiveRefused(f"corrupt archive: {exc}")


def _check_entries(
    infos: list[zipfile.ZipInfo], limits: ArchiveLimits
) -> ArchiveRefused | None:
    """Central-directory checks on every entry; nothing is decompressed."""
    if len(infos) > limits.entries:
        return ArchiveRefused(
            f"{len(infos)} entries exceed the limit of {limits.entries}"
        )
    counts = Counter(i.filename for i in infos)
    repeated = sorted(n for n, c in counts.items() if c > 1)
    if repeated:
        return ArchiveRefused(f"duplicate entry name(s): {repeated}")
    for info in infos:
        if info.flag_bits & 0x1:
            return ArchiveRefused(f"encrypted entry: {info.filename!r}")
        if info.compress_type not in _METHODS:
            return ArchiveRefused(
                f"unsupported compression method {info.compress_type}: "
                f"{info.filename!r}"
            )
        if info.file_size > limits.entry:
            return ArchiveRefused(
                f"entry {info.filename!r} exceeds {limits.entry} bytes uncompressed"
            )
    return None


def _discover(infos: list[zipfile.ZipInfo]) -> dict[str, list[zipfile.ZipInfo]]:
    """Step entries by top-level directory, whatever their names."""
    groups: dict[str, list[zipfile.ZipInfo]] = {}
    for info in infos:
        head, sep, rest = info.filename.partition("/")
        if sep and rest and rest != _SYSTEM and not info.is_dir():
            groups.setdefault(head, []).append(info)
    return groups


def _read(
    archive: zipfile.ZipFile, limits: ArchiveLimits
) -> tuple[StepDirectory, ...] | ArchiveRefused:
    infos = archive.infolist()
    refused = _check_entries(infos, limits)
    if refused is not None:
        return refused
    directories: list[StepDirectory] = []
    budget = limits.total
    for name, members in sorted(_discover(infos).items()):
        files: list[StepFile] = []
        for info in members:
            match = _STEP_NAME_RE.fullmatch(info.filename.partition("/")[2])
            if match is None:
                return ArchiveRefused(
                    f"step entry {info.filename!r} is not <N>_<name>.txt"
                )
            raw = _read_entry(archive, info, limits.entry, budget)
            if isinstance(raw, ArchiveRefused):
                return raw
            budget -= len(raw)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return ArchiveRefused(f"step file {info.filename!r} is not UTF-8")
            files.append(StepFile(int(match.group(1)), text))
        twice = sorted(n for n, c in Counter(f.number for f in files).items() if c > 1)
        if twice:
            return ArchiveRefused(f"directory {name!r} repeats step number(s) {twice}")
        ordered = tuple(sorted(files, key=lambda f: f.number))
        directories.append(StepDirectory(name, ordered))
    return tuple(directories)


def _read_entry(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, entry_limit: int, budget: int
) -> bytes | ArchiveRefused:
    """One entry, decompressed in bounded chunks against both limits."""
    chunks: list[bytes] = []
    size = 0
    with archive.open(info) as stream:
        while chunk := stream.read(_CHUNK):
            size += len(chunk)
            if size > entry_limit:
                return ArchiveRefused(
                    f"entry {info.filename!r} exceeds {entry_limit} bytes uncompressed"
                )
            if size > budget:
                return ArchiveRefused("entries exceed the total uncompressed limit")
            chunks.append(chunk)
    return b"".join(chunks)
