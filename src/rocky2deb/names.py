"""RPM name to Debian name, plus the base-package denylist.

Base packages are satisfied by the Debian installer. A pin cannot schedule
them for rebuild: replacing libc, systemd, the kernel, or the package manager
from a Rocky source breaks the host.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from rocky2deb.errors import Refused
from rocky2deb.resources import package_data

# RPM name → Debian binary that already provides it.
BASE_DEB = {
    "glibc": "libc6",
    "systemd": "systemd",
    "dbus": "dbus",
    "rpm": "apt",
    "rpm-libs": "apt",
    "rpm-build": "apt",
    "dnf": "apt",
    "yum": "apt",
    "yum-utils": "apt",
    "rocky-release": "base-files",
    "redhat-release": "base-files",
    "centos-release": "base-files",
    "shim-x64": "shim-signed",
    "linux-firmware": "firmware-linux-nonfree",
    "ca-certificates": "ca-certificates",
    "coreutils": "coreutils",
    "bash": "bash",
    "shadow-utils": "passwd",
    "util-linux": "util-linux",
    "ncurses-base": "ncurses-base",
    "tzdata": "tzdata",
}

_BASE_EXACT = frozenset(BASE_DEB)
_BASE_PREFIXES = ("kernel", "linux-image", "grub2", "grub")

TRADEMARK = frozenset(
    {
        "redhat-logos",
        "rocky-logos",
        "rocky-backgrounds",
        "centos-logos",
    }
)

_BLOCKED_ROOTS = frozenset({"glibc", "systemd", "rpm", "dnf", "yum"})

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9+.-]+")


@dataclass(frozen=True)
class NameEntry:
    rpm: str
    deb: str
    unit: str = ""
    deb_unit: str = ""


def is_base(name: str) -> bool:
    if name in _BASE_EXACT:
        return True
    return name.startswith(_BASE_PREFIXES)


def base_deb(name: str) -> str:
    if name in BASE_DEB:
        return BASE_DEB[name]
    if name.startswith(("kernel", "linux-image")):
        return "linux-image-amd64"
    if name.startswith(("grub", "grub2")):
        return "grub-efi-amd64"
    return ""


def is_trademark(name: str) -> bool:
    return name in TRADEMARK


def is_blocked_root(name: str) -> bool:
    """Packages rockify will not try to transplant onto Debian."""
    if name in _BLOCKED_ROOTS:
        return True
    if name == "kernel" or name.startswith("kernel-") or name.startswith("kmod-"):
        return True
    if name.endswith("-kmod"):
        return True
    return False


def deb_package_name(name: str) -> str:
    """Return a Debian package name, or refuse path-like input."""
    if "/" in name or "\\" in name or ".." in name:
        raise Refused(f"refusing package name {name!r}")
    text = name.lower().replace("_", "-")
    text = re.sub(r"[^a-z0-9+.-]", "", text)
    text = text.strip(".-")
    if _NAME_RE.fullmatch(text) is None:
        raise Refused(f"refusing package name {name!r}")
    return text


def load_name_map(path: Path | None = None) -> dict[str, NameEntry]:
    source = path or package_data("names.toml")
    data = tomllib.loads(source.read_text(encoding="utf-8"))
    names: dict[str, NameEntry] = {}
    for rpm, item in data.items():
        if not isinstance(item, dict) or "deb" not in item:
            raise Refused(f"name map entry {rpm} needs a deb field")
        names[rpm] = NameEntry(
            rpm=rpm,
            deb=str(item["deb"]),
            unit=str(item.get("unit", "")),
            deb_unit=str(item.get("deb_unit", "")),
        )
    return names
