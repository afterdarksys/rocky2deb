"""Pack an emitted debian/ tree into a 3.0 (quilt) .dsc.

dpkg-source is not used. It would run debian/rules clean on the controller.
The orig tarball and the debian.tar.xz are written as archives, and the .dsc
records their sha256. Nothing in the tree is executed.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

from rocky2deb.errors import Refused

_ORIG_SUFFIXES = (".tar.gz", ".tar.xz", ".tar.bz2", ".tgz")
_PATCH_CAP = 8 * 1024 * 1024


def find_orig(source_dir: Path) -> Path:
    found = []
    for path in sorted(Path(source_dir).iterdir()):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name.endswith(_ORIG_SUFFIXES):
            found.append(path)
    if not found:
        raise Refused("source tarball missing")
    if len(found) > 1:
        raise Refused("multiple source tarballs")
    return found[0]


def materialize_patches(tree: Path, source_dir: Path) -> None:
    series = tree / "debian" / "patches" / "series"
    if not series.is_file():
        return
    for line in series.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        if "/" in name or "\\" in name or name == ".." or name.startswith("."):
            raise Refused("refusing patch name")
        src = Path(source_dir) / name
        if not src.is_file() or src.is_symlink():
            raise Refused("missing patch")
        data = src.read_bytes()
        if len(data) > _PATCH_CAP:
            raise Refused("patch exceeds size cap")
        dest = tree / "debian" / "patches" / name
        dest.write_bytes(data)
        dest.chmod(0o644)


def _upstream(version: str) -> str:
    bare = version.split(":", 1)[-1]
    if "-" not in bare:
        raise Refused("debian version has no revision")
    return bare.rsplit("-", 1)[0]


def _changelog_version(tree: Path) -> tuple[str, str]:
    changelog = tree / "debian" / "changelog"
    if not changelog.is_file():
        raise Refused("debian/changelog is missing")
    line = changelog.read_text(encoding="utf-8").splitlines()[0]
    try:
        name, rest = line.split(" ", 1)
        version = rest.split("(", 1)[1].split(")", 1)[0]
    except IndexError as exc:
        raise Refused("debian/changelog first line is not name (version)") from exc
    if not name or not version:
        raise Refused("debian/changelog first line is not name (version)")
    return name, version


def tar_debian_bytes(tree: Path) -> bytes:
    """Pack debian/ with the same rules as the .dsc writer. Does not run rules."""
    return _tar_debian(tree)


def _tar_debian(tree: Path) -> bytes:
    debian = tree / "debian"
    if not debian.is_dir():
        raise Refused("debian directory is missing")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as archive:
        for path in sorted(debian.rglob("*")):
            if path.is_symlink():
                raise Refused("refusing symlink in debian tree")
            relative = path.relative_to(tree)
            if ".." in relative.parts:
                raise Refused("refusing debian path")
            info = archive.gettarinfo(str(path), arcname=relative.as_posix())
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            if path.is_dir():
                info.mode = 0o755
                archive.addfile(info)
                continue
            if not path.is_file():
                raise Refused("refusing special file in debian tree")
            info.mode = 0o755 if path.name == "rules" else 0o644
            info.size = path.stat().st_size
            with path.open("rb") as handle:
                archive.addfile(info, handle)
    return buffer.getvalue()


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(64 * 1024)
            if not block:
                break
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size


def pack_source(tree: Path, orig: Path, dest: Path) -> Path:
    """Write a .dsc and its tarballs under dest. Return the .dsc path."""
    source = Path(orig)
    if not source.is_file() or source.is_symlink():
        raise Refused("source tarball missing")
    name, version = _changelog_version(tree)
    upstream = _upstream(version)
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    suffix = ".tar.gz" if source.name.endswith(".tgz") else "".join(source.suffixes[-2:])
    if suffix not in {".tar.gz", ".tar.xz", ".tar.bz2"}:
        raise Refused("source tarball suffix is not tar.gz, tar.xz, or tar.bz2")
    orig_name = f"{name}_{upstream}.orig{suffix}"
    debian_name = f"{name}_{version.split(':', 1)[-1]}.debian.tar.xz"
    orig_dest = root / orig_name
    debian_dest = root / debian_name
    orig_dest.write_bytes(source.read_bytes())
    debian_dest.write_bytes(_tar_debian(tree))
    orig_hash, orig_size = _sha256(orig_dest)
    debian_hash, debian_size = _sha256(debian_dest)
    orig_md5 = hashlib.md5(orig_dest.read_bytes()).hexdigest()
    debian_md5 = hashlib.md5(debian_dest.read_bytes()).hexdigest()
    dsc = root / f"{name}_{version.split(':', 1)[-1]}.dsc"
    control = (tree / "debian" / "control").read_text(encoding="utf-8")
    binary = []
    for line in control.splitlines():
        if line.startswith("Package:"):
            binary.append(line.split(":", 1)[1].strip())
    dsc.write_text(
        "\n".join(
            [
                "Format: 3.0 (quilt)",
                f"Source: {name}",
                "Binary: " + ", ".join(binary),
                "Architecture: any",
                f"Version: {version}",
                "Maintainer: rocky2deb <rocky2deb@localhost>",
                "Checksums-Sha256:",
                f" {orig_hash} {orig_size} {orig_name}",
                f" {debian_hash} {debian_size} {debian_name}",
                "Files:",
                f" {orig_md5} {orig_size} {orig_name}",
                f" {debian_md5} {debian_size} {debian_name}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return dsc
