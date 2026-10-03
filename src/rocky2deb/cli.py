"""rocky2deb command line.

Build, fetch, apply, and suite-upgrade run only with --execute. A zero exit
still requires the artifact the command claims to have produced. / is refused
unless --allow-live-root is set.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from rocky2deb.applybundle import apply_bundle
from rocky2deb.applycheck import assert_debian_suite
from rocky2deb.buildcmd import assert_build_backend, run_build
from rocky2deb.emit_deb import emit_deb_tree
from rocky2deb.emit_rpm import emit_spec
from rocky2deb.errors import Refused, Rocky2debError
from rocky2deb.fetch import assert_https, fetch_verified
from rocky2deb.gates import assign_waves, next_wave_gate, pre_apply_gate
from rocky2deb.inventory import Inventory, collect_inventory, parse_inventory, ssh_argv
from rocky2deb.ir import ir_from_json, ir_to_json
from rocky2deb.license import load_ledger
from rocky2deb.names import load_name_map
from rocky2deb.pipeline import stage_dsc
from rocky2deb.profiles import REQUESTED_SUITES, get_profile
from rocky2deb.project import init_project, load_project
from rocky2deb.publish import write_repo
from rocky2deb.remap import format_remap_report, load_maps, remap_text
from rocky2deb.resolve import Decision, resolve_one
from rocky2deb.spec import parse_spec
from rocky2deb.srpm import read_src_rpm
from rocky2deb.upgrade import apply_suite_upgrade, retarget, suite_upgrade_plan
from rocky2deb.verify import verify_against

_DECISION_FIELDS = ("rpm", "action", "deb", "reason", "license_class")


def _index_from_args(pairs: list[str] | None, index_file: Path | None) -> dict[str, str]:
    index: dict[str, str] = {}
    if index_file is not None:
        data = json.loads(Path(index_file).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise Refused("index file must be a JSON object")
        index.update({str(key): str(value) for key, value in data.items()})
    for item in pairs or []:
        if "=" not in item:
            raise Refused("index entry must be name=version")
        name, version = item.split("=", 1)
        index[name] = version
    return index


def _load_decisions(path: Path) -> list[Decision]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise Refused("decisions file must be a list")
    rows: list[Decision] = []
    for item in data:
        if not isinstance(item, dict) or "rpm" not in item or "action" not in item:
            raise Refused("decision needs rpm and action")
        rows.append(Decision(**{key: str(item.get(key, "") or "") for key in _DECISION_FIELDS}))
    return rows


def _resolve_name(args: argparse.Namespace) -> Decision:
    pins = set(args.pins or [])
    if args.pin:
        pins.add(args.name)
    floors = {}
    if getattr(args, "floor", ""):
        floors[args.name] = args.floor
    return resolve_one(
        args.name,
        index=_index_from_args(args.index, args.index_file),
        names=load_name_map(),
        pins=pins,
        ledger=load_ledger(),
        license_text=args.license or "",
        floors=floors,
    )


def cmd_profiles(_args: argparse.Namespace) -> int:
    for name in (*REQUESTED_SUITES, "forky"):
        profile = get_profile(name)
        print(f"{profile.name} {profile.debian_version} {profile.status} glibc {profile.glibc}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    path = init_project(Path(args.dest), args.suite, args.rocky)
    print(path)
    return 0


def cmd_import_spec(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec)
    if spec_path.name.endswith(".src.rpm"):
        with tempfile.TemporaryDirectory(prefix="rocky2deb-srpm-") as tmp:
            spec = read_src_rpm(spec_path, Path(tmp))
            text = spec.read_text(encoding="utf-8")
    else:
        text = spec_path.read_text(encoding="utf-8")
    package = parse_spec(text, source_id=str(spec_path))
    Path(args.output).write_text(ir_to_json(package), encoding="utf-8")
    print(args.output)
    return 0


def cmd_emit_deb(args: argparse.Namespace) -> int:
    package = ir_from_json(Path(args.ir).read_text(encoding="utf-8"))
    root = emit_deb_tree(package, args.suite, Path(args.output))
    print(root)
    return 0


def cmd_emit_rpm(args: argparse.Namespace) -> int:
    package = ir_from_json(Path(args.ir).read_text(encoding="utf-8"))
    sys.stdout.write(emit_spec(package))
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    decision = _resolve_name(args)
    print(f"{decision.action}\t{decision.deb}\t{decision.reason}")
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    decision = _resolve_name(args)
    print(f"{decision.action}\t{decision.deb}\t{decision.reason}")
    if decision.action in {"debian", "base"}:
        return 0
    if decision.action == "gap":
        raise Refused(decision.reason or "gap")
    if decision.action not in {"rebuild", "pin-rebuild"}:
        raise Refused(decision.reason or decision.action)
    if not args.execute:
        raise Refused("refusing to build without --execute")
    if not args.srpm:
        raise Refused("rebuild needs --srpm")
    if not args.suite:
        raise Refused("rebuild needs --suite")
    srpm = Path(args.srpm)
    out = Path(args.output) if args.output else srpm.parent / "artifacts"
    with tempfile.TemporaryDirectory(prefix="rocky2deb-rebuild-") as tmp:
        work = Path(tmp)
        spec = read_src_rpm(srpm, work / "extracted")
        dsc = stage_dsc(spec.read_text(encoding="utf-8"), work / "extracted", args.suite, work)
        produced = run_build(
            "deb",
            dsc,
            execute=True,
            resultdir=out,
            suite=args.suite,
            arch=args.arch,
        )
    for path in produced:
        print(path)
    return 0


def cmd_remap(args: argparse.Namespace) -> int:
    maps = load_maps()
    if args.map not in maps:
        raise Refused(f"unknown config map {args.map}")
    text = Path(args.file).read_text(encoding="utf-8")
    result = remap_text(text, maps[args.map])
    sys.stdout.write(format_remap_report(result))
    if args.write_body:
        Path(args.write_body).write_text(result.body, encoding="utf-8")
    return 0


def cmd_apply_check(args: argparse.Namespace) -> int:
    text = Path(args.os_release).read_text(encoding="utf-8")
    assert_debian_suite(text, args.suite)
    print("ok")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    text = Path(args.os_release).read_text(encoding="utf-8")
    if not args.execute:
        assert_debian_suite(text, args.suite)
        print("ok")
        return 0
    if not args.root or not args.bundle:
        raise Refused("apply needs --root and --bundle")
    written = apply_bundle(
        Path(args.root),
        args.suite,
        Path(args.bundle),
        text,
        allow_live=args.allow_live_root,
    )
    print("executed")
    for rel in written:
        print(rel)
    return 0


def _print_upgrade_rows(rows) -> None:
    for row in rows:
        print(f"{row.name}\t{row.action}\t{row.deb}\t{row.reason}")


def cmd_suite_upgrade(args: argparse.Namespace) -> int:
    rows = suite_upgrade_plan(
        Path(args.os_release).read_text(encoding="utf-8"),
        args.from_suite,
        args.to_suite,
        _load_decisions(args.decisions),
        _index_from_args(args.index, args.index_file),
    )
    if not args.execute:
        print("plan-only")
        _print_upgrade_rows(rows)
        return 0
    if not args.root:
        raise Refused("refusing suite-upgrade without --root")
    root = Path(args.root)
    if root.is_symlink():
        raise Refused("refusing symlink root")
    if root.resolve() == Path("/").resolve() and not args.allow_live_root:
        raise Refused("refusing suite-upgrade onto /")
    apply_suite_upgrade(root, args.to_suite, rows, allow_live=args.allow_live_root)
    print("executed")
    _print_upgrade_rows(rows)
    return 0


def cmd_retarget(args: argparse.Namespace) -> int:
    suite, rows = retarget(
        args.to_suite,
        _load_decisions(args.decisions),
        _index_from_args(args.index, args.index_file),
    )
    if args.project:
        project = load_project(Path(args.project) / "rocky2deb.toml")
        pins = ", ".join(json.dumps(pin) for pin in project.pins)
        (Path(args.project) / "rocky2deb.toml").write_text(
            f'target_suite = "{suite}"\nrocky_major = {project.rocky_major}\npins = [{pins}]\n',
            encoding="utf-8",
        )
    payload = [
        {"name": row.name, "action": row.action, "deb": row.deb, "reason": row.reason}
        for row in rows
    ]
    if args.write:
        Path(args.write).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"retarget {suite}")
    print("plan-only")
    for row in rows:
        print(f"{row.name}\t{row.action}\t{row.deb}\t{row.reason}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    assert_build_backend(args.kind, args.execute)
    if not args.source or not args.suite:
        raise Refused("build needs --source and --suite")
    if args.kind == "rpm" and args.rocky not in (8, 9, 10):
        raise Refused("rpm build needs --rocky")
    result = Path(args.result) if args.result else Path(args.source).resolve().parent
    produced = run_build(
        args.kind,
        Path(args.source),
        execute=True,
        resultdir=result,
        suite=args.suite,
        arch=args.arch,
        rocky_major=args.rocky,
    )
    for path in produced:
        print(path)
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    assert_https(args.url)
    if not args.execute:
        raise Refused("refusing to fetch without --execute")
    if not args.sha256:
        raise Refused("fetch needs --sha256")
    if not args.signature:
        raise Refused("fetch needs --signature")
    if not args.keyring:
        raise Refused("fetch needs --keyring")
    if not args.output:
        raise Refused("fetch needs --output")
    signature = Path(args.signature)
    keyring = Path(args.keyring)
    if not signature.is_file() or signature.is_symlink():
        raise Refused("missing signature")
    if not keyring.is_file() or keyring.is_symlink():
        raise Refused("missing keyring")
    fetch_verified(
        args.url,
        Path(args.output),
        expected_sha256=args.sha256,
        signature=signature.read_bytes(),
        keyring=keyring,
    )
    print(args.output)
    return 0


def cmd_gates(args: argparse.Namespace) -> int:
    if args.hosts:
        hosts = [item for item in args.hosts.split(",") if item]
        canaries = [item for item in (args.canaries or "").split(",") if item]
        waves = assign_waves(hosts, canaries)
        print("canary: " + ",".join(waves["canary"]))
        print("rest: " + ",".join(waves["rest"]))
    if args.decisions:
        gate = pre_apply_gate(_load_decisions(args.decisions), args.unmapped or [])
        print(("ok" if gate.ok else "blocked") + " " + gate.name)
        for reason in gate.reasons:
            print(reason)
        if args.checked:
            nxt = next_wave_gate(gate.ok, args.canary_verified)
            print(("ok" if nxt.ok else "blocked") + " " + nxt.name)
            for reason in nxt.reasons:
                print(reason)
    return 0


def _print_inventory(inventory: Inventory) -> None:
    for package in inventory.packages:
        print(f"pkg {package.name} {package.version}-{package.release}")
    for unit in inventory.units:
        print(f"unit {unit}")


def cmd_inventory_parse(args: argparse.Namespace) -> int:
    _print_inventory(parse_inventory(Path(args.path).read_text(encoding="utf-8")))
    return 0


def cmd_inventory_collect(args: argparse.Namespace) -> int:
    _print_inventory(collect_inventory(args.host, execute=args.execute))
    return 0


def cmd_ssh_argv(args: argparse.Namespace) -> int:
    for part in ssh_argv(args.host):
        print(part)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    if not args.inventory and not args.host:
        raise Refused("verify needs --inventory or --host")
    if args.host:
        if not args.execute:
            raise Refused("refusing to verify a host without --execute")
        inventory = collect_inventory(args.host, execute=True)
        listener_text = ""
    else:
        inventory = parse_inventory(Path(args.inventory).read_text(encoding="utf-8"))
        listener_text = Path(args.listeners).read_text(encoding="utf-8") if args.listeners else ""
    result = verify_against(
        inventory,
        packages=args.package,
        units=args.unit,
        listeners=args.listener,
        listener_text=listener_text,
    )
    if result.ok:
        print("ok")
        return 0
    for line in result.lines():
        print(line)
    return 3


def cmd_publish(args: argparse.Namespace) -> int:
    print(write_repo(Path(args.debs), args.suite, Path(args.dest)))
    return 0


def _add_resolve_args(parser: argparse.ArgumentParser, *, floor: bool = False) -> None:
    parser.add_argument("--license", default="")
    parser.add_argument("--index", action="append", default=[])
    parser.add_argument("--index-file", type=Path)
    parser.add_argument("--pin", action="store_true")
    parser.add_argument("--pins", action="append", default=[])
    if floor:
        parser.add_argument("--floor", default="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rocky2deb")
    sub = parser.add_subparsers(dest="command", required=True)

    profiles = sub.add_parser("profiles")
    profiles.set_defaults(func=cmd_profiles)

    init = sub.add_parser("init")
    init.add_argument("dest")
    init.add_argument("--suite", required=True)
    init.add_argument("--rocky", required=True, type=int)
    init.set_defaults(func=cmd_init)

    import_spec = sub.add_parser("import-spec")
    import_spec.add_argument("spec")
    import_spec.add_argument("-o", "--output", required=True)
    import_spec.set_defaults(func=cmd_import_spec)

    emit_deb = sub.add_parser("emit-deb")
    emit_deb.add_argument("ir")
    emit_deb.add_argument("--suite", required=True)
    emit_deb.add_argument("-o", "--output", required=True)
    emit_deb.set_defaults(func=cmd_emit_deb)

    emit_rpm = sub.add_parser("emit-rpm")
    emit_rpm.add_argument("ir")
    emit_rpm.set_defaults(func=cmd_emit_rpm)

    resolve = sub.add_parser("resolve")
    resolve.add_argument("name")
    _add_resolve_args(resolve, floor=True)
    resolve.set_defaults(func=cmd_resolve)

    rebuild = sub.add_parser("rebuild")
    rebuild.add_argument("name")
    rebuild.add_argument("--execute", action="store_true")
    rebuild.add_argument("--srpm", default="")
    rebuild.add_argument("--suite", default="")
    rebuild.add_argument("--output", default="")
    rebuild.add_argument("--arch", default="amd64")
    _add_resolve_args(rebuild, floor=True)
    rebuild.set_defaults(func=cmd_rebuild)

    remap = sub.add_parser("remap")
    remap.add_argument("--map", required=True)
    remap.add_argument("--file", required=True)
    remap.add_argument("--write-body")
    remap.set_defaults(func=cmd_remap)

    apply_check = sub.add_parser("apply-check")
    apply_check.add_argument("--suite", required=True)
    apply_check.add_argument("--os-release", required=True)
    apply_check.set_defaults(func=cmd_apply_check)

    apply = sub.add_parser("apply")
    apply.add_argument("--suite", required=True)
    apply.add_argument("--os-release", required=True)
    apply.add_argument("--execute", action="store_true")
    apply.add_argument("--root", default="")
    apply.add_argument("--bundle", default="")
    apply.add_argument("--allow-live-root", action="store_true")
    apply.set_defaults(func=cmd_apply)

    upgrade = sub.add_parser("suite-upgrade")
    upgrade.add_argument("--from", dest="from_suite", required=True)
    upgrade.add_argument("--to", dest="to_suite", required=True)
    upgrade.add_argument("--os-release", required=True)
    upgrade.add_argument("--decisions", required=True, type=Path)
    upgrade.add_argument("--index", action="append", default=[])
    upgrade.add_argument("--index-file", type=Path)
    upgrade.add_argument("--execute", action="store_true")
    upgrade.add_argument("--root", default="")
    upgrade.add_argument("--allow-live-root", action="store_true")
    upgrade.set_defaults(func=cmd_suite_upgrade)

    retarget_cmd = sub.add_parser("retarget")
    retarget_cmd.add_argument("--to", dest="to_suite", required=True)
    retarget_cmd.add_argument("--decisions", required=True, type=Path)
    retarget_cmd.add_argument("--index", action="append", default=[])
    retarget_cmd.add_argument("--index-file", type=Path)
    retarget_cmd.add_argument("--project")
    retarget_cmd.add_argument("--write")
    retarget_cmd.set_defaults(func=cmd_retarget)

    build = sub.add_parser("build")
    build.add_argument("kind", choices=["deb", "rpm"])
    build.add_argument("--execute", action="store_true")
    build.add_argument("--source", default="")
    build.add_argument("--suite", default="")
    build.add_argument("--result", default="")
    build.add_argument("--arch", default="amd64")
    build.add_argument("--rocky", type=int, default=0)
    build.set_defaults(func=cmd_build)

    fetch = sub.add_parser("fetch")
    fetch.add_argument("url")
    fetch.add_argument("--execute", action="store_true")
    fetch.add_argument("--sha256", default="")
    fetch.add_argument("--signature", default="")
    fetch.add_argument("--keyring", default="")
    fetch.add_argument("--output", default="")
    fetch.set_defaults(func=cmd_fetch)

    gates = sub.add_parser("gates")
    gates.add_argument("--hosts", default="")
    gates.add_argument("--canaries", default="")
    gates.add_argument("--decisions", type=Path)
    gates.add_argument("--unmapped", action="append", default=[])
    gates.add_argument("--checked", action="store_true")
    gates.add_argument("--canary-verified", action="store_true")
    gates.set_defaults(func=cmd_gates)

    inventory = sub.add_parser("inventory-parse")
    inventory.add_argument("path")
    inventory.set_defaults(func=cmd_inventory_parse)

    collect = sub.add_parser("inventory-collect")
    collect.add_argument("host")
    collect.add_argument("--execute", action="store_true")
    collect.set_defaults(func=cmd_inventory_collect)

    ssh = sub.add_parser("ssh-argv")
    ssh.add_argument("host")
    ssh.set_defaults(func=cmd_ssh_argv)

    verify = sub.add_parser("verify")
    verify.add_argument("--inventory", default="")
    verify.add_argument("--host", default="")
    verify.add_argument("--execute", action="store_true")
    verify.add_argument("--package", action="append", default=[])
    verify.add_argument("--unit", action="append", default=[])
    verify.add_argument("--listener", action="append", default=[])
    verify.add_argument("--listeners", default="")
    verify.set_defaults(func=cmd_verify)

    publish = sub.add_parser("publish")
    publish.add_argument("--debs", required=True)
    publish.add_argument("--suite", required=True)
    publish.add_argument("--dest", required=True)
    publish.set_defaults(func=cmd_publish)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return args.func(args) or 0
    except Rocky2debError as exc:
        print(f"rocky2deb: {exc}", file=sys.stderr)
        return exc.exit_code
