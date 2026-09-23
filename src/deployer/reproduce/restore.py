"""Unpack the forge's tarball and prove it equals the Git tree (§1.3 b,c; §1.4).

Extraction uses ``tarfile``'s ``data`` filter (no absolute paths, no ``..``,
no links out of the destination, no devices; executable bits kept) and strips
the single top-level directory GitHub adds. Nothing from the archive runs.
"""

import hashlib
import io
import os
import tarfile
from pathlib import Path

from deployer.forge import TreeListing

_MODE_EXEC = "100755"
_MODE_LINK = "120000"
_MODE_GITLINK = "160000"
_DIFF = "archive differs from tree listing"


def git_blob_sha(data: bytes) -> str:
    """Git's blob object id of ``data``."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def extract(archive: bytes, dest: Path) -> str | None:
    """Unpack into ``dest``; the reason it could not, or ``None``."""
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
            members = tar.getmembers()
            tops = {m.name.split("/", 1)[0] for m in members}
            if len(tops) != 1:
                return f"archive has {len(tops)} top-level entries"
            prefix = tops.pop() + "/"
            renamed = [_strip(m, prefix) for m in members if m.name.startswith(prefix)]
            dest.mkdir(parents=True, exist_ok=True)
            tar.extractall(dest, members=renamed, filter="data")
    except tarfile.FilterError as exc:
        return f"archive member refused: {exc}"
    except (tarfile.TarError, EOFError) as exc:
        return f"archive unreadable: {exc.__class__.__name__}"
    except OSError as exc:
        return f"archive not written: {exc}"
    return None


def listing_conditions(dest: Path, listing: TreeListing) -> list[str]:
    """Unmet (b) ``.gitattributes`` and (c) path/mode/content equality."""
    unmet: list[str] = []
    if listing.truncated:
        unmet.append("tree listing truncated")
    expected = {}
    for entry in listing.entries:
        if entry.path.rsplit("/", 1)[-1] == ".gitattributes":
            unmet.append(f".gitattributes present: {entry.path}")
        if entry.mode == _MODE_GITLINK:
            unmet.append(f"submodule entry {entry.path}")
        elif entry.type == "blob":
            expected[entry.path] = entry
    on_disk = _files(dest)
    for path in sorted(set(expected) - set(on_disk)):
        unmet.append(f"{_DIFF}: missing {path}")
    for path in sorted(set(on_disk) - set(expected)):
        unmet.append(f"{_DIFF}: extra {path}")
    for path in sorted(set(expected) & set(on_disk)):
        entry = expected[path]
        unmet.extend(_compare(dest / path, path, entry.mode, entry.sha))
    return unmet


def make_tarball(tree: Path, prefix: str) -> bytes:
    """A GitHub-shaped tarball of ``tree`` under one top-level ``prefix``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(tree, arcname=prefix, recursive=True)
    return buf.getvalue()


def _strip(member: tarfile.TarInfo, prefix: str) -> tarfile.TarInfo:
    name = member.name[len(prefix) :] or "."
    if member.islnk() and member.linkname.startswith(prefix):
        return member.replace(name=name, linkname=member.linkname[len(prefix) :])
    return member.replace(name=name)


def _files(root: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in filenames:
            out.append((base / name).relative_to(root).as_posix())
        for name in dirnames:
            if (base / name).is_symlink():
                out.append((base / name).relative_to(root).as_posix())
    return out


def _compare(file: Path, path: str, mode: str, sha: str) -> list[str]:
    if mode == _MODE_LINK:
        if not file.is_symlink():
            return [f"{_DIFF}: mode of {path}"]
        target = os.readlink(file).encode()
        if git_blob_sha(target) == sha:
            return []
        return [f"{_DIFF}: symlink target of {path}"]
    if file.is_symlink():
        return [f"{_DIFF}: mode of {path}"]
    executable = bool(file.stat().st_mode & 0o111)
    problems: list[str] = []
    if executable != (mode == _MODE_EXEC):
        problems.append(f"{_DIFF}: mode of {path}")
    if git_blob_sha(file.read_bytes()) != sha:
        problems.append(f"{_DIFF}: content of {path}")
    return problems
