"""The no-follow reader shared by the preparation layer (A §2.4, §4.3,
§6.1): every file under a checked tree is read through a component-wise
walk that never follows a symlink, so a committed link cannot lead a read
out of the tree.
"""

import errno
import os
import stat
from pathlib import Path

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class Unreadable(OSError):
    """A path in the checked tree that is not read: missing, not a plain
    relative path, behind a symlink, or not a regular file."""


def read_in_tree(root: Path, rel: str, max_bytes: int | None = None) -> bytes:
    """The bytes of regular file ``rel`` under ``root``, opened one
    component at a time without following any symlink; a file longer than
    ``max_bytes`` is refused."""
    if "\x00" in rel:
        raise Unreadable(f"{rel!r} contains a NUL byte")
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise Unreadable(f"{rel} is not a plain relative path")
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for depth, name in enumerate(parts[:-1], start=1):
            sub = "/".join(parts[:depth])
            next_fd = _open_at(dir_fd, name, _DIR_FLAGS, sub, "a directory")
            os.close(dir_fd)
            dir_fd = next_fd
        fd = _open_at(dir_fd, parts[-1], _FILE_FLAGS, rel, "a regular file")
    finally:
        os.close(dir_fd)
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError:
        os.close(fd)
        raise
    if not regular:
        os.close(fd)
        raise Unreadable(f"{rel} is not a regular file; not read")
    with os.fdopen(fd, "rb") as f:
        data = f.read() if max_bytes is None else f.read(max_bytes + 1)
    if max_bytes is not None and len(data) > max_bytes:
        raise Unreadable(f"{rel} exceeds {max_bytes} bytes; not read")
    return data


def _open_at(dir_fd: int, name: str, flags: int, rel: str, kind: str) -> int:
    """``openat`` with no-follow flags, mapping refusals to ``Unreadable``."""
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        raise Unreadable(f"{rel} is missing") from None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise Unreadable(
                f"{rel} is a symlink or not {kind}; not followed"
            ) from None
        raise
