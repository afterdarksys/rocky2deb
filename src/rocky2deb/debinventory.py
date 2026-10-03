"""Parse a read-only Debian inventory and collect it over ssh.

Threats: the host is one argv element, never a shell string. Names outside
[A-Za-z0-9_.:-] are refused, including a leading '-', and the rejected host is
not echoed. ssh_argv does not open a connection. collect_deb_inventory does,
and only when execute is set. A Rocky seven-field PKG line is not a Debian
package line.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from rocky2deb.errors import Refused
from rocky2deb.inventory import ssh_argv

REMOTE_SCRIPT = """\
dpkg-query -W -f 'PKG\\t${Package}\\t${Version}\\t${Architecture}\\t${Source}\\n'
systemctl list-unit-files --state=enabled --no-legend --no-pager | awk '{print "UNIT\\t"$1"\\tenabled"}'
"""

Collector = Callable[[list[str], str], str]


@dataclass
class DebPackage:
    name: str
    version: str
    epoch: str
    upstream: str
    revision: str
    arch: str
    source: str


@dataclass
class DebInventory:
    packages: list[DebPackage] = field(default_factory=list)
    units: list[str] = field(default_factory=list)


def split_deb_version(version: str) -> tuple[str, str, str]:
    """Split a Debian version into epoch, upstream, and revision.

    Epoch ``0`` and ``(none)`` become empty. The revision is the text after
    the last hyphen. A native version has an empty revision.
    """
    epoch = ""
    rest = version
    if ":" in rest:
        epoch, rest = rest.split(":", 1)
    if epoch in {"", "0", "(none)"}:
        epoch = ""
    if "-" in rest:
        upstream, revision = rest.rsplit("-", 1)
    else:
        upstream, revision = rest, ""
    return epoch, upstream, revision


def _source_name(field: str) -> str:
    text = field.strip()
    if " (" in text:
        text = text.split(" (", 1)[0].strip()
    return text


def parse_deb_inventory(text: str) -> DebInventory:
    """Parse dpkg-query PKG lines (five fields) and UNIT lines (three)."""
    inventory = DebInventory()
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t")
        if parts[0] == "PKG" and len(parts) == 5:
            version = parts[2]
            epoch, upstream, revision = split_deb_version(version)
            inventory.packages.append(
                DebPackage(
                    name=parts[1],
                    version=version,
                    epoch=epoch,
                    upstream=upstream,
                    revision=revision,
                    arch=parts[3],
                    source=_source_name(parts[4]),
                )
            )
            continue
        if parts[0] == "UNIT" and len(parts) == 3:
            inventory.units.append(parts[1])
            continue
        raise Refused("unknown inventory line")
    return inventory


def _ssh_collect(argv: list[str], script: str) -> str:
    try:
        completed = subprocess.run(
            argv,
            input=script,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise Refused("inventory collect timed out") from exc
    if completed.returncode != 0:
        raise Refused("inventory collect failed")
    return completed.stdout


def collect_deb_inventory(
    host: str, *, execute: bool, runner: Collector | None = None
) -> DebInventory:
    if not execute:
        raise Refused("refusing to collect inventory without --execute")
    argv = ssh_argv(host)
    text = (runner or _ssh_collect)(argv, REMOTE_SCRIPT)
    return parse_deb_inventory(text)
