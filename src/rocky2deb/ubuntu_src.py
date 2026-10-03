"""Export an Ubuntu source package into a Debian or Rocky source.

Threats: a tampered Sources index or pool file must not be unpacked or
published. A hostile debian/rules must not run on the controller. A symlink,
path escape, or oversized member must not write outside the staging directory.
gpgv verifies the Sources bytes. Pool files are trusted only by the SHA256 in
that verified index. MD5 in Files: is Debian compatibility and is not a trust
decision. This module does not implement signature cryptography.

Does not protect against a compromised keyring or a correctly signed but
malicious upstream. Plan mode does not download and does not create dest.
"""

from __future__ import annotations

import io
import os
import re
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rocky2deb.d2r import spec_from_tree
from rocky2deb.dscpack import pack_source
from rocky2deb.errors import Refused
from rocky2deb.fetch import (
    DEFAULT_LIMIT,
    Opener,
    Runner,
    assert_https,
    checksums_match,
    download_https,
    read_limited,
    sha256_hex,
    verify_detached,
)
from rocky2deb.license import rebuild_permission
from rocky2deb.names import deb_package_name, is_deb_base, is_deb_trademark
from rocky2deb.profiles import get_profile

UBUNTU_ARCHIVE = "https://archive.ubuntu.com/ubuntu"
# Release list checked 2026-09-23. Resolute 26.04 LTS was released 2026-04-23.
# Do not invent a codename that Ubuntu has not published.
UBUNTU_SUITES = {
    "jammy": "22.04",
    "noble": "24.04",
    "oracular": "24.10",
    "plucky": "25.04",
    "questing": "25.10",
    "resolute": "26.04",
}

_MAX_FILES = 32
_MAX_MEMBERS = 10000
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_CHANGELOG_RE = re.compile(r"^(\S+)\s+\(([^)]+)\)(.*)$")


@dataclass(frozen=True)
class SourceFile:
    name: str
    size: int
    sha256: str
    md5: str = ""


@dataclass(frozen=True)
class SourceStanza:
    package: str
    version: str
    directory: str
    binaries: tuple[str, ...]
    files: tuple[SourceFile, ...]


@dataclass(frozen=True)
class ExportPlan:
    package: str
    version: str
    distro: str
    ubuntu_suite: str
    files: tuple[SourceFile, ...]
    debian_suite: str = ""
    rocky_major: int = 0


def ubuntu_release(name: str) -> str:
    """Return the Ubuntu version for a known suite. Unknown names are refused."""
    try:
        return UBUNTU_SUITES[name]
    except KeyError as exc:
        known = ", ".join(UBUNTU_SUITES)
        raise Refused(f"unknown Ubuntu suite {name!r}. Known suites: {known}") from exc


def _assert_version(version: str) -> None:
    if not version:
        raise Refused("refusing version")
    for char in version:
        if ord(char) < 32 or ord(char) > 126 or char.isspace():
            raise Refused("refusing version")
    if "/" in version or ".." in version or "(" in version or ")" in version:
        raise Refused("refusing version")


def retarget_version(
    version: str,
    *,
    distro: str,
    ubuntu_suite: str,
    debian_suite: str = "",
    rocky_major: int = 0,
) -> str:
    """Append a sort suffix so the export does not claim the original revision.

    An Ubuntu delta becomes ``<version>~<suite>1`` on Debian and
    ``<version>~el<major>1`` on Rocky. A Debian sync (no ``ubuntu`` token)
    becomes ``<version>~ubuntu.<ubuntu suite>.1`` on Debian, which sorts before
    the archive revision, and the same ``~el<major>1`` suffix on Rocky.
    The epoch stays in the version. It is not part of the filename.
    """
    ubuntu_release(ubuntu_suite)
    _assert_version(version)
    if distro == "debian":
        get_profile(debian_suite)
        if "ubuntu" in version:
            rewritten = f"{version}~{debian_suite}1"
        else:
            rewritten = f"{version}~ubuntu.{ubuntu_suite}.1"
    elif distro == "rocky":
        if rocky_major not in (8, 9, 10):
            raise Refused("rocky major must be 8, 9, or 10")
        rewritten = f"{version}~el{rocky_major}1"
    else:
        raise Refused("distro must be debian or rocky")
    _assert_version(rewritten)
    return rewritten


