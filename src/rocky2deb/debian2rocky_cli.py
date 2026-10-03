"""debian2rocky command line.

Plans a fresh Rocky replay of a Debian host. Inventory is read-only. Apply
copies a bundle onto a Rocky root and does not run dnf. Emit writes a spec.
Build launches mock only with --execute and a real .src.rpm. pack copies a
spec into an unsigned .src.rpm and does not run it. repack writes a noarch
source-install spec and does not run debian/rules. publish writes dnf
metadata and does not sign it. fetch-source downloads a Debian archive
source only with --execute.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from rocky2deb.applybundle import apply_rocky_bundle
from rocky2deb.applycheck import assert_rocky_release
from rocky2deb.buildcmd import assert_build_backend, run_build
from rocky2deb.d2r import repack_from_tree, resolve_deb, spec_from_tree
from rocky2deb.debinventory import collect_deb_inventory, parse_deb_inventory
from rocky2deb.debian_src import fetch_debian_source
from rocky2deb.errors import Refused, Rocky2debError
from rocky2deb.fetch import DEFAULT_LIMIT, assert_https, read_limited
from rocky2deb.inventory import ssh_argv
from rocky2deb.names import load_name_map
from rocky2deb.profiles import get_profile
from rocky2deb.remap import format_remap_report, load_maps, remap_text_reverse
from rocky2deb.repodata import write_rpm_repo
from rocky2deb.srpm_pack import pack_src_rpm


def _index(pairs: list[str] | None) -> dict[str, str]:
    index: dict[str, str] = {}
    for item in pairs or []:
        if "=" not in item:
            raise Rocky2debError("index entry must be name=version")
        name, version = item.split("=", 1)
        index[name] = version
    return index


def _print_inventory(inventory) -> None:
    for package in inventory.packages:
        print(f"pkg {package.name} {package.version} {package.source}")
    for unit in inventory.units:
        print(f"unit {unit}")


def cmd_resolve(args: argparse.Namespace) -> int:
    decision = resolve_deb(
        args.name,
        index=_index(args.index),
        names=load_name_map(),
        pins=set(args.pin or []),
        license_text=args.license or "",
        build_system=args.build_system,
    )
    print(f"{decision.rpm}\t{decision.action}\t{decision.deb}\t{decision.reason}")
    return 0


def cmd_inventory_parse(args: argparse.Namespace) -> int:
    _print_inventory(parse_deb_inventory(Path(args.path).read_text(encoding="utf-8")))
    return 0


def cmd_inventory_collect(args: argparse.Namespace) -> int:
    _print_inventory(collect_deb_inventory(args.host, execute=args.execute))
    return 0


def cmd_ssh_argv(args: argparse.Namespace) -> int:
    for part in ssh_argv(args.host):
        print(part)
    return 0


def cmd_remap(args: argparse.Namespace) -> int:
    maps = load_maps()
    if args.map not in maps:
        raise Refused(f"unknown config map {args.map}")
    text = Path(args.file).read_text(encoding="utf-8")
    result = remap_text_reverse(text, maps[args.map])
    sys.stdout.write(format_remap_report(result))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    text = Path(args.os_release).read_text(encoding="utf-8")
    if not args.execute:
        assert_rocky_release(text, args.rocky)
        print("ok")
        return 0
    if not args.root or not args.bundle:
        raise Refused("apply needs --root and --bundle")
    written = apply_rocky_bundle(
        Path(args.root),
        args.rocky,
        Path(args.bundle),
        text,
        allow_live=args.allow_live_root,
    )
    print("executed")
    for rel in written:
        print(rel)
    return 0


def cmd_emit(args: argparse.Namespace) -> int:
    spec = spec_from_tree(Path(args.tree), args.license or "")
    dest = Path(args.output)
    _write_text(dest, spec)
    print(dest)
    return 0


def _read_regular(path: Path, missing: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise Refused(missing)
    with path.open("rb") as handle:
        return read_limited(handle, DEFAULT_LIMIT)


def _write_text(dest: Path, text: str) -> None:
    if dest.is_symlink():
        raise Refused("refusing symlink output")
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    partial.write_text(text, encoding="utf-8")
    partial.chmod(0o644)
    os.replace(partial, dest)


def cmd_pack(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec)
    spec = _read_regular(spec_path, "spec is missing")
    sources: list[tuple[str, bytes]] = []
    for item in args.source or []:
        path = Path(item)
        if path.is_symlink():
            raise Refused("refusing symlink source")
        if not path.is_file():
            raise Refused("source is missing")
        with path.open("rb") as handle:
            sources.append((path.name, read_limited(handle, DEFAULT_LIMIT)))
    dest = Path(args.output)
    if dest.is_symlink():
        raise Refused("refusing symlink destination")
    written = pack_src_rpm(spec, sources, dest)
    print(written)
    return 0


def cmd_repack(args: argparse.Namespace) -> int:
    if not args.pin:
        raise Refused("repack requires a pin")
    text = repack_from_tree(Path(args.tree), args.license or "", pinned=True)
    dest = Path(args.output)
    _write_text(dest, text)
    print(dest)
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    source = Path(args.rpms)
    dest = Path(args.output)
    if source.is_symlink() or not source.is_dir():
        raise Refused("rpm directory is missing")
    if dest.is_symlink():
        raise Refused("refusing symlink destination")
    written = write_rpm_repo(source, dest)
    print(written)
    return 0


def cmd_fetch_source(args: argparse.Namespace) -> int:
    if args.archive:
        assert_https(args.archive.strip().rstrip("/") + "/")
    else:
        assert_https(get_profile(args.suite).archive.rstrip("/") + "/")
    if not args.execute:
        raise Refused("refusing to fetch without --execute")
    blob = _read_regular(Path(args.sources), "missing sources")
    if not args.signature:
        raise Refused("missing signature")
    signature = _read_regular(Path(args.signature), "missing signature")
    if not args.keyring:
        raise Refused("missing keyring")
    keyring = Path(args.keyring)
    if keyring.is_symlink() or not keyring.is_file():
        raise Refused("missing keyring")
    if not args.dest:
        raise Refused("fetch needs --dest")
    dest = Path(args.dest)
    if dest.is_symlink():
        raise Refused("refusing symlink destination")
    fetch_debian_source(
        blob,
        args.package,
        suite=args.suite,
        execute=True,
        license_text=args.license or "",
        signature=signature,
        keyring=keyring,
        dest=dest,
        archive=args.archive or "",
    )
    print(dest)
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    assert_build_backend("rpm", args.execute)
    if not args.source or not str(args.source).endswith(".src.rpm"):
        raise Refused("rpm build needs a .src.rpm")
    source = Path(args.source)
    result = Path(args.result) if args.result else source.resolve().parent
    produced = run_build(
        "rpm",
        source,
        execute=True,
        resultdir=result,
        rocky_major=args.rocky,
    )
    for path in produced:
        print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="debian2rocky")
    sub = parser.add_subparsers(dest="command", required=True)

    resolve = sub.add_parser("resolve")
    resolve.add_argument("name")
    resolve.add_argument("--index", action="append", default=[])
    resolve.add_argument("--pin", action="append", default=[])
    resolve.add_argument("--license", default="")
    resolve.add_argument("--build-system", default="")
    resolve.set_defaults(func=cmd_resolve)

    parse = sub.add_parser("inventory-parse")
    parse.add_argument("path")
    parse.set_defaults(func=cmd_inventory_parse)

    collect = sub.add_parser("inventory-collect")
    collect.add_argument("host")
    collect.add_argument("--execute", action="store_true")
    collect.set_defaults(func=cmd_inventory_collect)

    ssh = sub.add_parser("ssh-argv")
    ssh.add_argument("host")
    ssh.set_defaults(func=cmd_ssh_argv)

    remap = sub.add_parser("remap")
    remap.add_argument("--map", required=True)
    remap.add_argument("--file", required=True)
    remap.set_defaults(func=cmd_remap)

    apply = sub.add_parser("apply")
    apply.add_argument("--rocky", required=True, type=int)
    apply.add_argument("--os-release", required=True)
    apply.add_argument("--execute", action="store_true")
    apply.add_argument("--root", default="")
    apply.add_argument("--bundle", default="")
    apply.add_argument("--allow-live-root", action="store_true")
    apply.set_defaults(func=cmd_apply)

    emit = sub.add_parser("emit")
    emit.add_argument("--tree", required=True)
    emit.add_argument("--license", default="")
    emit.add_argument("--output", required=True)
    emit.set_defaults(func=cmd_emit)

    pack = sub.add_parser("pack")
    pack.add_argument("--spec", required=True)
    pack.add_argument("--source", action="append", default=[])
    pack.add_argument("--output", required=True)
    pack.set_defaults(func=cmd_pack)

    repack = sub.add_parser("repack")
    repack.add_argument("--tree", required=True)
    repack.add_argument("--license", default="")
    repack.add_argument("--output", required=True)
    repack.add_argument("--pin", action="store_true")
    repack.set_defaults(func=cmd_repack)

    publish = sub.add_parser("publish")
    publish.add_argument("--rpms", required=True)
    publish.add_argument("--output", required=True)
    publish.set_defaults(func=cmd_publish)

    fetch = sub.add_parser("fetch-source")
    fetch.add_argument("--suite", required=True)
    fetch.add_argument("--sources", required=True)
    fetch.add_argument("--package", required=True)
    fetch.add_argument("--execute", action="store_true")
    fetch.add_argument("--license", default="")
    fetch.add_argument("--signature", default="")
    fetch.add_argument("--keyring", default="")
    fetch.add_argument("--dest", default="")
    fetch.add_argument("--archive", default="")
    fetch.set_defaults(func=cmd_fetch_source)

    build = sub.add_parser("build")
    build.add_argument("--execute", action="store_true")
    build.add_argument("--source", default="")
    build.add_argument("--rocky", type=int, default=0)
    build.add_argument("--result", default="")
    build.set_defaults(func=cmd_build)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return args.func(args) or 0
    except Rocky2debError as exc:
        print(f"debian2rocky: {exc}", file=sys.stderr)
        return exc.exit_code
