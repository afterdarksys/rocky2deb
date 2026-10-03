"""Turn a spec plus its tarball into a .dsc, then into a package file.

The license check runs again on the spec that will be built. A plan decision
is not trusted on its own. debian/rules is not executed here; sbuild runs it
inside the unshare chroot.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rocky2deb.buildcmd import Runner, run_build
from rocky2deb.dscpack import find_orig, materialize_patches, pack_source
from rocky2deb.emit_deb import emit_deb_tree
from rocky2deb.errors import Refused
from rocky2deb.license import load_ledger, rebuild_permission
from rocky2deb.rockify import RockifyPlan, emit_compat_source
from rocky2deb.spec import parse_spec
from rocky2deb.srpm import read_src_rpm, spec_in


def stage_dsc(
    spec_text: str,
    source_dir: Path,
    suite: str,
    work: Path,
    orig: Path | None = None,
) -> Path:
    package = parse_spec(spec_text, source_id=str(source_dir))
    allowed, _klass, reason = rebuild_permission(package.name, package.license, load_ledger())
    if not allowed:
        raise Refused(reason)
    root = Path(work)
    tree = emit_deb_tree(package, suite, root / "tree")
    materialize_patches(tree, source_dir)
    tarball = Path(orig) if orig is not None else find_orig(source_dir)
    return pack_source(tree, tarball, root / "dsc")


def source_dir_for(name: str, sources: Path, work: Path) -> Path:
    srpm = Path(sources) / f"{name}.src.rpm"
    directory = Path(sources) / name
    if srpm.is_file() and not srpm.is_symlink():
        extracted = work / "extracted"
        read_src_rpm(srpm, extracted)
        return extracted
    if directory.is_dir() and not directory.is_symlink():
        spec_in(directory)
        return directory
    raise Refused("missing source")


def build_closure(
    plan: RockifyPlan,
    sources: Path,
    output: Path,
    *,
    execute: bool,
    which: Callable[[str], str | None] | None = None,
    runner: Runner | None = None,
    arch: str = "amd64",
) -> list[Path]:
    """Build plan.order in order. A blocked plan produces nothing."""
    if plan.report == "blocked":
        raise Refused(plan.reason or "blocked")
    if plan.order and not execute:
        raise Refused("refusing to build without --execute")
    produced: list[Path] = []
    dest = Path(output)
    result = dest / "artifacts" / plan.suite
    for name in plan.order:
        work = dest / "work" / name
        source = source_dir_for(name, sources, work)
        spec_path = spec_in(source)
        dsc = stage_dsc(spec_path.read_text(encoding="utf-8"), source, plan.suite, work)
        produced.extend(
            run_build(
                "deb",
                dsc,
                execute=execute,
                resultdir=result,
                suite=plan.suite,
                arch=arch,
                which=which,
                runner=runner,
            )
        )
    emit_compat_source(plan, dest / "compat")
    return produced
