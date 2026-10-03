"""Parse a read-only Rocky inventory and build the ssh argv that collects it.

Threats: the host argument is one argv element, never a shell string. Names
outside [A-Za-z0-9_.:-] are refused, including values that start with '-' and
would otherwise be read as ssh options. ssh_argv does not open a connection.
collect_inventory does, and only when execute is set.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from rocky2deb.errors import Refused

HOST_RE = re.compile(r"[A-Za-z0-9_.:-]+")

REMOTE_SCRIPT = """\
rpm -qa --qf 'PKG\\t%{NAME}\\t%{VERSION}\\t%{RELEASE}\\t%{EPOCH}\\t%{ARCH}\\t%{SOURCERPM}\\n'
systemctl list-unit-files --state=enabled --no-legend --no-pager | awk '{print "UNIT\\t"$1"\\tenabled"}'
"""


@dataclass
class InventoryPackage:
    name: str
    version: str
    release: str
    epoch: str
    arch: str
    sourcerpm: str


@dataclass
class Inventory:
    packages: list[InventoryPackage] = field(default_factory=list)
    units: list[str] = field(default_factory=list)


def parse_inventory(text: str) -> Inventory:
    inventory = Inventory()
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t")
        if parts[0] == "PKG" and len(parts) == 7:
            epoch = "" if parts[4] in {"(none)", "0"} else parts[4]
            inventory.packages.append(
                InventoryPackage(parts[1], parts[2], parts[3], epoch, parts[5], parts[6])
            )
            continue
        if parts[0] == "UNIT" and len(parts) == 3:
            inventory.units.append(parts[1])
            continue
        raise Refused("unknown inventory line")
    return inventory


def ssh_argv(host: str) -> list[str]:
    if host.startswith("-") or HOST_RE.fullmatch(host) is None:
        raise Refused("refusing host name")
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        host,
        "bash",
        "-s",
    ]


Collector = Callable[[list[str], str], str]


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


def collect_inventory(host: str, *, execute: bool, runner: Collector | None = None) -> Inventory:
    if not execute:
        raise Refused("refusing to collect inventory without --execute")
    argv = ssh_argv(host)
    text = (runner or _ssh_collect)(argv, REMOTE_SCRIPT)
    return parse_inventory(text)