def _paragraphs(text: str) -> list[dict[str, str]]:
    blocks = re.split(r"\n[ \t]*\n", text.strip())
    parsed: list[dict[str, str]] = []
    for block in blocks:
        fields: dict[str, str] = {}
        current = ""
        for line in block.splitlines():
            if line.startswith((" ", "\t")) and current:
                fields[current] = fields[current] + "\n" + line.strip()
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            current = key.strip().lower()
            fields[current] = value.strip()
        if fields:
            parsed.append(fields)
    return parsed


def _safe_filename(name: str) -> None:
    if (
        not name
        or "/" in name
        or "\\" in name
        or name.startswith("/")
        or ".." in name
        or name in {".", ".."}
    ):
        raise Refused("refusing source file")


def _safe_directory(directory: str) -> str:
    if not directory or directory.startswith("/") or "\\" in directory or ".." in directory:
        raise Refused("refusing source directory")
    parts = [part for part in directory.split("/") if part != ""]
    if not parts or any(part in {".", ".."} for part in parts):
        raise Refused("refusing source directory")
    return "/".join(parts)


def _parse_sha_rows(block: str) -> list[tuple[str, int, str]]:
    rows: list[tuple[str, int, str]] = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 3:
            raise Refused("missing checksum")
        digest, size_text, name = parts
        _safe_filename(name)
        if not size_text.isdigit():
            raise Refused("refusing source file")
        size = int(size_text)
        if size < 1:
            raise Refused("refusing empty size")
        if _HEX64.fullmatch(digest) is None:
            raise Refused("missing checksum")
        rows.append((digest, size, name))
    return rows


def _md5_by_name(block: str) -> dict[str, str]:
    """Record Files: MD5 for the .dsc writer. The value is not trusted."""
    found: dict[str, str] = {}
    for raw in block.splitlines():
        parts = raw.split()
        if len(parts) != 3:
            continue
        digest, _size, name = parts
        found[name] = digest
    return found


def _binaries(field: str) -> tuple[str, ...]:
    names: list[str] = []
    for part in field.split(","):
        token = part.strip()
        if not token:
            continue
        names.append(deb_package_name(token))
    return tuple(names)


def parse_sources(sources: str | bytes) -> tuple[bytes, list[SourceStanza]]:
    """Parse a Sources excerpt. Every stanza needs Checksums-Sha256.

    A duplicate source name refuses the index. Base and trademark are not
    decided here, so an excerpt that also lists libc6 can still export hello.
    """
    if isinstance(sources, bytes):
        payload = sources
        if len(payload) > DEFAULT_LIMIT:
            raise Refused(f"input exceeds {DEFAULT_LIMIT} bytes")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Refused("sources are not utf-8") from exc
    else:
        payload = sources.encode("utf-8")
        if len(payload) > DEFAULT_LIMIT:
            raise Refused(f"input exceeds {DEFAULT_LIMIT} bytes")
        text = sources
    stanzas: list[SourceStanza] = []
    seen: set[str] = set()
    for fields in _paragraphs(text):
        if "package" not in fields:
            raise Refused("sources stanza needs a package")
        package = deb_package_name(fields["package"])
        if package in seen:
            raise Refused("duplicate source package")
        seen.add(package)
        version = fields.get("version", "")
        if not version:
            raise Refused("refusing version")
        directory = _safe_directory(fields.get("directory", ""))
        sha_rows = _parse_sha_rows(fields.get("checksums-sha256", ""))
        if not sha_rows:
            raise Refused("missing checksum")
        if len(sha_rows) > _MAX_FILES:
            raise Refused("refusing source file count")
        names = [row[2] for row in sha_rows]
        if len(names) != len(set(names)):
            raise Refused("duplicate source file")
        md5 = _md5_by_name(fields.get("files", ""))
        files = tuple(
            SourceFile(name=name, size=size, sha256=digest, md5=md5.get(name, ""))
            for digest, size, name in sha_rows
        )
        stanzas.append(
            SourceStanza(
                package=package,
                version=version,
                directory=directory,
                binaries=_binaries(fields.get("binary", "")),
                files=files,
            )
        )
    return payload, stanzas


