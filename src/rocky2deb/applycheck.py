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


def assert_rocky_release(text: str, major: int) -> None:
    """Accept Rocky 8, 9, or 10. The major is the VERSION_ID component before the dot.

    "9" does not match "90" or "19". Debian and Ubuntu are refused.
    """
    if major not in (8, 9, 10):
        raise Refused("rocky major must be 8, 9, or 10")
    info = parse_os_release(text)
    host_id = info.get("ID", "")
    if host_id != "rocky":
        raise Refused(f"refusing apply: host ID is {host_id or 'missing'}, want rocky")
    version_id = info.get("VERSION_ID", "")
    head = version_id.split(".", 1)[0] if version_id else ""
    if head != str(major):
        raise Refused(f"refusing apply: host version is {version_id or 'missing'}, want {major}")
