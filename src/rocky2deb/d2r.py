"""Choose Rocky, rebuild, pin-rebuild, repack, base, or gap for one Debian package.

Order, first match wins:
1. Base denylist, even when the package is pinned and even when the name is
   in the Rocky index.
2. Trademark, always a gap. License text is not consulted.
3. Explicit pin, when the license class allows it. The pin set names the
   Debian package. build_system ``dh`` or ``custom`` is ``repack``. Any
   other value, including empty, stays ``pin-rebuild``.
4. Reverse curated map, when that RPM is in the Rocky index.
5. The same name in the Rocky index.
6. Rebuild from the Debian source when the license allows it.
7. Gap.

The cutover is a fresh Rocky install, then replay. This module does not write
a host. spec_from_tree writes spec text only. repack_from_tree writes a
noarch source-install spec and does not run debian/rules.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from rocky2deb.emit_rpm import emit_repack_spec, emit_spec
from rocky2deb.errors import Refused, SpecUnemittable
from rocky2deb.ir import PackageIR
from rocky2deb.license import LedgerEntry, rebuild_permission
from rocky2deb.names import (
    NameEntry,
    base_rpm,
    deb_package_name,
    is_deb_base,
    is_deb_trademark,
)
from rocky2deb.resolve import Decision
from rocky2deb.dsc import import_dsc_tree

_EMITTABLE = frozenset({"autotools", "cmake", "meson", "python"})
_REPACK_SYSTEMS = frozenset({"dh", "custom"})
_KEEP_ACTIONS = frozenset({"rocky", "base", "pin-rebuild", "rebuild", "repack"})
_TARBALL_SUFFIXES = (".tar.gz", ".tar.xz", ".tar.bz2")


def mapped_rpm(deb: str, names: dict[str, NameEntry]) -> str:
    """RPM name curated for this Debian package.

    When several RPMs share the deb, prefer the RPM whose name equals the deb,
    otherwise the alphabetically first.
    """
    matches = [entry.rpm for entry in names.values() if entry.deb == deb]
    if not matches:
        return ""
    if deb in matches:
        return deb
    return sorted(matches)[0]


def _trademark(name: str, mapped: str, ledger: dict[str, LedgerEntry]) -> bool:
    if is_deb_trademark(name) or (mapped and is_deb_trademark(mapped)):
        return True
    for candidate in (name, mapped):
        entry = ledger.get(candidate)
        if entry is not None and entry.license_class == "trademark":
            return True
    return False


def resolve_deb(
    deb: str,
    *,
    index: dict[str, str],
    names: dict[str, NameEntry],
    pins: set[str] | None = None,
    ledger: dict[str, LedgerEntry] | None = None,
    license_text: str = "",
    build_system: str = "",
) -> Decision:
    """Resolve one Debian package against a Rocky package index.

    ``index`` maps a Rocky binary name to a version. The version is not
    compared. ``Decision.rpm`` is the Rocky package to install or build.
    ``Decision.deb`` is the Debian package that was asked for.
    """
    pins = pins or set()
    ledger = ledger or {}
    name = deb_package_name(deb)
    if is_deb_base(name):
        return Decision(
            base_rpm(name) or name,
            "base",
            name,
            "satisfied by the Rocky installer",
        )
    mapped = mapped_rpm(name, names)
    rpm_name = mapped or name
    if _trademark(name, rpm_name, ledger):
        return Decision(rpm_name, "gap", name, "trademark", "trademark")
    allowed, klass, why = rebuild_permission(name, license_text, ledger)
    if name in pins:
        if not allowed:
            return Decision(rpm_name, "gap", name, why, klass)
        if build_system in _REPACK_SYSTEMS:
            return Decision(rpm_name, "repack", name, why, klass)
        return Decision(rpm_name, "pin-rebuild", name, why, klass)
    if mapped and mapped in index:
        return Decision(mapped, "rocky", name, "curated map")
    if name in index:
        return Decision(name, "rocky", name, "same name in rocky index")
    if allowed:
        return Decision(rpm_name, "rebuild", name, why, klass)
    return Decision(rpm_name, "gap", name, why, klass)


def rocky_package_list(decisions: list[Decision]) -> list[str]:
    """Rocky package names to install or build. Gaps are omitted."""
    chosen = [item.rpm for item in decisions if item.action in _KEEP_ACTIONS and item.rpm]
    return sorted(set(chosen))


def spec_from_tree(tree: Path, license_text: str) -> str:
    """Emit an RPM spec from a debian/ tree. Does not execute the tree.

    The build system is checked first, so a dh or custom rules file is
    spec-unemittable even when the license was omitted. License text is the
    caller's string. debian/copyright is not read.
    """
    package = import_dsc_tree(tree)
    if package.build_system not in _EMITTABLE:
        raise SpecUnemittable(
            f"{package.name} build system {package.build_system!r} has no spec template"
        )
    safe = deb_package_name(package.name)
    if is_deb_trademark(package.name) or is_deb_trademark(safe):
        raise Refused("trademark")
    if not license_text.strip():
        raise Refused("missing license")
    allowed, _klass, why = rebuild_permission(safe, license_text, {})
    if not allowed:
        raise Refused(why)
    package = replace(package, license=license_text.strip())
    return emit_spec(package)


def _source_tarballs(tree: Path) -> list[str]:
    names: list[str] = []
    if not tree.is_dir():
        raise Refused("repack needs a source tarball")
    for path in sorted(tree.iterdir(), key=lambda item: item.name):
        name = path.name
        if not name.endswith(_TARBALL_SUFFIXES):
            continue
        if path.is_symlink():
            raise Refused("refusing symlink source")
        if not path.is_file():
            raise Refused("source is missing")
        if not name or name.startswith(".") or name.startswith("-") or ".." in name:
            raise Refused("refusing source file")
        names.append(name)
    if not names:
        raise Refused("repack needs a source tarball")
    return names


def repack_from_tree(tree: Path, license_text: str, *, pinned: bool) -> str:
    """Write a noarch repack spec. Does not pack a .src.rpm and does not run rules.

    ``pinned`` is required before the tree is read, so a missing tree with
    no pin still reports that repack requires a pin.
    """
    if not pinned:
        raise Refused("repack requires a pin")
    package = import_dsc_tree(tree)
    if package.build_system in _EMITTABLE:
        raise Refused("build system has a spec template")
    if package.build_system not in _REPACK_SYSTEMS:
        raise Refused("build system has a spec template")
    safe = deb_package_name(package.name)
    if is_deb_trademark(package.name) or is_deb_trademark(safe):
        raise Refused("trademark")
    if not license_text.strip():
        raise Refused("missing license")
    allowed, _klass, why = rebuild_permission(safe, license_text, {})
    if not allowed:
        raise Refused(why)
    sources = _source_tarballs(Path(tree))
    summary = package.summary or package.name
    return emit_repack_spec(
        name=package.name,
        version=package.version,
        release=package.release or "",
        license_text=license_text.strip(),
        summary=summary,
        sources=sources,
        epoch=package.epoch,
    )


def load_tree(tree: Path) -> PackageIR:
    """Import a debian/ tree without guessing a license."""
    return import_dsc_tree(tree)
