"""Plan the dependency closure that lets one RPM name run on a Debian suite.

Default strategy: rebuild the gap closure against the suite libc. Binary
transplant is a diagnostic. It blocks when an ELF GLIBC symbol is newer than
the suite ceiling, and the source plan stays available.

A kernel, kernel module, glibc, systemd, or the rpm/dnf stack is blocked.
An empty compat package is not written when the plan has no shims.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from rocky2deb.errors import CycleError, Refused
from rocky2deb.license import LedgerEntry, load_ledger
from rocky2deb.names import NameEntry, deb_package_name, is_blocked_root, load_name_map
from rocky2deb.primary import RepoPackage
from rocky2deb.profiles import ROCKY_GLIBC, get_profile
from rocky2deb.remap import SFTP_DEBIAN, SFTP_ROCKY, ConfigMap, load_maps
from rocky2deb.resolve import Decision, resolve_one
from rocky2deb.version import cmp_glibc

_GLIBC_RE = re.compile(r"GLIBC_(\d+(?:\.\d+)*)")
_REBUILD = frozenset({"rebuild", "pin-rebuild"})


@dataclass(frozen=True)
class Shim:
    kind: str
    source: str = ""
    dest: str = ""
    note: str = ""


@dataclass
class RockifyPlan:
    name: str
    suite: str
    rocky_major: int
    report: str
    reason: str = ""
    order: list[str] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    shims: list[Shim] = field(default_factory=list)
    binary: str = "skipped"
    binary_reason: str = ""
    unbuildable: bool = False


def usable_dep(name: str) -> bool:
    return bool(name) and not name.startswith(("rpmlib(", "config(", "/", "("))


def topo_sort(nodes: list[str], prerequisites: dict[str, set[str]]) -> list[str]:
    """Kahn sort. The ready queue is alphabetical. A cycle names the leftovers."""
    indegree = {node: 0 for node in nodes}
    forward: dict[str, set[str]] = {node: set() for node in nodes}
    for node in nodes:
        for dep in prerequisites.get(node, set()):
            if dep not in indegree:
                continue
            forward[dep].add(node)
            indegree[node] += 1
    ready = sorted(node for node, degree in indegree.items() if degree == 0)
    ordered: list[str] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for nxt in sorted(forward[current]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
                ready.sort()
    if len(ordered) != len(nodes):
        leftover = sorted(set(nodes) - set(ordered))
        raise CycleError(leftover)
    return ordered


def binary_glibc_status(symbols: list[str], suite: str) -> tuple[str, str]:
    ceiling = get_profile(suite).glibc
    too_new = []
    for symbol in symbols:
        match = _GLIBC_RE.search(symbol)
        if match and cmp_glibc(match.group(1), ceiling) > 0:
            too_new.append(symbol)
    if too_new:
        listed = ", ".join(too_new)
        return "blocked", f"{listed} exceeds glibc {ceiling}; rebuild from source"
    return "ok", ""


def _dep_names(package: RepoPackage, weak: bool) -> list[str]:
    raw = list(package.requires) + list(package.build_requires)
    if weak:
        raw.extend(package.recommends)
    seen: list[str] = []
    for name in raw:
        if usable_dep(name) and name not in seen:
            seen.append(name)
    return seen


def _blocked_plan(name: str, suite: str, rocky_major: int, reason: str, binary: str, binary_reason: str) -> RockifyPlan:
    return RockifyPlan(
        name=name,
        suite=suite,
        rocky_major=rocky_major,
        report="blocked",
        reason=reason,
        binary=binary,
        binary_reason=binary_reason,
    )


def _collect_shims(
    decisions: dict[str, Decision],
    names: dict[str, NameEntry],
    maps: dict[str, ConfigMap],
    suite: str,
) -> list[Shim]:
    profile = get_profile(suite)
    unit_root = "/usr/lib/systemd/system" if profile.usrmerge else "/lib/systemd/system"
    shims: list[Shim] = []
    for rpm, decision in decisions.items():
        if decision.action == "gap":
            continue
        entry = names.get(rpm)
        if (
            decision.action == "debian"
            and entry is not None
            and entry.unit
            and entry.deb_unit
            and entry.unit != entry.deb_unit
        ):
            shims.append(
                Shim(
                    "unit-alias",
                    f"{unit_root}/{entry.deb_unit}",
                    f"/etc/systemd/system/{entry.unit}",
                    f"{entry.unit} alias of {entry.deb_unit}",
                )
            )
        if rpm == "openssh-server":
            shims.append(Shim("sftp", SFTP_ROCKY, SFTP_DEBIAN, "sshd subsystem path"))
        for cmap in maps.values():
            if rpm in cmap.packages and cmap.rocky != cmap.debian:
                shims.append(Shim("config-path", cmap.rocky, cmap.debian, cmap.id))
    return shims


def plan_closure(
    name: str,
    packages: list[RepoPackage],
    *,
    suite: str,
    rocky_major: int,
    index: dict[str, str] | None = None,
    names: dict[str, NameEntry] | None = None,
    pins: set[str] | None = None,
    ledger: dict[str, LedgerEntry] | None = None,
    binary_symbols: list[str] | None = None,
    compiler_log: str = "",
    weak: bool = False,
) -> RockifyPlan:
    if rocky_major not in ROCKY_GLIBC:
        raise Refused("rocky_major must be 8, 9, or 10")
    get_profile(suite)
    index = index or {}
    names = names if names is not None else load_name_map()
    pins = pins or set()
    ledger = ledger if ledger is not None else load_ledger()
    binary = "skipped"
    binary_reason = ""
    if binary_symbols is not None:
        binary, binary_reason = binary_glibc_status(binary_symbols, suite)
    if is_blocked_root(name):
        return _blocked_plan(
            name,
            suite,
            rocky_major,
            "kernel, glibc, systemd, and the rpm stack stay on the Debian installer",
            binary,
            binary_reason,
        )
    by_name = {package.name: package for package in packages}
    decisions: dict[str, Decision] = {}
    prerequisites: dict[str, set[str]] = {}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(rpm: str) -> None:
        if rpm in visited or rpm in visiting:
            return
        visiting.add(rpm)
        package = by_name.get(rpm)
        decision = resolve_one(
            rpm,
            index=index,
            names=names,
            pins=pins,
            ledger=ledger,
            license_text=package.license if package else "",
        )
        decisions[rpm] = decision
        deps = _dep_names(package, weak) if package else []
        prereqs: set[str] = set()
        for dep in deps:
            visit(dep)
            if (
                decision.action in _REBUILD
                and decisions[dep].action in _REBUILD
            ):
                prereqs.add(dep)
        if decision.action in _REBUILD:
            prerequisites[rpm] = prereqs
        visiting.remove(rpm)
        visited.add(rpm)

    visit(name)
    order = topo_sort(list(prerequisites), prerequisites)
    gaps = [item for item in decisions.values() if item.action == "gap"]
    root_pkg = by_name.get(name)
    unbuildable = bool(compiler_log) and not (root_pkg and root_pkg.patches)
    shims = _collect_shims(decisions, names, load_maps(), suite)
    reason = ""
    if gaps:
        report = "blocked"
        reason = gaps[0].reason
    elif unbuildable:
        report = "blocked"
        reason = "toolchain too old for the suite and the package has no patch"
    elif shims:
        report = "runs-with-shims"
    else:
        report = "runs"
    return RockifyPlan(
        name=name,
        suite=suite,
        rocky_major=rocky_major,
        report=report,
        reason=reason,
        order=order,
        decisions=list(decisions.values()),
        shims=shims,
        binary=binary,
        binary_reason=binary_reason,
        unbuildable=unbuildable,
    )


def emit_compat_source(plan: RockifyPlan, dest: Path) -> Path | None:
    """Write a metapackage when the plan has shims. Return None otherwise."""
    if plan.report == "blocked" or not plan.shims:
        return None
    deb = deb_package_name(f"rockify-compat-{plan.name}")
    provides = deb_package_name(f"rockify-{plan.name}")
    root = Path(dest) / deb
    debian = root / "debian"
    debian.mkdir(parents=True, exist_ok=True)
    depends = sorted(
        {
            item.deb
            for item in plan.decisions
            if item.deb and item.action in {"debian", "rebuild", "pin-rebuild"}
        }
    )
    depend_line = ", ".join(depends) if depends else "${misc:Depends}"
    control = (
        f"Source: {deb}\n"
        "Section: misc\n"
        "Priority: optional\n"
        "Maintainer: rocky2deb <rocky2deb@localhost>\n"
        "Build-Depends: debhelper-compat (= 13)\n"
        "Standards-Version: 4.7.0\n"
        "Rules-Requires-Root: no\n"
        "\n"
        f"Package: {deb}\n"
        "Architecture: all\n"
        f"Depends: {depend_line}\n"
        f"Provides: {provides}\n"
        f"Description: Compatibility package for {plan.name} on Debian\n"
        f" Pulls the resolved packages for {plan.name} and installs unit aliases\n"
        " when the Debian unit name differs.\n"
    )
    (debian / "control").write_text(control, encoding="utf-8")
    (debian / "rules").write_text("#!/usr/bin/make -f\n%:\n\tdh $@\n", encoding="utf-8")
    (debian / "changelog").write_text(
        f"{deb} (1.0-1) {plan.suite}; urgency=medium\n\n"
        f"  * Compatibility package for {plan.name}.\n\n"
        " -- rocky2deb <rocky2deb@localhost>  Sat, 03 Oct 2026 00:00:00 +0000\n",
        encoding="utf-8",
    )
    link_lines = [
        f"{shim.source} {shim.dest}"
        for shim in plan.shims
        if shim.kind == "unit-alias" and shim.source and shim.dest
    ]
    if link_lines:
        (debian / f"{deb}.links").write_text("\n".join(link_lines) + "\n", encoding="utf-8")
    notes = [f"{shim.kind} {shim.source} -> {shim.dest}" for shim in plan.shims]
    (debian / "rockify-notes").write_text("\n".join(notes) + "\n", encoding="utf-8")
    return root
