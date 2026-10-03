"""Extract a .src.rpm without running its scripts.

Threats: a hostile archive can write outside the destination with '..' names,
absolute paths, or symlinks, and a compressed payload can expand without
bound. Extraction refuses those names, refuses symlinks, and stops at the
size cap. It does not execute %prep or any file it writes. It does not check
a signature; the caller does that before calling this module.
"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
from pathlib import Path

from rocky2deb.errors import Refused
from rocky2deb.fetch import DEFAULT_LIMIT, read_limited

_LEAD = 96
_LEAD_MAGIC = b"\xed\xab\xee\xdb"
_HEADER_MAGIC = b"\x8e\xad\xe8\x01"
_MAX_FILES = 10000
_CPIO_MAGICS = (b"070701", b"070702")


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "big")


def _skip_header(data: bytes, offset: int, limit: int) -> int:
    if offset + 16 > len(data) or data[offset : offset + 4] != _HEADER_MAGIC:
        raise Refused("src.rpm header magic is wrong")
    count = _u32(data, offset + 8)
    store = _u32(data, offset + 12)
    if count > 100_000 or store > limit:
        raise Refused("src.rpm header exceeds size cap")
    region = 16 + count * 16 + store
    if offset + region > len(data):
        raise Refused("src.rpm header is truncated")
    pad = (8 - (region % 8)) % 8
    return offset + region + pad


def _decompress(payload: bytes, limit: int) -> bytes:
    if payload.startswith(b"\x1f\x8b"):
        stream = gzip.GzipFile(fileobj=io.BytesIO(payload))
    elif payload.startswith(b"\xfd7zXZ\x00"):
        stream = lzma.LZMAFile(io.BytesIO(payload))
    elif payload.startswith(b"BZh"):
        stream = bz2.BZ2File(io.BytesIO(payload))
    elif payload.startswith(_CPIO_MAGICS):
        return payload
    elif payload.startswith(b"\x28\xb5\x2f\xfd"):
        raise Refused("src.rpm payload is zstd; install zstd on the controller")
    else:
        raise Refused("src.rpm payload is not a cpio archive")
    try:
        return read_limited(stream, limit)
    except Refused as exc:
        if str(exc).startswith("input exceeds"):
            raise Refused("src.rpm exceeds size cap") from exc
        raise
    except Exception as exc:
        raise Refused("src.rpm payload did not decompress") from exc


def _relative(name: str) -> Path:
    text = name.replace("\\", "/")
    if text.startswith("/"):
        raise Refused("refusing src.rpm path")
    while text.startswith("./"):
        text = text[2:]
    parts = [part for part in text.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise Refused("refusing src.rpm path")
    return Path(*parts)


def _cpio_entries(archive: bytes, limit: int):
    offset = 0
    seen = 0
    total = 0
    while offset + 110 <= len(archive):
        magic = archive[offset : offset + 6]
        if magic not in _CPIO_MAGICS:
            raise Refused("src.rpm cpio magic is wrong")
        try:
            filesize = int(archive[offset + 54 : offset + 62], 16)
            namesize = int(archive[offset + 94 : offset + 102], 16)
            mode = int(archive[offset + 14 : offset + 22], 16)
        except ValueError as exc:
            raise Refused("src.rpm cpio header is corrupt") from exc
        if namesize < 1 or namesize > 4096 or filesize < 0:
            raise Refused("src.rpm cpio header is corrupt")
        name_at = offset + 110
        name_end = name_at + namesize
        if name_end > len(archive):
            raise Refused("src.rpm cpio is truncated")
        raw_name = archive[name_at : name_end - 1]
        try:
            name = raw_name.decode("utf-8")
        except UnicodeError as exc:
            raise Refused("src.rpm name is not utf-8") from exc
        data_at = name_end + ((4 - (name_end % 4)) % 4)
        data_end = data_at + filesize
        if data_end > len(archive):
            raise Refused("src.rpm cpio is truncated")
        next_at = data_end + ((4 - (filesize % 4)) % 4)
        if name.startswith("TRAILER!!!"):
            return
        seen += 1
        total += filesize
        if seen > _MAX_FILES or total > limit:
            raise Refused("src.rpm exceeds size cap")
        yield name, mode, archive[data_at:data_end]
        offset = next_at
    raise Refused("src.rpm cpio has no trailer")


def extract_src_rpm(blob: bytes, dest: Path, *, limit: int = DEFAULT_LIMIT) -> list[str]:
    """Write the archive members under dest. Return the relative names."""
    if limit < 1:
        raise Refused("size limit must be positive")
    if len(blob) > limit:
        raise Refused(f"input exceeds {limit} bytes")
    if len(blob) < _LEAD + 16 or blob[:4] != _LEAD_MAGIC:
        raise Refused("src.rpm header magic is wrong")
    main_at = _skip_header(blob, _LEAD, limit)
    payload_at = _skip_header(blob, main_at, limit)
    archive = _decompress(blob[payload_at:], limit)
    staged: list[tuple[Path, int, bytes]] = []
    for name, mode, data in _cpio_entries(archive, limit):
        relative = _relative(name)
        kind = mode & 0o170000
        if kind == 0o120000:
            raise Refused("refusing symlink in src.rpm")
        if kind not in (0o040000, 0o100000, 0):
            raise Refused("refusing special file in src.rpm")
        staged.append((relative, kind, data))
    root = Path(dest)
    if root.is_symlink():
        raise Refused("refusing symlink destination")
    root.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for relative, kind, data in staged:
        target = root.joinpath(relative)
        if root.resolve() not in target.resolve().parents and target.resolve() != root.resolve():
            raise Refused("refusing src.rpm path")
        if kind == 0o040000:
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        partial.write_bytes(data)
        partial.chmod(0o644)
        partial.replace(target)
        written.append(relative.as_posix())
    return written


def spec_in(dest: Path) -> Path:
    specs = sorted(path for path in Path(dest).rglob("*.spec") if path.is_file() and not path.is_symlink())
    if not specs:
        raise Refused("src.rpm has no spec")
    return specs[0]


def read_src_rpm(path: Path, dest: Path, *, limit: int = DEFAULT_LIMIT) -> Path:
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise Refused("src.rpm is missing")
    blob = source.read_bytes()
    extract_src_rpm(blob, dest, limit=limit)
    return spec_in(dest)
