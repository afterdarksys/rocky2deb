"""Debian suite profiles. Adding a release is a new entry here, not a code fork.

glibc versions are the baseline used for binary-symbol checks:
stretch 2.24, buster 2.28, bullseye 2.31, bookworm 2.36, trixie 2.41,
forky 2.43 (testing as of 2026-09-26). Rocky majors: 8 → 2.28, 9 → 2.34, 10 → 2.39.
"""

from __future__ import annotations

from dataclasses import dataclass

from rocky2deb.errors import Refused


@dataclass(frozen=True)
class SuiteProfile:
    name: str
    debian_version: int
    status: str
    archive: str
    debhelper_compat: int
    usrmerge: bool
    time64: bool
    sysusers: bool
    glibc: str
    libdir: str
    libexecdir: str
    check_valid_until: bool


def _profile(
    name: str,
    debian_version: int,
    status: str,
    archive: str,
    compat: int,
    *,
    usrmerge: bool,
    time64: bool,
    sysusers: bool,
    glibc: str,
    libexecdir: str,
    check_valid_until: bool,
) -> SuiteProfile:
    return SuiteProfile(
        name=name,
        debian_version=debian_version,
        status=status,
        archive=archive,
        debhelper_compat=compat,
        usrmerge=usrmerge,
        time64=time64,
        sysusers=sysusers,
        glibc=glibc,
        libdir="/usr/lib/x86_64-linux-gnu",
        libexecdir=libexecdir,
        check_valid_until=check_valid_until,
    )


PROFILES: dict[str, SuiteProfile] = {
    "stretch": _profile(
        "stretch", 9, "archived", "https://archive.debian.org/debian", 10,
        usrmerge=False, time64=False, sysusers=False, glibc="2.24",
        libexecdir="/usr/lib", check_valid_until=False,
    ),
    "buster": _profile(
        "buster", 10, "archived", "https://archive.debian.org/debian", 12,
        usrmerge=False, time64=False, sysusers=False, glibc="2.28",
        libexecdir="/usr/lib", check_valid_until=False,
    ),
    "bullseye": _profile(
        "bullseye", 11, "oldoldstable", "https://deb.debian.org/debian", 13,
        usrmerge=False, time64=False, sysusers=False, glibc="2.31",
        libexecdir="/usr/lib", check_valid_until=True,
    ),
    "bookworm": _profile(
        "bookworm", 12, "oldstable", "https://deb.debian.org/debian", 13,
        usrmerge=True, time64=False, sysusers=True, glibc="2.36",
        libexecdir="/usr/libexec", check_valid_until=True,
    ),
    "trixie": _profile(
        "trixie", 13, "stable", "https://deb.debian.org/debian", 13,
        usrmerge=True, time64=True, sysusers=True, glibc="2.41",
        libexecdir="/usr/libexec", check_valid_until=True,
    ),
    "forky": _profile(
        "forky", 14, "testing", "https://deb.debian.org/debian", 13,
        usrmerge=True, time64=True, sysusers=True, glibc="2.43",
        libexecdir="/usr/libexec", check_valid_until=True,
    ),
}

REQUESTED_SUITES = ("stretch", "buster", "bullseye", "bookworm", "trixie")

ROCKY_GLIBC = {8: "2.28", 9: "2.34", 10: "2.39"}


def get_profile(name: str) -> SuiteProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        known = ", ".join(PROFILES)
        raise Refused(f"unknown Debian suite {name!r}. Known suites: {known}") from exc


def debian_path(path: str, profile: SuiteProfile) -> str:
    """Rewrite EL library locations into the suite's Debian paths."""
    if path.startswith("/usr/lib64/"):
        path = profile.libdir + "/" + path[len("/usr/lib64/") :]
    elif path == "/usr/lib64":
        path = profile.libdir
    if path.startswith("/usr/libexec/"):
        path = profile.libexecdir + "/" + path[len("/usr/libexec/") :]
    elif path == "/usr/libexec":
        path = profile.libexecdir
    return path
