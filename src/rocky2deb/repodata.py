"""Write an unsigned dnf repository from RPM files already on disk.

Threats: a hostile RPM can be a symlink, a name that escapes the package
directory, or a file larger than the cap. Only basenames are copied. The
bytes are stored and no package script is run. repomd is not signed. dnf
and createrepo are not invoked. A bad RPM lead refuses before the
destination is created. SHA256 values written into primary.xml are
identifiers of the bytes just copied. This module does not compare them.
"""

from __future__ import annotations

import gzip
import os
import re
from pathlib import Path

from rocky2deb.fetch import DEFAULT_LIMIT, sha256_hex
from rocky2deb.errors import Refused
from rocky2deb.srpm_pack import read_rpm_identity

_RPM_NAME = re.compile(r"^[A-Za-z0-9._+-]+\.rpm$")


def _xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _plain(text: str, *, allow_break: bool) -> None:
    for char in text:
        if allow_break and char in "\n\t":
            continue
        code = ord(char)
        if code < 32 or code == 127:
            raise Refused("refusing spec field")


def _write(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise Refused("refusing symlink destination")
    partial = path.with_name(path.name + ".partial")
    partial.write_bytes(data)
    partial.chmod(0o644)
    os.replace(partial, path)


def write_rpm_repo(source: Path, dest: Path, *, limit: int = DEFAULT_LIMIT) -> Path:
    """Copy RPMs into Packages/ and write repodata/primary.xml.gz.

    Files that do not end in ``.rpm`` are skipped. Validation finishes
    before ``dest`` is created. An empty directory still writes a primary
    document with ``packages="0"``.
    """
    if limit < 1:
        raise Refused("size limit must be positive")
    root = Path(source)
    target = Path(dest)
    if root.is_symlink() or not root.is_dir():
        raise Refused("rpm directory is missing")
    if target.is_symlink():
        raise Refused("refusing symlink destination")
    if target.exists() and not target.is_dir():
        raise Refused("destination is not a directory")
    rows: list[tuple[str, bytes, str, str, str, str, str, str, str, str, str]] = []
    seen: set[str] = set()
    for path in root.iterdir():
        name = path.name
        if not name.endswith(".rpm"):
            continue
        if path.is_symlink():
            raise Refused("refusing symlink source")
        if not path.is_file():
            raise Refused("source is missing")
        if _RPM_NAME.fullmatch(name) is None or name in seen:
            raise Refused("duplicate source file" if name in seen else "refusing source file")
        size = path.stat().st_size
        if size > limit:
            raise Refused(f"input exceeds {limit} bytes")
        data = path.read_bytes()
        if len(data) > limit:
            raise Refused(f"input exceeds {limit} bytes")
        identity = read_rpm_identity(data, limit=limit)
        _plain(identity.summary, allow_break=False)
        _plain(identity.description, allow_break=True)
        _plain(identity.name, allow_break=False)
        _plain(identity.license, allow_break=False)
        seen.add(name)
        epoch = identity.epoch or "0"
        rows.append(
            (
                identity.name,
                data,
                name,
                identity.arch,
                epoch,
                identity.version,
                identity.release,
                identity.license,
                identity.sourcerpm,
                identity.summary,
                identity.description,
            )
        )
    rows.sort(key=lambda item: (item[0], item[2]))
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            '<metadata xmlns="http://linux.duke.edu/metadata/common" '
            'xmlns:rpm="http://linux.duke.edu/metadata/rpm" '
            f'packages="{len(rows)}">'
        ),
    ]
    copied: list[tuple[str, bytes]] = []
    for (
        pkg_name,
        data,
        filename,
        arch,
        epoch,
        version,
        release,
        license_text,
        sourcerpm,
        summary,
        description,
    ) in rows:
        digest = sha256_hex(data)
        href = f"Packages/{filename}"
        parts.append('  <package type="rpm">')
        parts.append(f"    <name>{_xml(pkg_name)}</name>")
        parts.append(f"    <arch>{_xml(arch)}</arch>")
        parts.append(
            f'    <version epoch="{_xml(epoch)}" ver="{_xml(version)}" rel="{_xml(release)}"/>'
        )
        parts.append(f'    <checksum type="sha256" pkgid="YES">{digest}</checksum>')
        parts.append(f"    <summary>{_xml(summary)}</summary>")
        parts.append(f"    <description>{_xml(description)}</description>")
        parts.append(f'    <location href="{_xml(href)}"/>')
        parts.append("    <format>")
        parts.append(f"      <rpm:license>{_xml(license_text)}</rpm:license>")
        parts.append(f"      <rpm:sourcerpm>{_xml(sourcerpm)}</rpm:sourcerpm>")
        parts.append("    </format>")
        parts.append("  </package>")
        copied.append((filename, data))
    parts.append("</metadata>")
    parts.append("")
    xml = "\n".join(parts).encode("utf-8")
    compressed = gzip.compress(xml, compresslevel=9, mtime=0)
    repomd = "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<repomd xmlns="http://linux.duke.edu/metadata/repo">',
            '  <data type="primary">',
            f'    <checksum type="sha256">{sha256_hex(compressed)}</checksum>',
            f'    <open-checksum type="sha256">{sha256_hex(xml)}</open-checksum>',
            '    <location href="repodata/primary.xml.gz"/>',
            f"    <size>{len(compressed)}</size>",
            f"    <open-size>{len(xml)}</open-size>",
            "  </data>",
            "</repomd>",
            "",
        ]
    ).encode("utf-8")
    target.mkdir(parents=True, exist_ok=True)
    packages = target / "Packages"
    meta = target / "repodata"
    packages.mkdir(exist_ok=True)
    meta.mkdir(exist_ok=True)
    for filename, data in copied:
        _write(packages / filename, data)
    _write(meta / "primary.xml.gz", compressed)
    _write(meta / "repomd.xml", repomd)
    return target
