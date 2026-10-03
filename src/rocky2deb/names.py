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

# Debian names the Rocky installer already provides. This is not a blind invert
# of BASE_DEB: rpm, dnf, and yum all map to apt, and the reverse of apt is dnf.
BASE_FROM_DEB = {
    "libc6": "glibc",
    "systemd": "systemd",
    "dbus": "dbus",
    "apt": "dnf",
    "dpkg": "rpm",
    "base-files": "rocky-release",
    "shim-signed": "shim-x64",
    "firmware-linux-nonfree": "linux-firmware",
    "ca-certificates": "ca-certificates",
    "coreutils": "coreutils",
    "bash": "bash",
    "passwd": "shadow-utils",
    "util-linux": "util-linux",
    "ncurses-base": "ncurses-base",
    "tzdata": "tzdata",
}

# Companions of a base package. The exact name lives in BASE_FROM_DEB.
_DEB_BASE_PREFIX_RPM = (
    ("libc6-", "glibc"),
    ("linux-headers", "kernel"),
    ("dpkg-", "rpm"),
    ("apt-", "dnf"),
    ("systemd-", "systemd"),
)

# Branding that must not be rebuilt into either distro. License text is not consulted.
DEB_TRADEMARK = frozenset(
    {
        "debian-logos",
        "ubuntu-mono",
        "ubuntu-wallpapers",
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


def is_deb_trademark(name: str) -> bool:
    return is_trademark(name) or name in DEB_TRADEMARK


def is_deb_base(name: str) -> bool:
    """True when Rocky already provides this Debian or Rocky package name."""
    if name in BASE_FROM_DEB or is_base(name):
        return True
    return any(name.startswith(prefix) for prefix, _rpm in _DEB_BASE_PREFIX_RPM)


def base_rpm(name: str) -> str:
    """Rocky package that satisfies a Debian base name. Empty when it is not base."""
    if name in BASE_FROM_DEB:
        return BASE_FROM_DEB[name]
    if name in BASE_DEB:
        return name
    for prefix, rpm in _DEB_BASE_PREFIX_RPM:
        if name.startswith(prefix):
            return rpm
    if name.startswith("linux-image"):
        return "kernel"
    if name.startswith(("grub-efi", "grub-pc", "grub2", "grub")):
        return "grub2-efi-x64"
    if name.startswith("kernel"):
        return name
    return ""


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
