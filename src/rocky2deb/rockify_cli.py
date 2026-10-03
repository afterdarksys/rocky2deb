"""rockify command line. Plans a closure and, with --execute, builds it."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rocky2deb.errors import Refused, Rocky2debError
from rocky2deb.pipeline import build_closure
from rocky2deb.primary import load_repo
from rocky2deb.rockify import binary_glibc_status, emit_compat_source, plan_closure


def _index(pairs: list[str] | None) -> dict[str, str]:
    index: dict[str, str] = {}
    for item in pairs or []:
        if "=" not in item:
            raise Rocky2debError("index entry must be name=version")
        name, version = item.split("=", 1)
        index[name] = version
    return index


def _plan(args: argparse.Namespace):
    repo_path = Path(args.repo)
    if not repo_path.is_file():
        raise Rocky2debError("missing repo file")
    packages = load_repo(repo_path.read_text(encoding="utf-8"), filename=repo_path.name)
    compiler_log = ""
    if args.compiler_log:
        compiler_log = Path(args.compiler_log).read_text(encoding="utf-8")
    return plan_closure(
        args.name,
        packages,
        suite=args.suite,
        rocky_major=args.rocky,
        index=_index(args.index),
        binary_symbols=args.symbol,
        compiler_log=compiler_log,
        weak=args.weak,
    )


def _print_plan(plan) -> None:
    print(f"report: {plan.report}")
    print(f"reason: {plan.reason}")
    print(f"binary: {plan.binary}")
    print(f"binary_reason: {plan.binary_reason}")
    print(f"unbuildable: {'yes' if plan.unbuildable else 'no'}")
    print("order: " + " ".join(plan.order))
    for decision in plan.decisions:
        print(
            f"decision: {decision.rpm} {decision.action} {decision.deb} {decision.reason}"
        )
    for shim in plan.shims:
        print(f"shim: {shim.kind} {shim.source} {shim.dest}")


def _add_plan_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name")
    parser.add_argument("--rocky", required=True, type=int)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--index", action="append", default=[])
    parser.add_argument("--symbol", action="append")
    parser.add_argument("--compiler-log")
    parser.add_argument("--weak", action="store_true")


def cmd_plan(args: argparse.Namespace) -> int:
    _print_plan(_plan(args))
    return 0


def cmd_compat(args: argparse.Namespace) -> int:
    plan = _plan(args)
    root = emit_compat_source(plan, Path(args.dest))
    if root is None:
        print("no compat package")
        return 0
    print(root)
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    plan = _plan(args)
    if plan.report == "blocked":
        raise Refused(plan.reason or "blocked")
    if not args.execute:
        raise Refused("refusing to build without --execute")
    if not args.sources or not args.output:
        raise Refused("build needs --sources and --output")
    produced = build_closure(
        plan,
        Path(args.sources),
        Path(args.output),
        execute=True,
    )
    for path in produced:
        print(path)
    return 0


def cmd_binary_check(args: argparse.Namespace) -> int:
    status, reason = binary_glibc_status(args.symbol or [], args.suite)
    print(status)
    if reason:
        print(reason)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rockify")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan")
    _add_plan_args(plan)
    plan.set_defaults(func=cmd_plan)

    compat = sub.add_parser("compat")
    _add_plan_args(compat)
    compat.add_argument("--dest", required=True)
    compat.set_defaults(func=cmd_compat)

    build = sub.add_parser("build")
    _add_plan_args(build)
    build.add_argument("--execute", action="store_true")
    build.add_argument("--sources", default="")
    build.add_argument("--output", default="")
    build.set_defaults(func=cmd_build)

    binary = sub.add_parser("binary-check")
    binary.add_argument("--suite", required=True)
    binary.add_argument("--symbol", action="append", default=[])
    binary.set_defaults(func=cmd_binary_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return args.func(args) or 0
    except Rocky2debError as exc:
        print(f"rockify: {exc}", file=sys.stderr)
        return exc.exit_code
