"""ubuntu-exporter command line.

plan prints the retargeted version and the Sources file names. It does not
download. export downloads only with --execute, and only after the archive
URL, the license, and gpgv have been accepted. Pool files are not fetched
by this module's plan command.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rocky2deb.errors import Refused, Rocky2debError
from rocky2deb.fetch import DEFAULT_LIMIT, assert_https, read_limited
from rocky2deb.ubuntu_src import UBUNTU_ARCHIVE, export_ubuntu


def _read_sources(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise Refused("missing sources")
    with path.open("rb") as handle:
        return read_limited(handle, DEFAULT_LIMIT)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sources", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--ubuntu", required=True)
    parser.add_argument("--distro", default="debian")
    parser.add_argument("--suite", default="")
    parser.add_argument("--rocky", type=int, default=0)


def cmd_plan(args: argparse.Namespace) -> int:
    plan = export_ubuntu(
        _read_sources(Path(args.sources)),
        args.package,
        ubuntu_suite=args.ubuntu,
        distro=args.distro,
        debian_suite=args.suite,
        rocky_major=args.rocky,
        execute=False,
    )
    print(plan.version)
    for item in plan.files:
        print(f"{item.sha256} {item.size} {item.name}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    archive = (args.archive or UBUNTU_ARCHIVE).strip()
    assert_https(archive.rstrip("/") + "/")
    if not args.execute:
        raise Refused("refusing to export without --execute")
    sources = Path(args.sources)
    blob = _read_sources(sources)
    if not args.signature:
        raise Refused("missing signature")
    signature = Path(args.signature)
    if signature.is_symlink() or not signature.is_file():
        raise Refused("missing signature")
    if not args.keyring:
        raise Refused("missing keyring")
    keyring = Path(args.keyring)
    if keyring.is_symlink() or not keyring.is_file():
        raise Refused("missing keyring")
    if not args.dest:
        raise Refused("export needs --dest")
    dest = Path(args.dest)
    if dest.is_symlink():
        raise Refused("refusing symlink destination")
    with signature.open("rb") as handle:
        sig_bytes = read_limited(handle, DEFAULT_LIMIT)
    export_ubuntu(
        blob,
        args.package,
        ubuntu_suite=args.ubuntu,
        distro=args.distro,
        debian_suite=args.suite,
        rocky_major=args.rocky,
        execute=True,
        license_text=args.license or "",
        signature=sig_bytes,
        keyring=keyring,
        dest=dest,
        archive=archive,
    )
    print(dest)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ubuntu-exporter")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    _common(plan)
    plan.set_defaults(func=cmd_plan)
    export = sub.add_parser("export")
    _common(export)
    export.add_argument("--execute", action="store_true")
    export.add_argument("--license", default="")
    export.add_argument("--signature", default="")
    export.add_argument("--keyring", default="")
    export.add_argument("--dest", default="")
    export.add_argument("--archive", default="")
    export.set_defaults(func=cmd_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return args.func(args) or 0
    except Rocky2debError as exc:
        print(f"ubuntu-exporter: {exc}", file=sys.stderr)
        return exc.exit_code
