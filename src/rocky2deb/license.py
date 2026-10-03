"""License classification for rebuilds.

Threats: an empty, mixed, or unrecognized license must not be treated as free
and must not be stored as a rebuildable artifact. This module does not verify
signatures, does not fetch licenses from the network, and does not decide
trademark by SPDX token. Branding packages are named in the ledger and in
names.is_trademark. Unknown stays unknown: this code never guesses non-free.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from rocky2deb.errors import Refused
from rocky2deb.resources import package_data

_CLASSES = frozenset({"dfsg", "non-free", "trademark", "unknown"})

# RPM license tags after light normalization. Conjunctions are handled separately.
_DFSG = frozenset(
    {
        "mit",
        "bsd",
        "bsd-2-clause",
        "bsd-3-clause",
        "isc",
        "zlib",
        "apache-2.0",
        "mpl-2.0",
        "public-domain",
        "unlicense",
        "cc0-1.0",
        "gplv2",
        "gplv2+",
        "gpl+",
        "gpl-2.0",
        "gpl-2.0-only",
        "gpl-2.0-or-later",
        "gpl-2.0+",
        "gplv3",
        "gplv3+",
        "gpl-3.0",
        "gpl-3.0-only",
        "gpl-3.0-or-later",
        "lgplv2+",
        "lgplv2.1",
        "lgplv2.1+",
        "lgpl-2.1",
        "lgpl-2.1-only",
        "lgpl-2.1-or-later",
        "lgplv3",
        "lgplv3+",
        "lgpl+",
        "lgpl-3.0",
        "lgpl-3.0-only",
        "lgpl-3.0-or-later",
        "bsl-1.0",
        "boost",
        "psf-2.0",
        "artistic-2.0",
    }
)

_ALIASES = {
    "asl2.0": "apache-2.0",
    "asl-2.0": "apache-2.0",
    "apache2.0": "apache-2.0",
    "mplv2.0": "mpl-2.0",
    "gplv2.0+": "gplv2+",
    "bsd2clause": "bsd-2-clause",
    "bsd3clause": "bsd-3-clause",
}


@dataclass(frozen=True)
class LedgerEntry:
    name: str
    license_class: str
    allow_rebuild: bool
    note: str = ""


def _canon(token: str) -> str:
    text = token.strip().lower().replace("license", "")
    text = "".join(text.split())
    return _ALIASES.get(text, text)


def _is_dfsg_token(token: str) -> bool:
    return _canon(token) in _DFSG


def classify_text(license_text: str) -> str:
    """Return dfsg or unknown. Never returns non-free or trademark."""
    text = (license_text or "").strip()
    if not text:
        return "unknown"
    folded = text.casefold()
    if " and " in folded and " or " in folded:
        return "unknown"
    if " and " in folded:
        parts = _split_on(text, " and ")
        if parts and all(_is_dfsg_token(part) for part in parts):
            return "dfsg"
        return "unknown"
    if " or " in folded:
        parts = _split_on(text, " or ")
        if any(_is_dfsg_token(part) for part in parts):
            return "dfsg"
        return "unknown"
    if _is_dfsg_token(text):
        return "dfsg"
    return "unknown"


def _split_on(text: str, needle: str) -> list[str]:
    folded = text.casefold()
    needle_cf = needle.casefold()
    parts: list[str] = []
    start = 0
    while True:
        index = folded.find(needle_cf, start)
        if index < 0:
            parts.append(text[start:].strip())
            break
        parts.append(text[start:index].strip())
        start = index + len(needle)
    return [part for part in parts if part]


def load_ledger(path: Path | None = None) -> dict[str, LedgerEntry]:
    source = path or package_data("ledger.toml")
    data = tomllib.loads(source.read_text(encoding="utf-8"))
    ledger: dict[str, LedgerEntry] = {}
    for name, item in data.items():
        if not isinstance(item, dict):
            raise Refused(f"ledger entry {name} is not a table")
        license_class = str(item.get("class", ""))
        if license_class not in _CLASSES:
            raise Refused(f"ledger entry {name} has class {license_class!r}")
        ledger[name] = LedgerEntry(
            name=name,
            license_class=license_class,
            allow_rebuild=bool(item.get("allow_rebuild", False)),
            note=str(item.get("note", "")),
        )
    return ledger


def rebuild_permission(
    name: str, license_text: str, ledger: dict[str, LedgerEntry]
) -> tuple[bool, str, str]:
    """Return (allowed, license_class, reason). Fail closed."""
    entry = ledger.get(name)
    if entry is not None:
        if entry.license_class == "dfsg":
            return True, "dfsg", "ledger"
        if entry.license_class == "non-free" and entry.allow_rebuild:
            return True, "non-free", "ledger allow_rebuild"
        if entry.license_class == "non-free":
            return False, "non-free", "non-free rebuild not allowed"
        return False, entry.license_class, entry.license_class
    kind = classify_text(license_text)
    if kind == "dfsg":
        return True, "dfsg", "free license token"
    return False, "unknown", "license unknown"