def _lookup(stanzas: list[SourceStanza], package: str) -> SourceStanza:
    safe = deb_package_name(package)
    found = [item for item in stanzas if item.package == safe]
    if not found:
        raise Refused("unknown source package")
    return found[0]


def _refuse_special(stanza: SourceStanza) -> None:
    names = (stanza.package, *stanza.binaries)
    for name in names:
        if is_deb_base(name):
            raise Refused("base package")
        if is_deb_trademark(name):
            raise Refused("trademark")


def _is_debian_tar(name: str) -> bool:
    return ".debian.tar." in name


def _is_orig(name: str) -> bool:
    return ".orig.tar." in name


def _classify(stanza: SourceStanza) -> tuple[list[SourceFile], list[SourceFile]]:
    origs = [item for item in stanza.files if _is_orig(item.name)]
    debians = [item for item in stanza.files if _is_debian_tar(item.name)]
    for item in stanza.files:
        if _is_orig(item.name) or _is_debian_tar(item.name) or item.name.endswith(".dsc"):
            continue
        raise Refused("unexpected source file")
    if len(debians) != 1:
        raise Refused("source needs exactly one debian tarball")
    if len(origs) > 1:
        raise Refused("multiple source tarballs")
    return origs, debians


def _download_checked(url: str, item: SourceFile, opener: Opener | None) -> bytes:
    if item.size > DEFAULT_LIMIT:
        raise Refused("checksum mismatch")
    try:
        body, _final = download_https(url, limit=item.size, opener=opener)
    except Refused as exc:
        if str(exc).startswith("input exceeds"):
            raise Refused("checksum mismatch") from exc
        raise
    if len(body) != item.size or not checksums_match(sha256_hex(body), item.sha256):
        raise Refused("checksum mismatch")
    return body


