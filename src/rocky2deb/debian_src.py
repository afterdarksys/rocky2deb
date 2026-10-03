"""Fetch one Debian source package from a local signed Sources index.

Threats: a hostile Sources file can name a non-https URL, a URL with
userinfo, or a pool file whose SHA256 does not match the index. The archive
URL is checked before a download. gpgv checks the Sources signature. Pool
files are accepted only when their SHA256 matches the signed index. MD5 in
the Files field is not compared. debian/rules is not executed. The changelog
is not rewritten and the Ubuntu version suffix is not applied. The
destination is created only after the tree has been staged.

This path always requires an orig tarball. It does not call retarget_version.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rocky2deb.errors import Refused
from rocky2deb.fetch import assert_https, verify_detached
from rocky2deb.license import rebuild_permission
from rocky2deb.profiles import get_profile
from rocky2deb.ubuntu_src import (
    SourceFile,
    _assert_version,
    _classify,
    _download_checked,
    _lookup,
    _refuse_special,
    _safe_filename,
    _unpack_debian,
    parse_sources,
)


@dataclass(frozen=True)
class DebianFetchPlan:
    package: str
    version: str
    suite: str
    files: tuple[SourceFile, ...]


def _stage_debian(
    stanza,
    root: str,
    opener,
    origs: list[SourceFile],
    debian_tar: SourceFile,
) -> list[tuple[str, bytes, int]]:
    with tempfile.TemporaryDirectory(prefix="debian-fetch-") as tmp:
        work = Path(tmp)
        tree = work / "tree"
        tree.mkdir()
        downloaded: dict[str, bytes] = {}
        for item in stanza.files:
            url = f"{root}/{stanza.directory}/{item.name}"
            assert_https(url)
            downloaded[item.name] = _download_checked(url, item, opener)
        _unpack_debian(debian_tar.name, downloaded[debian_tar.name], tree)
        payloads: list[tuple[str, bytes, int]] = []
        for path in sorted(tree.rglob("*")):
            if path.is_symlink():
                raise Refused("refusing symlink in debian tree")
            if path.is_dir():
                continue
            if not path.is_file():
                raise Refused("refusing special file in debian tarball")
            relative = path.relative_to(tree).as_posix()
            mode = 0o755 if relative == "debian/rules" else 0o644
            payloads.append((relative, path.read_bytes(), mode))
        for item in origs:
            _safe_filename(item.name)
            payloads.append((item.name, downloaded[item.name], 0o644))
        return payloads


def _publish_tree(dest: Path, payloads: list[tuple[str, bytes, int]]) -> None:
    if dest.is_symlink():
        raise Refused("refusing symlink destination")
    if dest.exists() and not dest.is_dir():
        raise Refused("destination is not a directory")
    planned: list[tuple[tuple[str, ...], bytes, int]] = []
    for relative, data, mode in payloads:
        if relative.startswith("/") or "\\" in relative or "\x00" in relative:
            raise Refused("refusing debian path")
        parts = tuple(relative.split("/"))
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise Refused("refusing debian path")
        planned.append((parts, data, mode))
    dest.mkdir(parents=True, exist_ok=True)
    for parts, data, mode in planned:
        current = dest
        for part in parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise Refused("refusing symlink in debian tree")
            current.mkdir(exist_ok=True)
        target = dest.joinpath(*parts)
        if target.is_symlink():
            raise Refused("refusing symlink destination")
        partial = target.with_name(target.name + ".partial")
        partial.write_bytes(data)
        partial.chmod(mode)
        os.replace(partial, target)


def fetch_debian_source(
    sources: str | bytes,
    package: str,
    *,
    suite: str,
    execute: bool = False,
    license_text: str = "",
    signature: bytes = b"",
    keyring: Path | None = None,
    dest: Path | None = None,
    archive: str = "",
    opener=None,
    runner=None,
) -> DebianFetchPlan:
    """Plan or fetch one Debian source package.

    Without ``execute``, return the original version and the index file
    names. Nothing is downloaded and ``dest`` is not created. With
    ``execute``, the archive URL is checked before the license, and gpgv
    runs before any pool file. The changelog bytes are copied unchanged.
    """
    profile = get_profile(suite)
    _payload, stanzas = parse_sources(sources)
    stanza = _lookup(stanzas, package)
    _refuse_special(stanza)
    _assert_version(stanza.version)
    plan = DebianFetchPlan(
        package=stanza.package,
        version=stanza.version,
        suite=suite,
        files=stanza.files,
    )
    if not execute:
        return plan
    root = (archive or profile.archive).strip().rstrip("/")
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
    verify_detached(_payload, signature, Path(keyring), runner)
    origs, debians = _classify(stanza)
    if not origs:
        raise Refused("source tarball missing")
    if dest is None:
        raise Refused("fetch needs a destination")
    destination = Path(dest)
    if destination.is_symlink():
        raise Refused("refusing symlink destination")
    if destination.exists() and not destination.is_dir():
        raise Refused("destination is not a directory")
    payloads = _stage_debian(stanza, root, opener, origs, debians[0])
    _publish_tree(destination, payloads)
    return plan
