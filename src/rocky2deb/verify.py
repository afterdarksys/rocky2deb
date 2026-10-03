"""Compare an inventory with the packages, units, and listeners a wave requires.

Threats: verify reports missing names. It does not open a connection by itself.
A host check belongs to collect_inventory, which refuses unless execute is set.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rocky2deb.errors import Refused
from rocky2deb.inventory import Inventory


@dataclass
class VerifyResult:
    ok: bool
    missing_packages: list[str] = field(default_factory=list)
    missing_units: list[str] = field(default_factory=list)
    missing_listeners: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        rows = [f"missing package {name}" for name in self.missing_packages]
        rows.extend(f"missing unit {name}" for name in self.missing_units)
        rows.extend(f"missing listener {name}" for name in self.missing_listeners)
        return rows


def parse_listeners(text: str) -> set[str]:
    found: set[str] = set()
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t")
        if parts[0] != "LISTEN" or len(parts) != 3:
            raise Refused("unknown listener line")
        found.add(f"{parts[1]}/{parts[2]}")
    return found


def verify_against(
    inventory: Inventory,
    *,
    packages: list[str] | None = None,
    units: list[str] | None = None,
    listeners: list[str] | None = None,
    listener_text: str = "",
) -> VerifyResult:
    have_packages = {item.name for item in inventory.packages}
    have_units = set(inventory.units)
    have_listeners = parse_listeners(listener_text) if listeners else set()
    missing_packages = [name for name in packages or [] if name not in have_packages]
    missing_units = [name for name in units or [] if name not in have_units]
    missing_listeners = [name for name in listeners or [] if name not in have_listeners]
    return VerifyResult(
        ok=not (missing_packages or missing_units or missing_listeners),
        missing_packages=missing_packages,
        missing_units=missing_units,
        missing_listeners=missing_listeners,
    )