def _member_parts(name: str) -> tuple[str, ...]:
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        raise Refused("refusing debian path")
    text = name.rstrip("/")
    parts = tuple(text.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise Refused("refusing debian path")
    if parts[0] != "debian":
        raise Refused("refusing debian path")
    return parts


def _tar_mode(name: str) -> str:
    if name.endswith(".tar.xz"):
        return "r:xz"
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return "r:gz"
    if name.endswith(".tar.bz2"):
        return "r:bz2"
    raise Refused("debian tarball suffix is not tar.xz, tar.gz, or tar.bz2")


def _under_root(root: Path, relative: tuple[str, ...]) -> Path:
    current = root
    for part in relative:
        current = current / part
        if current.is_symlink():
            raise Refused("refusing symlink in debian tree")
    resolved_root = root.resolve()
    resolved = current.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise Refused("refusing debian path")
    return current


def _unpack_debian(name: str, blob: bytes, tree: Path) -> None:
    """Unpack debian/ only. Members are checked before any file is written."""
    if len(blob) > DEFAULT_LIMIT:
        raise Refused(f"input exceeds {DEFAULT_LIMIT} bytes")
    try:
        archive = tarfile.open(fileobj=io.BytesIO(blob), mode=_tar_mode(name))
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise Refused("debian tarball is not a tar archive") from exc
    with archive:
        members = archive.getmembers()
        if len(members) > _MAX_MEMBERS:
            raise Refused("debian tarball has too many members")
        planned: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
        total = 0
        seen: set[tuple[str, ...]] = set()
        for member in members:
            parts = _member_parts(member.name)
            if parts in seen:
                raise Refused("refusing debian path")
            seen.add(parts)
            if member.issym() or member.islnk() or member.type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                raise Refused("refusing symlink in debian tarball")
            if member.isdir():
                planned.append((member, parts))
                continue
            if not member.isfile():
                raise Refused("refusing special file in debian tarball")
            if member.size < 0:
                raise Refused("refusing debian path")
            total += member.size
            if total > DEFAULT_LIMIT:
                raise Refused(f"input exceeds {DEFAULT_LIMIT} bytes")
            planned.append((member, parts))
        files: list[tuple[tuple[str, ...], bytes]] = []
        for member, parts in planned:
            if member.isdir():
                continue
            handle = archive.extractfile(member)
            if handle is None:
                raise Refused("debian tarball member is missing")
            try:
                data = read_limited(handle, member.size if member.size else 1)
            except Refused as exc:
                if str(exc).startswith("input exceeds"):
                    raise Refused(f"input exceeds {DEFAULT_LIMIT} bytes") from exc
                raise
            if len(data) != member.size:
                raise Refused("checksum mismatch")
            files.append((parts, data))
    for member, parts in planned:
        if member.isdir():
            _under_root(tree, parts).mkdir(parents=True, exist_ok=True)
    for parts, data in files:
        target = _under_root(tree, parts)
        if target.exists() and not target.is_file():
            raise Refused("refusing debian path")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.parent.is_symlink():
            raise Refused("refusing symlink in debian tree")
        mode = 0o755 if parts == ("debian", "rules") else 0o644
        partial = target.with_name(target.name + ".partial")
        partial.write_bytes(data)
        partial.chmod(mode)
        os.replace(partial, target)


def _rewrite_changelog(tree: Path, package: str, version: str) -> None:
    path = tree / "debian" / "changelog"
    if path.is_symlink() or not path.is_file():
        raise Refused("debian/changelog is missing")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines:
        raise Refused("debian/changelog first line is not name (version)")
    match = _CHANGELOG_RE.match(lines[0])
    if match is None:
        raise Refused("debian/changelog first line is not name (version)")
    name = match.group(1)
    if deb_package_name(name) != deb_package_name(package):
        raise Refused("changelog package does not match source")
    _assert_version(version)
    lines[0] = f"{name} ({version}){match.group(3)}"
    rewritten = "\n".join(lines)
    if text.endswith("\n"):
        rewritten += "\n"
    partial = path.with_name(path.name + ".partial")
    partial.write_text(rewritten, encoding="utf-8")
    partial.chmod(0o644)
    os.replace(partial, path)


def _write_bytes(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise Refused("refusing symlink destination")
    partial = path.with_name(path.name + ".partial")
    partial.write_bytes(data)
    partial.chmod(0o644)
    os.replace(partial, path)


def _stage(
    stanza: SourceStanza,
    version: str,
    *,
    distro: str,
    root: str,
    license_text: str,
    opener: Opener | None,
    origs: list[SourceFile],
    debian_tar: SourceFile,
) -> list[tuple[str, bytes]]:
    with tempfile.TemporaryDirectory(prefix="ubuntu-export-") as tmp:
        work = Path(tmp)
        downloaded: dict[str, bytes] = {}
        for item in stanza.files:
            url = f"{root}/{stanza.directory}/{item.name}"
            assert_https(url)
            downloaded[item.name] = _download_checked(url, item, opener)
        tree = work / "tree"
        tree.mkdir()
        _unpack_debian(debian_tar.name, downloaded[debian_tar.name], tree)
        _rewrite_changelog(tree, stanza.package, version)
        if distro == "rocky":
            spec = spec_from_tree(tree, license_text)
            return [(f"{stanza.package}.spec", spec.encode("utf-8"))]
        orig = origs[0]
        orig_path = work / orig.name
        _write_bytes(orig_path, downloaded[orig.name])
        stage = work / "stage"
        pack_source(tree, orig_path, stage)
        payloads: list[tuple[str, bytes]] = []
        for path in sorted(stage.iterdir()):
            if path.is_symlink() or not path.is_file():
                raise Refused("refusing symlink in packed source")
            payloads.append((path.name, path.read_bytes()))
        if not payloads:
            raise Refused("export produced nothing")
        return payloads


def _publish(dest: Path, payloads: list[tuple[str, bytes]]) -> None:
    if dest.is_symlink():
        raise Refused("refusing symlink destination")
    if dest.exists() and not dest.is_dir():
        raise Refused("destination is not a directory")
    dest.mkdir(parents=True, exist_ok=True)
    for name, data in payloads:
        _safe_filename(name)
        target = dest / name
        if target.is_symlink():
            raise Refused("refusing symlink destination")
        partial = target.with_name(target.name + ".partial")
        partial.write_bytes(data)
        partial.chmod(0o644)
        os.replace(partial, target)


def export_ubuntu(
    sources: str | bytes,
    package: str,
    *,
    ubuntu_suite: str,
    distro: str,
    debian_suite: str = "",
    rocky_major: int = 0,
    execute: bool = False,
    license_text: str = "",
    signature: bytes = b"",
    keyring: Path | None = None,
    dest: Path | None = None,
    archive: str = UBUNTU_ARCHIVE,
    opener: Opener | None = None,
    runner: Runner | None = None,
) -> ExportPlan:
    """Plan or export one Ubuntu source package.

    Without ``execute``, return the retargeted version and the index file
    names. Nothing is downloaded and ``dest`` is not created. With ``execute``,
    the archive URL is checked before the license, the license before any
    download, and ``gpgv`` before any pool file. ``dest`` is created only after
    the unpacked tree is retargeted and packed. debian/rules is not executed.
    """
    ubuntu_release(ubuntu_suite)
    if distro not in {"debian", "rocky"}:
        raise Refused("distro must be debian or rocky")
    if distro == "debian":
        get_profile(debian_suite)
    elif rocky_major not in (8, 9, 10):
        raise Refused("rocky major must be 8, 9, or 10")
    payload, stanzas = parse_sources(sources)
    stanza = _lookup(stanzas, package)
    _refuse_special(stanza)
    version = retarget_version(
        stanza.version,
        distro=distro,
        ubuntu_suite=ubuntu_suite,
        debian_suite=debian_suite,
        rocky_major=rocky_major,
    )
    plan = ExportPlan(
        package=stanza.package,
        version=version,
        distro=distro,
        ubuntu_suite=ubuntu_suite,
        files=stanza.files,
        debian_suite=debian_suite if distro == "debian" else "",
        rocky_major=rocky_major if distro == "rocky" else 0,
    )
    if not execute:
        return plan
    root = archive.strip().rstrip("/")
    assert_https(root + "/")
    if not license_text.strip():
        raise Refused("missing license")
    allowed, _klass, why = rebuild_permission(stanza.package, license_text, {})
    if not allowed:
        raise Refused(why)
    if not signature:
        raise Refused("missing signature")
    if keyring is None or Path(keyring).is_symlink() or not Path(keyring).is_file():
        raise Refused("missing keyring")
    verify_detached(payload, signature, Path(keyring), runner)
    origs, debians = _classify(stanza)
    if distro == "debian" and not origs:
        raise Refused("source tarball missing")
    if dest is None:
        raise Refused("export needs a destination")
    destination = Path(dest)
    if destination.is_symlink():
        raise Refused("refusing symlink destination")
    if destination.exists() and not destination.is_dir():
        raise Refused("destination is not a directory")
    payloads = _stage(
        stanza,
        version,
        distro=distro,
        root=root,
        license_text=license_text,
        opener=opener,
        origs=origs,
        debian_tar=debians[0],
    )
    _publish(destination, payloads)
    return plan


# Runner is re-exported for callers that annotate a gpgv fake.
GpgRunner = Callable[[list[str]], int]
