"""Private, bounded Linux tree inventories and GNU tar preflight checks."""

import base64
import hashlib
import os
import stat
import tarfile
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_BYTES = 8 * 1024**3
MAX_FILE_BYTES = 4 * 1024**3
MAX_TOTAL_BYTES = 8 * 1024**3
MAX_MEMBERS = 100_000
MAX_MANIFEST_BYTES = 16 * 1024**2


class CheckpointError(RuntimeError):
    """Only fixed codes escape the private checkpoint boundary."""


def digest(stream):
    stream.seek(0)
    result = hashlib.file_digest(stream, "sha256").hexdigest()
    stream.seek(0)
    return result


def member_name(value):
    if value.startswith("./"):
        value = value[2:]
    value = value.rstrip("/")
    if value == ".":
        return value
    parts = value.split("/")
    if not value or any(part in {"", ".", ".."} for part in parts) or "\x00" in value:
        raise CheckpointError("unsafe_member")
    return value


def internal_link(root, path, target):
    if not target or os.path.isabs(target):
        raise CheckpointError("external_link")
    try:
        (path.parent / target).resolve().relative_to(root)
    except (ValueError, RuntimeError):
        raise CheckpointError("external_link") from None


def inventory(root: Path):
    """Do not follow links; reject unaccounted inode aliases and special files."""
    entries, identities, inodes = {}, {}, {}
    total = 0

    def visit(path, name):
        nonlocal total
        info = path.lstat()
        identity = (info.st_dev, info.st_ino, info.st_ctime_ns, info.st_nlink)
        identities[name] = identity
        record = {
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mtime_ns": info.st_mtime_ns,
            "xattrs": {
                key: base64.b64encode(os.getxattr(path, key, follow_symlinks=False)).decode()
                for key in sorted(os.listxattr(path, follow_symlinks=False))
            },
        }
        if stat.S_ISDIR(info.st_mode):
            record["kind"] = "directory"
        elif stat.S_ISREG(info.st_mode):
            if info.st_size > MAX_FILE_BYTES:
                raise CheckpointError("size_limit")
            total += info.st_size
            record.update(kind="file", size=info.st_size)
            with path.open("rb") as stream:
                record["sha256"] = digest(stream)
            key = (info.st_dev, info.st_ino)
            inodes.setdefault(key, []).append(name)
        elif stat.S_ISLNK(info.st_mode):
            if info.st_nlink != 1:
                raise CheckpointError("unaccounted_link")
            target = os.readlink(path)
            internal_link(root, path, target)
            record.update(kind="symlink", target=target)
        else:
            raise CheckpointError("unsupported_type")
        entries[name] = record
        if len(entries) > MAX_MEMBERS or total > MAX_TOTAL_BYTES:
            raise CheckpointError("size_limit")
        if record["kind"] == "directory":
            for child in sorted(path.iterdir()):
                visit(child, str(child.relative_to(root)))
        after = path.lstat()
        if (
            after.st_dev,
            after.st_ino,
            after.st_ctime_ns,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        ) != (*identity, info.st_size, info.st_mtime_ns):
            raise CheckpointError("source_changed")

    visit(root, ".")
    for names in inodes.values():
        if len(names) != identities[names[0]][3]:
            raise CheckpointError("unaccounted_link")
        leader = min(names)
        for name in names:
            entries[name]["hardlink_group"] = leader
    return entries, identities


class _BoundedTarInfo(tarfile.TarInfo):
    def _proc_pax(self, tarfile_object):
        if self.size > MAX_MANIFEST_BYTES:
            raise CheckpointError("size_limit")
        return super()._proc_pax(tarfile_object)


def preflight(stream, entries):
    """Match the complete closed member set before GNU tar can write anything."""
    stream.seek(0)
    seen = {}
    total = 0
    with tarfile.open(fileobj=stream, mode="r:", tarinfo=_BoundedTarInfo) as archive:
        for member in archive:
            name = member_name(member.name)
            if name in seen or name not in entries:
                raise CheckpointError("member_set_mismatch")
            if member.sparse is not None or any(
                k.startswith("GNU.sparse") for k in member.pax_headers
            ):
                raise CheckpointError("unsupported_type")
            expected = entries[name]
            kind = expected["kind"]
            if kind == "directory" and member.isdir():
                pass
            elif kind == "symlink" and member.issym():
                if member.linkname != expected["target"]:
                    raise CheckpointError("link_mismatch")
            elif kind == "file" and member.isfile():
                if member.size != expected["size"] or member.size > MAX_FILE_BYTES:
                    raise CheckpointError("size_limit")
                total += member.size
                with archive.extractfile(member) as payload:
                    if hashlib.file_digest(payload, "sha256").hexdigest() != expected["sha256"]:
                        raise CheckpointError("content_mismatch")
            elif kind == "file" and member.islnk():
                target = member_name(member.linkname)
                if (
                    target not in seen
                    or entries[target]["kind"] != "file"
                    or (entries[target]["hardlink_group"] != expected["hardlink_group"])
                ):
                    raise CheckpointError("external_link")
            else:
                raise CheckpointError("unsupported_type")
            if member.size < 0 or (not member.isfile() and member.size != 0):
                raise CheckpointError("size_limit")
            if name != ".":
                for parent in PurePosixPath(name).parents:
                    parent_name = str(parent)
                    if parent_name not in entries or entries[parent_name]["kind"] != "directory":
                        raise CheckpointError("unsafe_ancestry")
            seen[name] = member
            if total > MAX_TOTAL_BYTES or len(seen) > MAX_MEMBERS:
                raise CheckpointError("size_limit")
        end = archive.offset
    if seen.keys() != entries.keys():
        raise CheckpointError("member_set_mismatch")
    stream.seek(end)
    trailer_size = 0
    while block := stream.read(1024 * 1024):
        trailer_size += len(block)
        if any(block):
            raise CheckpointError("archive_trailer")
    if trailer_size < 1024:
        raise CheckpointError("archive_truncated")
    stream.seek(0)
