"""Refuse to apply a bundle unless the host is the requested Debian suite.

The check reads ID and VERSION_CODENAME from os-release text. It does not
contact a host and it does not change one.
"""

from __future__ import annotations

from rocky2deb.errors import Refused


def parse_os_release(text: str) -> dict[str, str]:
    info: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        info[key.strip()] = value.strip().strip('"').strip("'")
    return info


def assert_debian_suite(text: str, suite: str) -> None:
    info = parse_os_release(text)
    host_id = info.get("ID", "")
    codename = info.get("VERSION_CODENAME", "")
    if host_id != "debian":
        raise Refused(f"refusing apply: host ID is {host_id or 'missing'}, want debian")
    if codename != suite:
        raise Refused(f"refusing apply: host suite is {codename or 'missing'}, want {suite}")
