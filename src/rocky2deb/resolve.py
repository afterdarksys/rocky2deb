"""Choose Debian, rebuild, pin-rebuild, base, or gap for one RPM.

Order, first match wins:
1. Base denylist, even when the package is pinned.
2. Trademark, always a gap.
3. Explicit rebuild pin, when the license class allows it.
4. Curated name map, when that Debian package is in the suite index and any
   version floor is met.
5. The same upstream name in the suite index.
6. Rebuild from the Rocky source when the license allows it.
7. Gap, with the reason filled in.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from rocky2deb.license import LedgerEntry, rebuild_permission
from rocky2deb.names import NameEntry, base_deb, deb_package_name, is_base, is_trademark
from rocky2deb.version import debian_at_least

_KEEP_ACTIONS = frozenset({"debian", "base", "pin-rebuild", "rebuild"})


@dataclass(frozen=True)
class Decision:
    rpm: str
    action: str
    deb: str = ""
    reason: str = ""
    license_class: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_one(
    rpm: str,
    *,
    index: dict[str, str],
    names: dict[str, NameEntry],
    pins: set[str] | None = None,
    ledger: dict[str, LedgerEntry] | None = None,
    license_text: str = "",
    floors: dict[str, str] | None = None,
) -> Decision:
    pins = pins or set()
    ledger = ledger or {}
    floors = floors or {}
    if is_base(rpm):
        return Decision(rpm, "base", base_deb(rpm), "satisfied by the Debian installer")
    if is_trademark(rpm) or (rpm in ledger and ledger[rpm].license_class == "trademark"):
        return Decision(rpm, "gap", "", "trademark", "trademark")
    allowed, klass, why = rebuild_permission(rpm, license_text, ledger)
    if rpm in pins:
        if allowed:
            return Decision(rpm, "pin-rebuild", deb_package_name(rpm), why, klass)
        return Decision(rpm, "gap", "", why, klass)
    entry = names.get(rpm)
    if entry is not None and entry.deb in index and _floor_ok(index[entry.deb], floors.get(rpm, "")):
        return Decision(rpm, "debian", entry.deb, "curated map")
    if rpm in index and _floor_ok(index[rpm], floors.get(rpm, "")):
        return Decision(rpm, "debian", deb_package_name(rpm), "same name in suite index")
    if allowed:
        return Decision(rpm, "rebuild", deb_package_name(rpm), why, klass)
    return Decision(rpm, "gap", "", why, klass)


def _floor_ok(have: str, floor: str) -> bool:
    if not floor:
        return True
    return debian_at_least(have, floor)


def package_list(decisions: list[Decision]) -> list[str]:
    """Debian package names to install or build. Gaps are omitted."""
    names = [item.deb for item in decisions if item.action in _KEEP_ACTIONS and item.deb]
    return sorted(set(names))
