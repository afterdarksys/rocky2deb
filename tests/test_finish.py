"""Runners behind --execute. Fakes stand in for sbuild, mock, ssh, and apt.

Nothing here opens a network connection or writes to the live root.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from rocky2deb.applybundle import apply_bundle
from rocky2deb.buildcmd import deb_argv, rpm_argv, run_build
from rocky2deb.cli import main as rocky_main
from rocky2deb.dscpack import materialize_patches, pack_source
from rocky2deb.errors import Refused
from rocky2deb.fetch import fetch_verified
from rocky2deb.inventory import Inventory, InventoryPackage
from rocky2deb.pipeline import build_closure, stage_dsc
from rocky2deb.publish import write_repo
from rocky2deb.rockify import RockifyPlan
from rocky2deb.rockify_cli import main as rockify_main
from rocky2deb.spec import parse_spec
from rocky2deb.emit_deb import emit_deb_tree
from rocky2deb.srpm import extract_src_rpm, read_src_rpm
from rocky2deb.upgrade import UpgradeRow, apt_commands, apply_suite_upgrade, sources_list_line
from rocky2deb.verify import verify_against

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT / "tests" / "fixtures" / "repo.json"

SPEC = """Name: {name}
Version: 1.0
Release: 1
Summary: demo
License: MIT
%description
demo package
%prep
%autosetup
%build
%configure
echo should-not-run
%install
%files
"""


def _run(main, argv):
    import io as _io
    from contextlib import redirect_stderr, redirect_stdout

    out, err = _io.StringIO(), _io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _rpm_header(count: int, store: int) -> bytes:
    header = b"\x8e\xad\xe8\x01" + b"\x00\x00\x00\x00"
    header += count.to_bytes(4, "big") + store.to_bytes(4, "big")
    header += b"\x00" * (count * 16 + store)
    return header


def _src_rpm(payload: bytes, *, count: int = 0, store: int = 0) -> bytes:
    lead = b"\xed\xab\xee\xdb" + b"\x00" * 92
    header = _rpm_header(count, store)
    pad = (8 - (len(header) % 8)) % 8
    aligned = header + b"\x00" * pad
    return lead + aligned + aligned + payload


def _newc(name: str, data: bytes, mode: int) -> bytes:
    raw_name = name.encode("utf-8") + b"\x00"
    numbers = [0, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(raw_name), 0]
    header = b"070701" + b"".join(f"{number:08x}".encode() for number in numbers)
    if len(header) != 110:
        raise AssertionError(len(header))
    entry = header + raw_name
    entry += b"\x00" * ((4 - (len(entry) % 4)) % 4)
    entry += data
    entry += b"\x00" * ((4 - (len(data) % 4)) % 4)
    return entry


def _cpio(*entries: bytes) -> bytes:
    return b"".join(entries) + _newc("TRAILER!!!", b"", 0)


def _tar_gz(name: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        payload = b"payload\n"
        info = tarfile.TarInfo(f"{name}-1.0/README")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _source_tree(root: Path, name: str) -> None:
    directory = root / name
    directory.mkdir()
    (directory / f"{name}.spec").write_text(SPEC.format(name=name), encoding="utf-8")
    (directory / f"{name}-1.0.tar.gz").write_bytes(_tar_gz(name))


def _deb(control: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        payload = control.encode()
        info = tarfile.TarInfo("control")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    control_tar = buffer.getvalue()

    def member(name: str, payload: bytes) -> bytes:
        header = (
            name.encode().ljust(16)
            + b"0".ljust(12)
            + b"0".ljust(6)
            + b"0".ljust(6)
            + b"100644".ljust(8)
            + str(len(payload)).encode().ljust(10)
            + b"`\n"
        )
        if len(header) != 60:
            raise AssertionError(len(header))
        pad = b"\n" if len(payload) % 2 else b""
        return header + payload + pad

    return b"!<arch>\n" + member("debian-binary", b"2.0\n") + member("control.tar.gz", control_tar)


class SrpmTests(unittest.TestCase):
    def test_padded_header_round_trip_and_gzip(self) -> None:
        spec = b"Name: hello\nVersion: 1\nRelease: 1\nSummary: s\nLicense: MIT\n"
        payload = gzip.compress(_cpio(_newc("hello.spec", spec, 0o100644)))
        region = 16 + 1 * 16 + 1
        pad = (8 - (region % 8)) % 8
        self.assertEqual(region, 33)
        self.assertEqual(pad, 7)
        payload_at = 96 + (region + pad) + (region + pad)
        self.assertEqual(payload_at, 176)
        blob = _src_rpm(payload, count=1, store=1)
        self.assertEqual(blob[payload_at : payload_at + 2], b"\x1f\x8b")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out"
            names = extract_src_rpm(blob, dest)
            self.assertEqual(names, ["hello.spec"])
            self.assertEqual((dest / "hello.spec").read_bytes(), spec)
            self.assertEqual((dest / "hello.spec").stat().st_mode & 0o777, 0o644)

    def test_traversal_symlink_and_caps_write_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "dest"
            outside = root / "outside"
            blob = _src_rpm(_cpio(_newc("../outside", b"nope", 0o100644)))
            with self.assertRaises(Refused) as caught:
                extract_src_rpm(blob, dest)
            self.assertEqual(str(caught.exception), "refusing src.rpm path")
            self.assertFalse(outside.exists())
            link = root / "link"
            link.symlink_to(dest, target_is_directory=True)
            dest.mkdir()
            with self.assertRaises(Refused) as caught:
                extract_src_rpm(_src_rpm(_cpio(_newc("a", b"a", 0o100644))), link)
            self.assertEqual(str(caught.exception), "refusing symlink destination")
            self.assertEqual(list(dest.iterdir()), [])
            linked = _src_rpm(_cpio(_newc("escape", b"target", 0o120777)))
            with self.assertRaises(Refused) as caught:
                extract_src_rpm(linked, dest)
            self.assertEqual(str(caught.exception), "refusing symlink in src.rpm")
            self.assertFalse((dest / "escape").exists())
            big = b"A" * 2000
            capped = _src_rpm(gzip.compress(_cpio(_newc("big", big, 0o100644))))
            self.assertLess(len(capped), len(big))
            with self.assertRaises(Refused) as caught:
                extract_src_rpm(capped, dest, limit=len(capped))
            self.assertEqual(str(caught.exception), "src.rpm exceeds size cap")
            zstd = _src_rpm(b"\x28\xb5\x2f\xfd" + b"\x00" * 8)
            with self.assertRaises(Refused) as caught:
                extract_src_rpm(zstd, dest)
            self.assertIn("zstd", str(caught.exception))
            missing = root / "gone.src.rpm"
            with self.assertRaises(Refused) as caught:
                read_src_rpm(missing, dest)
            self.assertEqual(str(caught.exception), "src.rpm is missing")


class DscTests(unittest.TestCase):
    def test_pack_does_not_execute_rules_and_copies_patches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src"
            _source_tree(source.parent, "hello")
            source = source.parent / "hello"
            text = (source / "hello.spec").read_text(encoding="utf-8")
            package = parse_spec(text, source_id="hello.spec")
            tree = emit_deb_tree(package, "trixie", root / "tree")
            marker = root / "rules-ran"
            rules = tree / "debian" / "rules"
            rules.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
            rules.chmod(0o755)
            dsc = pack_source(tree, source / "hello-1.0.tar.gz", root / "dsc")
            self.assertFalse(marker.exists())
            body = dsc.read_text(encoding="utf-8")
            self.assertIn("Format: 3.0 (quilt)", body)
            self.assertIn("Checksums-Sha256:", body)
            self.assertIn("Files:", body)
            packed = stage_dsc(text, source, "trixie", root / "stage")
            debian_tar = next(packed.parent.glob("*.debian.tar.xz"))
            with tarfile.open(debian_tar, "r:xz") as archive:
                rules_bytes = archive.extractfile("debian/rules").read()
            self.assertIn(b"dh $@", rules_bytes)
            self.assertNotIn(b"%configure", rules_bytes)
            series = tree / "debian" / "patches" / "series"
            series.write_text("fix.patch\n", encoding="utf-8")
            with self.assertRaises(Refused) as caught:
                materialize_patches(tree, source)
            self.assertEqual(str(caught.exception), "missing patch")
            (source / "fix.patch").write_text("patch\n", encoding="utf-8")
            materialize_patches(tree, source)
            self.assertEqual((tree / "debian" / "patches" / "fix.patch").read_text(encoding="utf-8"), "patch\n")


class BuildRunnerTests(unittest.TestCase):
    def test_fake_runner_must_create_a_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dsc = root / "hello_1.0-1.dsc"
            dsc.write_bytes(b"dsc")
            result = root / "result"
            argv = deb_argv("/usr/bin/sbuild", dsc, "trixie", "amd64")
            self.assertEqual(argv[0], "/usr/bin/sbuild")
            self.assertIn("--chroot-mode=unshare", argv)
            (result).mkdir()
            (result / "already.deb").write_bytes(b"old")

            def quiet(argv_in, cwd):
                self.assertEqual(argv_in[0], "/usr/bin/sbuild")
                return 0

            with self.assertRaises(Refused) as caught:
                run_build(
                    "deb",
                    dsc,
                    execute=True,
                    resultdir=result,
                    suite="trixie",
                    which=lambda _name: "/usr/bin/sbuild",
                    runner=quiet,
                )
            self.assertIn("exited 0 but no package was produced", str(caught.exception))
            self.assertTrue((result / "already.deb").is_file())

            def failed(argv_in, cwd):
                return 1

            with self.assertRaises(Refused) as caught:
                run_build(
                    "deb",
                    dsc,
                    execute=True,
                    resultdir=result,
                    suite="trixie",
                    which=lambda _name: "/usr/bin/sbuild",
                    runner=failed,
                )
            self.assertIn("sbuild failed; no package was produced", str(caught.exception))
            srpm = root / "hello.src.rpm"
            srpm.write_bytes(b"srpm")
            rpm = rpm_argv("/usr/bin/mock", srpm, 9, result)
            self.assertEqual(rpm[:4], ["/usr/bin/mock", "--root", "rocky-9-x86_64", "--resultdir"])
            self.assertEqual(rpm[-2:], ["--rebuild", str(srpm)])

            def produced(argv_in, cwd):
                (cwd / "hello_1.0-1_amd64.deb").write_bytes(b"new")
                return 0

            created = run_build(
                "deb",
                dsc,
                execute=True,
                resultdir=result,
                suite="trixie",
                which=lambda _name: "/usr/bin/sbuild",
                runner=produced,
            )
            self.assertEqual([path.name for path in created], ["hello_1.0-1_amd64.deb"])


class FetchRunnerTests(unittest.TestCase):
    def test_opener_stores_only_an_https_body(self) -> None:
        body = b"srpm-bytes"
        digest = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keyring = root / "keys.gpg"
            keyring.write_bytes(b"ring")
            dest = root / "out" / "hello.src.rpm"

            def opener(url, timeout, limit):
                return body, url

            fetch_verified(
                "https://example.com/hello.src.rpm",
                dest,
                expected_sha256=digest,
                signature=b"SIG",
                keyring=keyring,
                opener=opener,
                runner=lambda argv: 0,
            )
            self.assertEqual(dest.read_bytes(), body)
            self.assertFalse(dest.with_name(dest.name + ".partial").exists())
            missed = root / "missed.src.rpm"

            def http_final(url, timeout, limit):
                return body, "http://example.com/hello.src.rpm"

            with self.assertRaises(Refused):
                fetch_verified(
                    "https://example.com/hello.src.rpm",
                    missed,
                    expected_sha256=digest,
                    signature=b"SIG",
                    keyring=keyring,
                    opener=http_final,
                    runner=lambda argv: 0,
                )
            self.assertFalse(missed.exists())
            self.assertFalse(missed.with_name(missed.name + ".partial").exists())


class VerifyApplyPublishTests(unittest.TestCase):
    def test_verify_apply_and_publish(self) -> None:
        inventory = Inventory(
            packages=[InventoryPackage("hello", "1", "1", "", "x86_64", "hello.src.rpm")],
            units=["hello.service"],
        )
        ok = verify_against(
            inventory,
            packages=["hello"],
            units=["hello.service"],
            listeners=["22/tcp"],
            listener_text="LISTEN\t22\ttcp\n",
        )
        self.assertTrue(ok.ok)
        missing = verify_against(inventory, packages=["gone"], units=["gone.service"], listeners=["1/udp"])
        self.assertEqual(missing.lines(), ["missing package gone", "missing unit gone.service", "missing listener 1/udp"])
        with self.assertRaises(Refused) as caught:
            verify_against(inventory, listeners=["1/udp"], listener_text="NOPE\t1\tudp\n")
        self.assertEqual(str(caught.exception), "unknown listener line")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "bundle"
            member = bundle / "etc" / "hello.conf"
            member.parent.mkdir(parents=True)
            member.write_text("pool kept\n", encoding="utf-8")
            (bundle / "manifest").write_text("etc/hello.conf\n", encoding="utf-8")
            target = root / "root"
            target.mkdir()
            os_release = 'ID=debian\nVERSION_CODENAME=bookworm\n'
            first = apply_bundle(target, "bookworm", bundle, os_release)
            second = apply_bundle(target, "bookworm", bundle, os_release)
            self.assertEqual(first, second)
            self.assertEqual((target / "etc" / "hello.conf").read_text(encoding="utf-8"), "pool kept\n")
            with self.assertRaises(Refused) as caught:
                apply_bundle(Path("/"), "bookworm", bundle, os_release)
            self.assertEqual(str(caught.exception), "refusing to write /")
            (bundle / "manifest").write_text("../escaped\n", encoding="utf-8")
            with self.assertRaises(Refused) as caught:
                apply_bundle(target, "bookworm", bundle, os_release)
            self.assertEqual(str(caught.exception), "refusing bundle path")
            self.assertFalse((root / "escaped").exists())
            debs = root / "debs"
            debs.mkdir()
            control = "Package: widget\nVersion: 1.0-1\nArchitecture: amd64\n"
            (debs / "widget.deb").write_bytes(_deb(control))
            release = write_repo(debs, "trixie", root / "repo")
            packages = release.parent / "rocky2deb" / "binary-amd64" / "Packages"
            packaged = packages.read_text(encoding="utf-8")
            pooled = root / "repo" / "pool" / "trixie" / "widget_1.0-1_amd64.deb"
            digest = hashlib.sha256(pooled.read_bytes()).hexdigest()
            self.assertIn(digest, packaged)
            index_digest = hashlib.sha256(packages.read_bytes()).hexdigest()
            self.assertIn(index_digest, release.read_text(encoding="utf-8"))
            (debs / "evil.deb").write_bytes(_deb("Package: ../evil\nVersion: 1\nArchitecture: amd64\n"))
            with self.assertRaises(Refused):
                write_repo(debs, "trixie", root / "repo2")


class SuiteAndClosureTests(unittest.TestCase):
    def test_source_line_apt_argv_and_closure_order(self) -> None:
        stretch = sources_list_line("stretch")
        self.assertIn("[check-valid-until=no]", stretch)
        self.assertIn("https://archive.debian.org/debian", stretch)
        self.assertTrue(stretch.endswith("stretch main\n"))
        trixie = sources_list_line("trixie")
        self.assertEqual(trixie, "deb https://deb.debian.org/debian trixie main\n")
        commands = apt_commands(
            [
                UpgradeRow("widget", "rebuild", "widget", "still absent"),
                UpgradeRow("bash", "keep", "bash", "base package"),
            ]
        )
        self.assertEqual(
            commands,
            [
                ["apt-get", "update"],
                ["apt-get", "install", "-y", "--", "widget"],
                ["apt-get", "full-upgrade", "-y"],
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "host"
            root.mkdir()
            seen: list[list[str]] = []

            def runner(argv):
                seen.append(argv)
                return 0

            apply_suite_upgrade(
                root,
                "trixie",
                [UpgradeRow("widget", "rebuild", "widget", "still absent")],
                runner=runner,
            )
            line = (root / "etc" / "apt" / "sources.list.d" / "trixie.list").read_text(encoding="utf-8")
            self.assertEqual(line, "deb https://deb.debian.org/debian trixie main\n")
            self.assertEqual(seen[0][0], "apt-get")
            self.assertIn(f"Dir={root.resolve()}", seen[0])
            sources = root.parent / "sources"
            sources.mkdir()
            _source_tree(sources, "alpha")
            _source_tree(sources, "beta")
            order: list[str] = []

            def build(argv, cwd):
                order.append(Path(argv[-1]).name.split("_", 1)[0])
                (cwd / f"{order[-1]}.deb").write_bytes(b"deb")
                return 0

            plan = RockifyPlan(
                name="beta",
                suite="trixie",
                rocky_major=9,
                report="runs",
                order=["alpha", "beta"],
            )
            produced = build_closure(
                plan,
                sources,
                root.parent / "out",
                execute=True,
                which=lambda _name: "/usr/bin/sbuild",
                runner=build,
            )
            self.assertEqual(order, ["alpha", "beta"])
            self.assertEqual([path.name for path in produced], ["alpha.deb", "beta.deb"])
            called = {"n": 0}

            def boom(argv, cwd):
                called["n"] += 1
                return 0

            blocked = RockifyPlan(name="glibc", suite="trixie", rocky_major=9, report="blocked", reason="blocked root")
            with self.assertRaises(Refused) as caught:
                build_closure(blocked, sources, root.parent / "nope", execute=True, runner=boom)
            self.assertEqual(str(caught.exception), "blocked root")
            self.assertEqual(called["n"], 0)


class FinishCliTests(unittest.TestCase):
    def test_new_refusals_stay_closed(self) -> None:
        code, _out, err = _run(rocky_main, ["verify"])
        self.assertEqual(code, 3)
        self.assertIn("verify needs --inventory or --host", err)
        code, _out, err = _run(rocky_main, ["verify", "--host", "web-1.example.com"])
        self.assertEqual(code, 3)
        self.assertIn("refusing to verify a host without --execute", err)
        code, _out, err = _run(rocky_main, ["inventory-collect", "web-1.example.com"])
        self.assertEqual(code, 3)
        self.assertIn("refusing to collect inventory without --execute", err)
        code, out, err = _run(rocky_main, ["rebuild", "bash", "--license", "GPL-3.0-or-later"])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("base\t"))
        code, out, err = _run(rocky_main, ["rebuild", "widget", "--license", "MIT"])
        self.assertEqual(code, 3)
        self.assertIn("rebuild", out)
        self.assertIn("without --execute", err)
        code, _out, err = _run(
            rocky_main,
            ["fetch", "https://example.com/a.src.rpm", "--execute"],
        )
        self.assertEqual(code, 3)
        self.assertIn("fetch needs --sha256", err)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os_release = root / "os-release"
            os_release.write_text('ID=debian\nVERSION_CODENAME="bookworm"\n', encoding="utf-8")
            decisions = root / "decisions.json"
            decisions.write_text("[]", encoding="utf-8")
            code, out, err = _run(
                rocky_main,
                [
                    "apply",
                    "--suite",
                    "bookworm",
                    "--os-release",
                    str(os_release),
                ],
            )
            self.assertEqual(code, 0, err)
            self.assertEqual(out.strip(), "ok")
            code, _out, err = _run(
                rocky_main,
                [
                    "apply",
                    "--suite",
                    "bookworm",
                    "--os-release",
                    str(os_release),
                    "--execute",
                ],
            )
            self.assertEqual(code, 3)
            self.assertIn("apply needs --root and --bundle", err)
            code, _out, err = _run(
                rocky_main,
                [
                    "suite-upgrade",
                    "--from",
                    "bookworm",
                    "--to",
                    "trixie",
                    "--os-release",
                    str(os_release),
                    "--decisions",
                    str(decisions),
                    "--execute",
                ],
            )
            self.assertEqual(code, 3)
            self.assertIn("refusing suite-upgrade without --root", err)
            code, _out, err = _run(
                rocky_main,
                [
                    "suite-upgrade",
                    "--from",
                    "bookworm",
                    "--to",
                    "trixie",
                    "--os-release",
                    str(os_release),
                    "--decisions",
                    str(decisions),
                    "--execute",
                    "--root",
                    "/",
                ],
            )
            self.assertEqual(code, 3)
            self.assertIn("refusing suite-upgrade onto /", err)
        code, _out, err = _run(
            rockify_main,
            ["build", "widget", "--rocky", "9", "--suite", "trixie", "--repo", str(REPO), "--index", "gcc=12.2.0-1"],
        )
        self.assertEqual(code, 3)
        self.assertIn("without --execute", err)
        code, _out, err = _run(
            rockify_main,
            ["build", "glibc", "--rocky", "9", "--suite", "trixie", "--repo", str(REPO)],
        )
        self.assertEqual(code, 3)
        self.assertIn("Debian installer", err)
        self.assertNotIn("without --execute", err)


if __name__ == "__main__":
    unittest.main()
