"""Write an apt repository slice for rebuilt debs.

The component is rocky2deb, separate from Debian main. Packages and Release
carry sha256. This module does not sign Release and does not invent a key.
A .deb is read as an ar archive; only the control member is loaded, and it
is capped.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

from rocky2deb.errors import Refused
from rocky2deb.names import deb_package_name
from rocky2deb.profiles import get_profile

_CONTROL_CAP = 1024 * 1024
_DEB_CAP = 512 * 1024 * 1024
_AR_MAGIC = b"!<arch>\n"


def _ar_members(blob: bytes):
    if not blob.startswith(_AR_MAGIC):
        raise Refused("deb is not an ar archive")
    offset = len(_AR_MAGIC)
    while offset + 60 <= len(blob):
        header = blob[offset : offset + 60]
        if header[58:60] != b"`\n":
            raise Refused("deb ar header is corrupt")
        name = header[:16].decode("ascii", "replace").strip()
        try:
            size = int(header[48:58].decode("ascii").strip() or "0")
        except ValueError as exc:
            raise Refused("deb ar header is corrupt") from exc
        if size < 0 or size > len(blob):
            raise Refused("deb ar member exceeds size cap")
        start = offset + 60
        end = start + size
        if end > len(blob):
            raise Refused("deb ar member is truncated")
        yield name, blob[start:end]
        offset = end + (size % 2)


def _control_text(blob: bytes) -> str:
    for name, payload in _ar_members(blob):
        if not name.startswith("control.tar"):
            continue
        if len(payload) > _CONTROL_CAP:
            raise Refused("deb control exceeds size cap")
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
                member = archive.extractfile("control") or archive.extractfile("./control")
                if member is None:
                    raise Refused("deb control has no control file")
                data = member.read(_CONTROL_CAP + 1)
        except Refused:
            raise
        except Exception as exc:
            raise Refused("deb control did not parse") from exc
        if len(data) > _CONTROL_CAP:
            raise Refused("deb control exceeds size cap")
        return data.decode("utf-8")
    raise Refused("deb has no control member")


def _fields(control: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw in control.splitlines():
        if not raw or raw[0].isspace() or ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        found[key.strip()] = value.strip()
    return found


def write_repo(debs: Path, suite: str, dest: Path) -> Path:
    """Copy debs into pool and write dists/<suite>/Release. Return the Release path."""
    get_profile(suite)
    source = Path(debs)
    root = Path(dest)
    pool = root / "pool" / suite
    binary = root / "dists" / suite / "rocky2deb" / "binary-amd64"
    pool.mkdir(parents=True, exist_ok=True)
    binary.mkdir(parents=True, exist_ok=True)
    stanzas: list[str] = []
    for path in sorted(source.glob("*.deb")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_size > _DEB_CAP:
            raise Refused("deb exceeds size cap")
        fields = _fields(_control_text(path.read_bytes()))
        package = deb_package_name(fields.get("Package", ""))
        version = fields.get("Version", "")
        architecture = fields.get("Architecture", "")
        if not version or architecture not in {"amd64", "all", "arm64", "i386"}:
            raise Refused("deb control is missing version or architecture")
        blob = path.read_bytes()
        filename = f"pool/{suite}/{package}_{version}_{architecture}.deb"
        target = root / filename
        target.write_bytes(blob)
        digest = hashlib.sha256(blob).hexdigest()
        lines = [
            f"Package: {package}",
            f"Version: {version}",
            f"Architecture: {architecture}",
            f"Filename: {filename}",
            f"Size: {len(blob)}",
            f"SHA256: {digest}",
        ]
        if fields.get("Depends"):
            lines.append(f"Depends: {fields['Depends']}")
        stanzas.append("\n".join(lines))
    packages = ("\n\n".join(stanzas) + ("\n" if stanzas else ""))
    (binary / "Packages").write_text(packages, encoding="utf-8")
    digest = hashlib.sha256(packages.encode()).hexdigest()
    relative = "rocky2deb/binary-amd64/Packages"
    release = root / "dists" / suite / "Release"
    release.write_text(
        "\n".join(
            [
                f"Suite: {suite}",
                "Component: rocky2deb",
                "Architectures: amd64",
                "SHA256:",
                f" {digest} {len(packages.encode())} {relative}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return release
