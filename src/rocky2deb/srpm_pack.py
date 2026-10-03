"""Pack a spec and source files into an unsigned RPM v4 .src.rpm.

Threats: a hostile spec can name files outside the archive, and a huge
payload can exhaust the controller. Member names are basenames, symlinks are
refused, and the blob stops at the size cap. The spec text is stored. %prep,
%build, and %install are not executed. SHA1 and MD5 bytes written into the
signature header are RPM v4 format fields, not a trust decision, and this
module does not compare them. The package is not signed. rpm and mock are
not invoked, so this writer does not claim either tool accepted the blob.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from rocky2deb.errors import Refused
from rocky2deb.fetch import DEFAULT_LIMIT
from rocky2deb.srpm import _HEADER_MAGIC, _LEAD, _LEAD_MAGIC, _skip_header, _u32

_MEMBER = re.compile(r"^[A-Za-z0-9._+-]+$")
_REGION_IMMUTABLE = 63
_REGION_SIGNATURE = 62
_SIG_SHA1 = 269
_SIG_SHA256 = 273
_SIG_SIZE = 1000
_SIG_MD5 = 1004
_SIG_PAYLOADSIZE = 1007


@dataclass(frozen=True)
class RpmIdentity:
    name: str
    version: str
    release: str
    epoch: str
    arch: str
    license: str
    summary: str
    description: str
    sourcerpm: str


@dataclass
class _Field:
    tag: int
    typ: int
    data: bytes
    count: int
    align: int


def refuse_spec_field(value: str, *, allow_space: bool) -> None:
    """Refuse a preamble value that must not be copied into a spec or header.

    The value is not included in the error.
    """
    if value is None:
        raise Refused("refusing spec field")
    for char in value:
        code = ord(char)
        if code < 32 or code > 126 or char == "%":
            raise Refused("refusing spec field")
        if not allow_space and char == " ":
            raise Refused("refusing spec field")
    if not allow_space and not value:
        raise Refused("refusing spec field")


def _member_name(name: str) -> None:
    if (
        not name
        or name.startswith(".")
        or name.startswith("-")
        or ".." in name
        or "/" in name
        or "\\" in name
        or _MEMBER.fullmatch(name) is None
    ):
        raise Refused("refusing source file")


def _string_field(tag: int, value: str, typ: int = 6) -> _Field:
    return _Field(tag, typ, value.encode("utf-8") + b"\x00", 1, 1)


def _i32_field(tag: int, value: int) -> _Field:
    if value < 0 or value > 0xFFFFFFFF:
        raise Refused("rpm header exceeds size cap")
    return _Field(tag, 4, value.to_bytes(4, "big"), 1, 4)


def _bin_field(tag: int, data: bytes) -> _Field:
    return _Field(tag, 7, data, len(data), 1)


def _trailer(region_tag: int, nindex: int) -> bytes:
    offset = -(nindex * 16)
    return (
        region_tag.to_bytes(4, "big")
        + (7).to_bytes(4, "big")
        + offset.to_bytes(4, "big", signed=True)
        + (16).to_bytes(4, "big")
    )


def _build_header(region_tag: int, fields: list[_Field]) -> bytes:
    fields = sorted(fields, key=lambda item: item.tag)
    seen: set[int] = set()
    for field in fields:
        if field.tag <= region_tag or field.tag in seen:
            raise Refused("rpm header tag is duplicated")
        seen.add(field.tag)
    nindex = 1 + len(fields)
    store = bytearray()
    entries: list[tuple[int, int, int, int]] = []
    for field in fields:
        while len(store) % field.align:
            store.append(0)
        entries.append((field.tag, field.typ, len(store), field.count))
        store.extend(field.data)
    trailer_at = len(store)
    store += _trailer(region_tag, nindex)
    while len(store) % 8:
        store.append(0)
    index = [(region_tag, 7, trailer_at, 16), *entries]
    intro = (
        _HEADER_MAGIC
        + b"\x00\x00\x00\x00"
        + nindex.to_bytes(4, "big")
        + len(store).to_bytes(4, "big")
    )
    body = bytearray()
    for tag, typ, offset, count in index:
        body += (
            tag.to_bytes(4, "big")
            + typ.to_bytes(4, "big")
            + offset.to_bytes(4, "big", signed=True)
            + count.to_bytes(4, "big")
        )
    return bytes(intro + body + store)


def _newc(name: str, data: bytes, mode: int) -> bytes:
    raw_name = name.encode("utf-8") + b"\x00"
    numbers = [0, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(raw_name), 0]
    header = b"070701" + b"".join(f"{number:08x}".encode() for number in numbers)
    entry = header + raw_name
    entry += b"\x00" * ((4 - (len(entry) % 4)) % 4)
    entry += data
    entry += b"\x00" * ((4 - (len(data) % 4)) % 4)
    return entry


def _cpio(members: list[tuple[str, bytes]]) -> bytes:
    chunks = [_newc(name, data, 0o100644) for name, data in members]
    chunks.append(_newc("TRAILER!!!", b"", 0))
    return b"".join(chunks)


def _preamble(text: str) -> tuple[dict[str, str], str]:
    fields: dict[str, str] = {}
    description: list[str] = []
    mode = "preamble"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("%"):
            if stripped == "%description" or stripped.startswith("%description "):
                mode = "description"
            else:
                mode = "other"
            continue
        if mode == "preamble":
            if not stripped or stripped.startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            if key not in fields:
                fields[key] = value.strip()
        elif mode == "description":
            description.append(line)
    return fields, "\n".join(description).strip()


def _lead(filename: str) -> bytes:
    raw = filename.encode("utf-8")
    if len(raw) > 65:
        raise Refused("refusing spec field")
    name = raw + b"\x00" * (66 - len(raw))
    return (
        _LEAD_MAGIC
        + bytes((3, 0))
        + (1).to_bytes(2, "big")
        + (1).to_bytes(2, "big")
        + name
        + (1).to_bytes(2, "big")
        + (5).to_bytes(2, "big")
        + b"\x00" * 16
    )


def _main_fields(identity: dict[str, str], description: str, payload_size: int) -> list[_Field]:
    fields = [
        _string_field(100, "C", 8),
        _string_field(1000, identity["name"]),
        _string_field(1001, identity["version"]),
        _string_field(1002, identity["release"]),
        _string_field(1004, identity["summary"], 9),
        _string_field(1005, description, 9),
        _i32_field(1006, 0),
        _string_field(1007, "localhost"),
        _i32_field(1009, payload_size),
        _string_field(1014, identity["license"]),
        _string_field(1016, "Unspecified"),
        _string_field(1021, "linux"),
        _string_field(1022, "src"),
        _string_field(1044, ""),
        _string_field(1124, "cpio"),
        _string_field(1125, "gzip"),
        _string_field(1126, "9"),
        _string_field(5062, "utf-8"),
    ]
    if identity["epoch"]:
        fields.append(_i32_field(1003, int(identity["epoch"])))
    return fields


def _identity_from_spec(text: str) -> tuple[dict[str, str], str]:
    fields, description = _preamble(text)
    for key in ("name", "version", "release"):
        if key not in fields or not fields[key]:
            raise Refused("missing spec field")
    name = fields["name"]
    version = fields["version"]
    release = fields["release"]
    summary = fields.get("summary") or name
    license_text = fields.get("license") or "unknown"
    epoch = fields.get("epoch", "")
    for value in (name, version, release, epoch):
        if value:
            refuse_spec_field(value, allow_space=False)
    refuse_spec_field(summary, allow_space=True)
    refuse_spec_field(license_text, allow_space=True)
    if epoch:
        if not epoch.isdigit() or int(epoch) > 0xFFFFFFFF:
            raise Refused("refusing spec field")
        if int(epoch) == 0:
            epoch = ""
    for char in description:
        code = ord(char)
        if char in "\n\t":
            continue
        if code < 32 or code > 126:
            raise Refused("refusing spec field")
    if not description:
        description = summary
    _member_name(f"{name}.spec")
    filename = f"{name}-{version}-{release}.src.rpm"
    if len(filename.encode("utf-8")) > 65:
        raise Refused("refusing spec field")
    return {
        "name": name,
        "version": version,
        "release": release,
        "epoch": epoch,
        "summary": summary,
        "license": license_text,
        "filename": filename,
    }, description


def pack_src_rpm(
    spec: bytes,
    sources: list[tuple[str, bytes]] | None,
    dest: Path,
    *,
    limit: int = DEFAULT_LIMIT,
) -> Path:
    """Write an unsigned .src.rpm. The spec is not executed."""
    if limit < 1:
        raise Refused("size limit must be positive")
    if len(spec) > limit:
        raise Refused(f"input exceeds {limit} bytes")
    try:
        text = spec.decode("utf-8")
    except UnicodeError as exc:
        raise Refused("spec is not utf-8") from exc
    identity, description = _identity_from_spec(text)
    members: list[tuple[str, bytes]] = [(f"{identity['name']}.spec", spec)]
    seen = {members[0][0]}
    total = len(spec)
    for name, data in sources or []:
        _member_name(name)
        if name.endswith(".spec") or name in seen:
            raise Refused("duplicate source file")
        if len(data) > limit or total + len(data) > limit:
            raise Refused(f"input exceeds {limit} bytes")
        seen.add(name)
        total += len(data)
        members.append((name, data))
    members = [members[0], *sorted(members[1:], key=lambda item: item[0])]
    archive = _cpio(members)
    if len(archive) > limit or len(archive) > 0xFFFFFFFF:
        raise Refused(f"input exceeds {limit} bytes")
    payload = gzip.compress(archive, compresslevel=9, mtime=0)
    if len(payload) > limit or len(payload) > 0xFFFFFFFF:
        raise Refused(f"input exceeds {limit} bytes")
    main = _build_header(_REGION_IMMUTABLE, _main_fields(identity, description, len(archive)))
    # SHA1 and MD5 are RPM v4 signature-header fields. They are not compared.
    sha1 = hashlib.sha1(main).hexdigest()
    sha256 = hashlib.sha256(main).hexdigest()
    md5 = hashlib.md5(main + payload).digest()
    signature = _build_header(
        _REGION_SIGNATURE,
        [
            _string_field(_SIG_SHA1, sha1),
            _string_field(_SIG_SHA256, sha256),
            _i32_field(_SIG_SIZE, len(payload)),
            _bin_field(_SIG_MD5, md5),
            _i32_field(_SIG_PAYLOADSIZE, len(archive)),
        ],
    )
    blob = _lead(identity["filename"]) + signature + main + payload
    if len(blob) > limit:
        raise Refused(f"input exceeds {limit} bytes")
    target = Path(dest)
    if target.is_symlink():
        raise Refused("refusing symlink destination")
    if target.exists() and not target.is_file():
        raise Refused("destination is not a file")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.write_bytes(blob)
    partial.chmod(0o644)
    os.replace(partial, target)
    return target


def _cstring(store: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(store):
        raise Refused("rpm header is incomplete")
    end = store.find(b"\x00", offset)
    if end < 0:
        raise Refused("rpm header is incomplete")
    try:
        return store[offset:end].decode("utf-8")
    except UnicodeError as exc:
        raise Refused("rpm header is not utf-8") from exc


def _parse_index(blob: bytes, offset: int, limit: int) -> tuple[dict[int, tuple[int, int, int]], bytes]:
    if offset + 16 > len(blob) or blob[offset : offset + 4] != _HEADER_MAGIC:
        raise Refused("rpm header magic is wrong")
    count = _u32(blob, offset + 8)
    store_len = _u32(blob, offset + 12)
    if count > 100_000 or store_len > limit:
        raise Refused("src.rpm header exceeds size cap")
    index_end = offset + 16 + count * 16
    data_end = index_end + store_len
    if data_end > len(blob):
        raise Refused("src.rpm header is truncated")
    entries: dict[int, tuple[int, int, int]] = {}
    for index in range(count):
        at = offset + 16 + index * 16
        tag = _u32(blob, at)
        typ = _u32(blob, at + 4)
        field_offset = int.from_bytes(blob[at + 8 : at + 12], "big", signed=True)
        field_count = _u32(blob, at + 12)
        entries[tag] = (typ, field_offset, field_count)
    return entries, blob[index_end:data_end]


def _need(entries: dict[int, tuple[int, int, int]], store: bytes, tag: int) -> str:
    if tag not in entries:
        raise Refused("rpm header is incomplete")
    typ, offset, count = entries[tag]
    if typ not in (6, 9) or count != 1:
        raise Refused("rpm header is incomplete")
    return _cstring(store, offset)


def read_rpm_identity(blob: bytes, *, limit: int = DEFAULT_LIMIT) -> RpmIdentity:
    """Read name, version, and related tags. Does not trust a signature."""
    if limit < 1:
        raise Refused("size limit must be positive")
    if len(blob) > limit:
        raise Refused(f"input exceeds {limit} bytes")
    if len(blob) < _LEAD + 16 or blob[:4] != _LEAD_MAGIC:
        raise Refused("rpm header magic is wrong")
    try:
        main_at = _skip_header(blob, _LEAD, limit)
    except Refused as exc:
        if "magic" in str(exc):
            raise Refused("rpm header magic is wrong") from exc
        raise
    entries, store = _parse_index(blob, main_at, limit)
    epoch = ""
    if 1003 in entries:
        typ, offset, count = entries[1003]
        if typ != 4 or count != 1 or offset < 0 or offset + 4 > len(store):
            raise Refused("rpm header is incomplete")
        value = int.from_bytes(store[offset : offset + 4], "big")
        epoch = "" if value == 0 else str(value)
    summary = _cstring(store, entries[1004][1]) if 1004 in entries else ""
    description = _cstring(store, entries[1005][1]) if 1005 in entries else ""
    license_text = _cstring(store, entries[1014][1]) if 1014 in entries else ""
    sourcerpm = _cstring(store, entries[1044][1]) if 1044 in entries else ""
    return RpmIdentity(
        name=_need(entries, store, 1000),
        version=_need(entries, store, 1001),
        release=_need(entries, store, 1002),
        epoch=epoch,
        arch=_need(entries, store, 1022),
        license=license_text,
        summary=summary,
        description=description,
        sourcerpm=sourcerpm,
    )
