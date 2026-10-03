"""Launch sbuild or mock inside its own chroot.

The controller never runs %prep or %build. sbuild --chroot-mode=unshare
builds a .dsc with mmdebstrap. mock --rebuild builds a .src.rpm. Both refuse
without --execute, refuse when the tool is missing, and refuse a zero exit
that did not write a new package file.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from rocky2deb.errors import Refused
from rocky2deb.profiles import get_profile

_TOOLS = {"deb": "sbuild", "rpm": "mock"}
_ARCH = {"amd64", "arm64", "i386", "all"}
_LOG_CAP = 1024 * 1024

Runner = Callable[[list[str], Path], int]


def assert_build_backend(
    kind: str,
    execute: bool,
    which: Callable[[str], str | None] | None = None,
) -> str:
    if kind not in _TOOLS:
        raise Refused("build kind must be deb or rpm")
    if not execute:
        raise Refused("refusing to build without --execute")
    tool = _TOOLS[kind]
    finder = which or shutil.which
    found = finder(tool)
    if found is None:
        raise Refused(f"{tool} is not installed; refusing to pretend the build succeeded")
    return found


def deb_argv(tool: str, dsc: Path, suite: str, arch: str) -> list[str]:
    get_profile(suite)
    if arch not in _ARCH:
        raise Refused("build arch must be amd64, arm64, i386, or all")
    source = Path(dsc)
    if not source.is_file() or source.suffix != ".dsc":
        raise Refused("deb build needs a .dsc")
    return [
        tool,
        "--dist",
        suite,
        "--arch",
        arch,
        "--chroot-mode=unshare",
        "--no-run-lintian",
        str(source),
    ]


def rpm_argv(tool: str, srpm: Path, rocky_major: int, resultdir: Path) -> list[str]:
    if rocky_major not in (8, 9, 10):
        raise Refused("rocky_major must be 8, 9, or 10")
    source = Path(srpm)
    if not source.is_file() or not source.name.endswith(".src.rpm"):
        raise Refused("rpm build needs a .src.rpm")
    return [
        tool,
        "--root",
        f"rocky-{rocky_major}-x86_64",
        "--resultdir",
        str(resultdir),
        "--rebuild",
        str(source),
    ]


def _write_log(resultdir: Path, stdout: bytes, stderr: bytes) -> None:
    blob = stdout + b"\n" + stderr
    (resultdir / "rocky2deb-build.log").write_bytes(blob[:_LOG_CAP])


def default_runner(argv: list[str], cwd: Path, timeout: int = 7200) -> int:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        _write_log(cwd, exc.stdout or b"", exc.stderr or b"")
        raise Refused("build timed out; no package was produced") from exc
    _write_log(cwd, completed.stdout, completed.stderr)
    return completed.returncode


def run_build(
    kind: str,
    source: Path,
    *,
    execute: bool,
    resultdir: Path,
    suite: str = "",
    arch: str = "amd64",
    rocky_major: int = 0,
    which: Callable[[str], str | None] | None = None,
    runner: Runner | None = None,
) -> list[Path]:
    tool = assert_build_backend(kind, execute, which)
    result = Path(resultdir)
    if result.is_symlink():
        raise Refused("refusing symlink result directory")
    result.mkdir(parents=True, exist_ok=True)
    if kind == "deb":
        argv = deb_argv(tool, source, suite, arch)
    else:
        argv = rpm_argv(tool, source, rocky_major, result)
    before = set(result.glob("*.deb")) | set(result.glob("*.rpm"))
    code = (runner or default_runner)(argv, result)
    if code != 0:
        raise Refused(f"{Path(tool).name} failed; no package was produced")
    created = (set(result.glob("*.deb")) | set(result.glob("*.rpm"))) - before
    if not created:
        raise Refused(f"{Path(tool).name} exited 0 but no package was produced")
    return sorted(created)
