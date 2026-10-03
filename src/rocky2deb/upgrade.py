"""Debian to Debian suite moves.

Rocky to Debian is a fresh install plus a replayed bundle. A later Debian
release is the one in-place path: the host must already be Debian, and its
codename must be the suite named by --from. Rebuilds that the new suite ships
flip to the Debian package. Pins stay rebuilds.
"""

from __future__ import annotations

from dataclasses import dataclass

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from rocky2deb.applybundle import assert_root
from rocky2deb.applycheck import parse_os_release
from rocky2deb.errors import Refused
from rocky2deb.profiles import get_profile
from rocky2deb.resolve import Decision

AptRunner = Callable[[list[str]], int]


@dataclass(frozen=True)
class UpgradeRow:
    name: str
    action: str
    deb: str = ""
    reason: str = ""


def diff_suite(decisions: list[Decision], new_index: dict[str, str]) -> list[UpgradeRow]:
    rows: list[UpgradeRow] = []
    for item in decisions:
        if item.action == "base":
            rows.append(UpgradeRow(item.rpm, "keep", item.deb, "base package"))
        elif item.action == "pin-rebuild":
            rows.append(UpgradeRow(item.rpm, "rebuild", item.deb, "pin still requests a rebuild"))
        elif item.action == "rebuild":
            if item.deb in new_index:
                rows.append(UpgradeRow(item.rpm, "flip-to-debian", item.deb, "Debian now ships this package"))
            elif item.rpm in new_index:
                rows.append(UpgradeRow(item.rpm, "flip-to-debian", item.rpm, "Debian now ships this package"))
            else:
                rows.append(UpgradeRow(item.rpm, "rebuild", item.deb, "still absent from Debian"))
        elif item.action == "debian":
            if item.deb in new_index or item.rpm in new_index:
                rows.append(UpgradeRow(item.rpm, "keep", item.deb, "still in the suite"))
            else:
                rows.append(UpgradeRow(item.rpm, "drop", item.deb, "absent from the new suite"))
        else:
            chosen = item.deb if item.deb in new_index else item.rpm
            if item.action == "gap" and (item.deb in new_index or item.rpm in new_index):
                deb = item.deb if item.deb in new_index else item.rpm
                rows.append(UpgradeRow(item.rpm, "flip-to-debian", deb, "suite now ships it"))
            else:
                rows.append(UpgradeRow(item.rpm, "blocked", chosen if chosen in new_index else "", item.reason or "gap"))
    return rows


def assert_suite_upgrade_host(os_release: str, from_suite: str) -> None:
    get_profile(from_suite)
    info = parse_os_release(os_release)
    host_id = info.get("ID", "")
    if host_id != "debian":
        raise Refused("suite-upgrade refuses a host that is not Debian")
    codename = info.get("VERSION_CODENAME", "")
    if codename != from_suite:
        raise Refused(f"host suite is {codename or 'missing'}, want {from_suite}")


def suite_upgrade_plan(
    os_release: str,
    from_suite: str,
    to_suite: str,
    decisions: list[Decision],
    new_index: dict[str, str],
) -> list[UpgradeRow]:
    get_profile(to_suite)
    assert_suite_upgrade_host(os_release, from_suite)
    return diff_suite(decisions, new_index)


def sources_list_line(suite: str) -> str:
    """One deb line for the suite archive. Archived suites skip Valid-Until."""
    profile = get_profile(suite)
    options = "" if profile.check_valid_until else "[check-valid-until=no] "
    return f"deb {options}{profile.archive} {suite} main\n"


def apt_commands(rows: list[UpgradeRow]) -> list[list[str]]:
    """Apt argv for an in-place upgrade. The caller runs them only with --execute."""
    debs = sorted({row.deb for row in rows if row.action == "rebuild" and row.deb})
    commands = [["apt-get", "update"]]
    if debs:
        commands.append(["apt-get", "install", "-y", "--", *debs])
    commands.append(["apt-get", "full-upgrade", "-y"])
    return commands


def _run_apt(argv: list[str]) -> int:
    completed = subprocess.run(argv, check=False, capture_output=True)
    return completed.returncode


def apply_suite_upgrade(
    root: Path,
    to_suite: str,
    rows: list[UpgradeRow],
    *,
    allow_live: bool = False,
    runner: AptRunner | None = None,
) -> None:
    """Write the suite source line under root and run the apt argv.

    A root other than / gets apt-get -o Dir=<root> so the controller's own
    apt database is left alone. The caller already refused a live / unless
    allow_live is set.
    """
    dest = assert_root(Path(root), allow_live=allow_live)
    if runner is None and shutil.which("apt-get") is None:
        raise Refused("apt-get is not installed; refusing to pretend the suite changed")
    run = runner or _run_apt
    sources = dest / "etc" / "apt" / "sources.list.d" / f"{to_suite}.list"
    sources.parent.mkdir(parents=True, exist_ok=True)
    partial = sources.with_name(sources.name + ".partial")
    partial.write_text(sources_list_line(to_suite), encoding="utf-8")
    partial.chmod(0o644)
    partial.replace(sources)
    live = dest == Path("/").resolve()
    for argv in apt_commands(rows):
        invocation = list(argv) if live else [argv[0], "-o", f"Dir={dest}", *argv[1:]]
        if run(invocation) != 0:
            raise Refused("apt-get failed; suite was not changed")


def retarget(
    to_suite: str,
    decisions: list[Decision],
    new_index: dict[str, str],
) -> tuple[str, list[UpgradeRow]]:
    """Re-resolve recorded decisions against a new suite index."""
    get_profile(to_suite)
    return to_suite, diff_suite(decisions, new_index)
