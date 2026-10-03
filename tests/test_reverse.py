"""debian2rocky and ubuntu-exporter. No network, ssh, sbuild, or mock."""

from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from rocky2deb.applybundle import apply_rocky_bundle
from rocky2deb.applycheck import assert_rocky_release
from rocky2deb.d2r import resolve_deb, spec_from_tree
from rocky2deb.debinventory import collect_deb_inventory, parse_deb_inventory
from rocky2deb.debian2rocky_cli import main as debian2rocky_main
from rocky2deb.errors import Refused, Rocky2debError, SpecUnemittable
from rocky2deb.fetch import DEFAULT_LIMIT
from rocky2deb.names import load_name_map
from rocky2deb.remap import (
    format_remap_report,
    load_maps,
    remap_text_reverse,
    translate_config_path_reverse,
)
from rocky2deb.ubuntu_cli import main as ubuntu_main
from rocky2deb.ubuntu_src import (
    export_ubuntu,
    parse_sources,
    retarget_version,
    ubuntu_release,
)

ROOT = Path(__file__).resolve().parents[1]


def run_d2r(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = debian2rocky_main(argv)
    return code, out.getvalue(), err.getvalue()


def run_ubuntu(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ubuntu_main(argv)
    return code, out.getvalue(), err.getvalue()


def _control(name: str = "hello") -> str:
    return (
        f"Source: {name}\nSection: devel\n\n"
        f"Package: {name}\nArchitecture: any\nDescription: hi\n long\n"
    )


def _changelog(name: str, version: str, older: str = "") -> str:
    text = (
        f"{name} ({version}) unstable; urgency=low\n\n"
        "  * test\n\n"
        " -- rocky2deb <rocky2deb@localhost>  Sat, 03 Oct 2026 00:00:00 +0000\n"
    )
    if older:
        text += (
            f"\n{name} ({older}) unstable; urgency=low\n\n"
            "  * older\n\n"
            " -- rocky2deb <rocky2deb@localhost>  Sat, 03 Oct 2026 00:00:00 +0000\n"
        )
    return text


def _rules(kind: str = "autotools") -> str:
    if kind == "dh":
        body = "\tdh $@\n"
    else:
        body = "\tdh $@ --buildsystem=autotools\n\ttouch SHOULD_NOT_EXIST\n"
    return "#!/usr/bin/make -f\n%:\n" + body


def _tar_xz(members: list[tuple[str, bytes, int]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as archive:
        for name, data, mode in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            info.uid = 0
            info.gid = 0
            info.type = tarfile.REGTYPE
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _debian_tar(name: str, version: str, kind: str = "autotools", older: str = "") -> bytes:
    return _tar_xz(
        [
            (f"debian/control", _control(name).encode(), 0o644),
            (f"debian/changelog", _changelog(name, version, older).encode(), 0o644),
            (f"debian/rules", _rules(kind).encode(), 0o755),
        ]
    )


def _sources(
    files: list[tuple[str, bytes]],
    *,
    package: str = "hello",
    version: str = "2.10-3ubuntu1",
    binary: str = "hello",
    directory: str = "pool/main/h/hello",
    bad_md5: bool = False,
) -> bytes:
    sha_rows: list[str] = []
    md5_rows: list[str] = []
    for filename, data in files:
        digest = hashlib.sha256(data).hexdigest()
        md5 = "0" * 32 if bad_md5 else hashlib.md5(data).hexdigest()
        sha_rows.append(f" {digest} {len(data)} {filename}")
        md5_rows.append(f" {md5} {len(data)} {filename}")
    text = (
        f"Package: {package}\nBinary: {binary}\nVersion: {version}\n"
        f"Directory: {directory}\nChecksums-Sha256:\n"
        + "\n".join(sha_rows)
        + "\nFiles:\n"
        + "\n".join(md5_rows)
        + "\n"
    )
    return text.encode()


def _tree(root: Path, name: str, version: str, kind: str) -> Path:
    debian = root / "debian"
    debian.mkdir(parents=True)
    (debian / "control").write_text(_control(name), encoding="utf-8")
    (debian / "changelog").write_text(_changelog(name, version), encoding="utf-8")
    rules = debian / "rules"
    rules.write_text(_rules(kind), encoding="utf-8")
    rules.chmod(0o755)
    return root


class ReverseResolveTests(unittest.TestCase):
    def test_base_wins_over_pin_and_index(self) -> None:
        decision = resolve_deb(
            "libc6",
            index={"libc6": "1", "glibc": "1"},
            names={},
            pins={"libc6"},
            license_text="MIT",
        )
        self.assertEqual(decision.action, "base")
        self.assertEqual(decision.rpm, "glibc")
        self.assertEqual(decision.deb, "libc6")
        self.assertEqual(decision.reason, "satisfied by the Rocky installer")
        self.assertEqual(decision.license_class, "")

    def test_base_companions_and_package_managers(self) -> None:
        cases = {
            "apt": "dnf",
            "dpkg": "rpm",
            "libc6-dev": "glibc",
            "linux-image-amd64": "kernel",
            "glibc": "glibc",
        }
        for deb, rpm in cases.items():
            decision = resolve_deb(deb, index={deb: "1", rpm: "1"}, names={}, pins={deb})
            self.assertEqual(decision.action, "base", deb)
            self.assertEqual(decision.rpm, rpm, deb)

    def test_aptitude_is_not_base(self) -> None:
        decision = resolve_deb("aptitude", index={}, names={}, license_text="MIT")
        self.assertEqual(decision.action, "rebuild")
        self.assertEqual(decision.rpm, "aptitude")
        self.assertEqual(decision.reason, "free license token")

    def test_trademark_gap_even_when_pinned(self) -> None:
        decision = resolve_deb(
            "rocky-logos",
            index={"rocky-logos": "1"},
            names={},
            pins={"rocky-logos"},
            license_text="MIT",
        )
        self.assertEqual(decision.action, "gap")
        self.assertEqual(decision.reason, "trademark")
        self.assertEqual(decision.license_class, "trademark")
        ubuntu = resolve_deb("ubuntu-mono", index={}, names={}, license_text="MIT")
        self.assertEqual(ubuntu.action, "gap")
        self.assertEqual(ubuntu.reason, "trademark")

    def test_pin_stays_rebuild_when_indexed(self) -> None:
        decision = resolve_deb(
            "widget",
            index={"widget": "1"},
            names={},
            pins={"widget"},
            license_text="MIT",
        )
        self.assertEqual(decision.action, "pin-rebuild")
        self.assertEqual(decision.rpm, "widget")
        self.assertEqual(decision.license_class, "dfsg")

    def test_absent_free_package_rebuilds_and_unknown_is_gap(self) -> None:
        rebuild = resolve_deb("widget", index={}, names={}, license_text="MIT")
        self.assertEqual(rebuild.action, "rebuild")
        self.assertEqual(rebuild.rpm, "widget")
        self.assertEqual(rebuild.license_class, "dfsg")
        self.assertEqual(rebuild.reason, "free license token")
        gap = resolve_deb("widget", index={}, names={}, license_text="")
        self.assertEqual(gap.action, "gap")
        self.assertEqual(gap.reason, "license unknown")
        mixed = resolve_deb("widget", index={}, names={}, license_text="MIT and Proprietary")
        self.assertEqual(mixed.reason, "license unknown")

    def test_curated_map_uses_the_rocky_index(self) -> None:
        names = load_name_map()
        nginx = resolve_deb("nginx", index={"nginx": "1"}, names=names)
        self.assertEqual(nginx.action, "rocky")
        self.assertEqual(nginx.rpm, "nginx")
        self.assertEqual(nginx.reason, "curated map")
        self.assertEqual(nginx.license_class, "")
        apache = resolve_deb("apache2", index={"httpd": "2"}, names=names)
        self.assertEqual(apache.action, "rocky")
        self.assertEqual(apache.rpm, "httpd")
        self.assertEqual(apache.reason, "curated map")


class ReverseRemapTests(unittest.TestCase):
    def test_chrony_and_sftp_reverse_are_idempotent(self) -> None:
        maps = load_maps()
        chrony = translate_config_path_reverse("/etc/chrony/chrony.conf", maps["chrony"])
        self.assertEqual(chrony, "/etc/chrony.conf")
        self.assertEqual(translate_config_path_reverse(chrony, maps["chrony"]), chrony)
        text = "Subsystem sftp /usr/lib/openssh/sftp-server\n"
        once = remap_text_reverse(text, maps["sshd"])
        self.assertIn("/usr/libexec/openssh/sftp-server", once.body)
        twice = remap_text_reverse(once.body, maps["sshd"])
        self.assertEqual(twice.body, once.body)
        self.assertEqual(once.debian_path, maps["sshd"].rocky)

    def test_report_prints_keys_and_path(self) -> None:
        maps = load_maps()
        text = "pool pool.ntp.org iburst\nnotakey secret-value\n"
        result = remap_text_reverse(text, maps["chrony"])
        report = format_remap_report(result)
        self.assertIn("map chrony -> /etc/chrony.conf", report)
        self.assertIn("unmapped notakey", report)
        self.assertNotIn("secret-value", report)
        self.assertNotIn("pool.ntp.org", report)
        self.assertIn("secret-value", result.body)


class DebInventoryTests(unittest.TestCase):
    def test_parse_pkg_and_reject_rocky_line(self) -> None:
        text = (
            "PKG\thello\t2.10-1\tall\thello (2.10-1)\n"
            "PKG\tepoch\t1:2.0-1\tamd64\tepoch\n"
            "PKG\tzero\t0:1.0\tall\t\n"
            "PKG\tnone\t(none):1.2-3\tall\tnone\n"
            "UNIT\tcron.service\tenabled\n"
        )
        inventory = parse_deb_inventory(text)
        hello = inventory.packages[0]
        self.assertEqual(hello.source, "hello")
        self.assertEqual(hello.revision, "1")
        self.assertEqual(inventory.packages[1].epoch, "1")
        self.assertEqual(inventory.packages[2].epoch, "")
        self.assertEqual(inventory.packages[2].revision, "")
        self.assertEqual(inventory.packages[3].epoch, "")
        self.assertEqual(inventory.packages[3].upstream, "1.2")
        self.assertEqual(inventory.units, ["cron.service"])
        rocky = "PKG\thello\t2.10\t1.el9\tx86_64\t(none)\thello-1.src.rpm\n"
        with self.assertRaises(Refused) as ctx:
            parse_deb_inventory(rocky)
        self.assertEqual(str(ctx.exception), "unknown inventory line")

    def test_collect_refuses_without_execute(self) -> None:
        def runner(argv: list[str], script: str) -> str:
            raise AssertionError("runner")

        with self.assertRaises(Refused) as ctx:
            collect_deb_inventory("not a host", execute=False, runner=runner)
        self.assertEqual(str(ctx.exception), "refusing to collect inventory without --execute")
        self.assertNotIn("not a host", str(ctx.exception))

    def test_collect_uses_ssh_argv(self) -> None:
        seen: dict[str, object] = {}

        def runner(argv: list[str], script: str) -> str:
            seen["argv"] = argv
            seen["script"] = script
            return "PKG\thello\t1.0-1\tall\thello\n"

        inventory = collect_deb_inventory("box.example", execute=True, runner=runner)
        argv = seen["argv"]
        self.assertIsInstance(argv, list)
        assert isinstance(argv, list)
        self.assertEqual(argv[-3], "box.example")
        self.assertIn("dpkg-query", str(seen["script"]))
        self.assertEqual(inventory.packages[0].name, "hello")


class RockyApplyTests(unittest.TestCase):
    def test_identity_and_live_root(self) -> None:
        with self.assertRaises(Refused) as ctx:
            assert_rocky_release("ID=debian\nVERSION_CODENAME=bookworm\n", 9)
        self.assertIn("want rocky", str(ctx.exception))
        with self.assertRaises(Refused) as ctx:
            assert_rocky_release("ID=Rocky\nVERSION_ID=9\n", 9)
        self.assertIn("want rocky", str(ctx.exception))
        with self.assertRaises(Refused) as ctx:
            assert_rocky_release("ID=rocky\nVERSION_ID=90\n", 9)
        self.assertIn("want 9", str(ctx.exception))
        with self.assertRaises(Refused) as ctx:
            assert_rocky_release("ID=rocky\nVERSION_ID=09\n", 9)
        self.assertIn("want 9", str(ctx.exception))
        with self.assertRaises(Refused) as ctx:
            apply_rocky_bundle(Path("/"), 9, Path("/no/such/bundle"), "ID=rocky\nVERSION_ID=9\n")
        self.assertEqual(str(ctx.exception), "refusing to write /")

    def test_apply_copies_onto_rocky_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "bundle"
            (bundle / "etc").mkdir(parents=True)
            (bundle / "manifest").write_text("etc/hello.conf\n", encoding="utf-8")
            (bundle / "etc" / "hello.conf").write_text("hello\n", encoding="utf-8")
            host = root / "host"
            host.mkdir()
            written = apply_rocky_bundle(
                host, 9, bundle, 'ID=rocky\nVERSION_ID="9.3"\n'
            )
            target = host / "etc" / "hello.conf"
            self.assertEqual(written, ["etc/hello.conf"])
            self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)


class SpecFromTreeTests(unittest.TestCase):
    def test_build_system_is_checked_before_license(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dh = _tree(root / "dh", "hello", "2.10-1", "dh")
            with self.assertRaises(SpecUnemittable) as ctx:
                spec_from_tree(dh, "")
            self.assertIn("has no spec template", str(ctx.exception))
            mono_dh = _tree(root / "mono-dh", "ubuntu-mono", "1.0-1", "dh")
            with self.assertRaises(SpecUnemittable):
                spec_from_tree(mono_dh, "")
            tools = _tree(root / "tools", "hello", "2.10-1", "autotools")
            with self.assertRaises(Refused) as missing:
                spec_from_tree(tools, "")
            self.assertEqual(str(missing.exception), "missing license")
            mono = _tree(root / "mono", "ubuntu-mono", "1.0-1", "autotools")
            with self.assertRaises(Refused) as brand:
                spec_from_tree(mono, "")
            self.assertEqual(str(brand.exception), "trademark")
            spec = spec_from_tree(tools, "MIT")
            self.assertIn("%configure", spec)
            self.assertIn("License: MIT", spec)


class Debian2RockyCliTests(unittest.TestCase):
    def test_resolve_inventory_and_remap(self) -> None:
        code, out, err = run_d2r(["resolve", "libc6"])
        self.assertEqual(code, 0, err)
        self.assertIn("glibc\tbase\tlibc6", out)
        code, out, err = run_d2r(["resolve", "widget", "--index", "nocolon"])
        self.assertEqual(code, 1)
        self.assertIn("index entry must be name=version", err)
        self.assertIsInstance(Rocky2debError("index entry must be name=version"), Rocky2debError)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory = root / "inventory.txt"
            inventory.write_text("PKG\thello\t2.10-1\tall\thello (2.10-1)\n", encoding="utf-8")
            code, out, err = run_d2r(["inventory-parse", str(inventory)])
            self.assertEqual(code, 0, err)
            self.assertIn("pkg hello 2.10-1 hello", out)
            conf = root / "chrony.conf"
            conf.write_text("pool pool.ntp.org iburst\nnotakey secret-value\n", encoding="utf-8")
            code, out, err = run_d2r(["remap", "--map", "chrony", "--file", str(conf)])
            self.assertEqual(code, 0, err)
            self.assertIn("map chrony -> /etc/chrony.conf", out)
            self.assertIn("unmapped notakey", out)
            self.assertNotIn("secret-value", out)
            self.assertNotIn("pool.ntp.org", out)

    def test_collect_and_ssh_do_not_echo_a_bad_host(self) -> None:
        code, out, err = run_d2r(["inventory-collect", "ok.example"])
        self.assertEqual(code, 3)
        self.assertIn("without --execute", err)
        code, out, err = run_d2r(["ssh-argv", "not a host"])
        self.assertEqual(code, 3)
        self.assertIn("refusing host name", err)
        self.assertNotIn("not a host", err + out)

    def test_apply_and_build_refuse_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            osrel = root / "os-release"
            osrel.write_text("ID=debian\nVERSION_ID=12\n", encoding="utf-8")
            code, out, err = run_d2r(["apply", "--rocky", "9", "--os-release", str(osrel)])
            self.assertEqual(code, 3)
            self.assertIn("want rocky", err)
            osrel.write_text("ID=rocky\nVERSION_ID=9\n", encoding="utf-8")
            code, out, err = run_d2r(["apply", "--rocky", "9", "--os-release", str(osrel)])
            self.assertEqual(code, 0, err)
            self.assertEqual(out.strip(), "ok")
            code, out, err = run_d2r(
                ["apply", "--execute", "--rocky", "9", "--os-release", str(osrel)]
            )
            self.assertEqual(code, 3)
            self.assertIn("apply needs --root and --bundle", err)
            bundle = root / "bundle"
            (bundle / "etc").mkdir(parents=True)
            (bundle / "manifest").write_text("etc/hello.conf\n", encoding="utf-8")
            (bundle / "etc" / "hello.conf").write_text("hello\n", encoding="utf-8")
            host = root / "host"
            host.mkdir()
            code, out, err = run_d2r(
                [
                    "apply",
                    "--execute",
                    "--rocky",
                    "9",
                    "--os-release",
                    str(osrel),
                    "--root",
                    str(host),
                    "--bundle",
                    str(bundle),
                ]
            )
            self.assertEqual(code, 0, err)
            self.assertEqual((host / "etc" / "hello.conf").read_text(encoding="utf-8"), "hello\n")

        import rocky2deb.debian2rocky_cli as cli

        def boom(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("run_build")

        original = cli.run_build
        cli.run_build = boom
        try:
            code, out, err = run_d2r(["build"])
        finally:
            cli.run_build = original
        self.assertEqual(code, 3)
        self.assertIn("without --execute", err)


class UbuntuPolicyTests(unittest.TestCase):
    def test_suites_and_version_rewrite(self) -> None:
        self.assertEqual(ubuntu_release("resolute"), "26.04")
        with self.assertRaises(Refused) as ctx:
            ubuntu_release("stonking")
        self.assertEqual(
            str(ctx.exception),
            "unknown Ubuntu suite 'stonking'. Known suites: "
            "jammy, noble, oracular, plucky, questing, resolute",
        )
        self.assertEqual(
            retarget_version(
                "2.10-3ubuntu1", distro="debian", ubuntu_suite="noble", debian_suite="trixie"
            ),
            "2.10-3ubuntu1~trixie1",
        )
        self.assertEqual(
            retarget_version(
                "2.10-3", distro="debian", ubuntu_suite="jammy", debian_suite="trixie"
            ),
            "2.10-3~ubuntu.jammy.1",
        )
        self.assertEqual(
            retarget_version(
                "1:2.10-3ubuntu1",
                distro="debian",
                ubuntu_suite="noble",
                debian_suite="trixie",
            ),
            "1:2.10-3ubuntu1~trixie1",
        )
        self.assertEqual(
            retarget_version(
                "2.10-3ubuntu1", distro="rocky", ubuntu_suite="noble", rocky_major=9
            ),
            "2.10-3ubuntu1~el91",
        )
        self.assertEqual(
            retarget_version("2.10-3", distro="rocky", ubuntu_suite="jammy", rocky_major=9),
            "2.10-3~el91",
        )
        with self.assertRaises(Refused) as ctx:
            retarget_version(
                "2.10-3ubuntu1\nEvil: injected",
                distro="debian",
                ubuntu_suite="noble",
                debian_suite="trixie",
            )
        self.assertEqual(str(ctx.exception), "refusing version")
        self.assertNotIn("Evil", str(ctx.exception))
        self.assertNotIn("injected", str(ctx.exception))
        with self.assertRaises(Refused) as ctx:
            retarget_version("2.10-3", distro="fedora", ubuntu_suite="noble")
        self.assertEqual(str(ctx.exception), "distro must be debian or rocky")

    def test_sources_parser_refuses_hostile_index(self) -> None:
        debian = _debian_tar("hello", "2.10-3ubuntu1")
        good = _sources([("hello_2.10-3ubuntu1.debian.tar.xz", debian)])
        _payload, stanzas = parse_sources(good)
        self.assertEqual(stanzas[0].package, "hello")
        self.assertEqual(stanzas[0].version, "2.10-3ubuntu1")
        libc = (
            "Package: libc6\nVersion: 2.41-1\nDirectory: pool/main/g/glibc\n"
            "Checksums-Sha256:\n "
            + "ab" * 32
            + " 4 libc6_2.41.orig.tar.gz\n\n"
        )
        _payload, both = parse_sources(good + b"\n" + libc.encode())
        self.assertEqual([item.package for item in both], ["hello", "libc6"])
        with self.assertRaises(Refused) as ctx:
            parse_sources(b"\xff")
        self.assertEqual(str(ctx.exception), "sources are not utf-8")
        with self.assertRaises(Refused) as ctx:
            parse_sources(b"Package: hello\nVersion: 1\nDirectory: pool/main/h/hello\n")
        self.assertEqual(str(ctx.exception), "missing checksum")
        with self.assertRaises(Refused) as ctx:
            parse_sources(
                b"Package: hello\nVersion: 1\nDirectory: /pool\nChecksums-Sha256:\n "
                + b"ab" * 32
                + b" 4 hello.dsc\n"
            )
        self.assertEqual(str(ctx.exception), "refusing source directory")
        with self.assertRaises(Refused) as ctx:
            parse_sources(
                b"Package: hello\nVersion: 1\nDirectory: pool/main/h/hello\n"
                b"Checksums-Sha256:\n " + b"ab" * 32 + b" 4 ../evil\n"
            )
        self.assertEqual(str(ctx.exception), "refusing source file")
        with self.assertRaises(Refused) as ctx:
            parse_sources(
                b"Package: hello\nVersion: 1\nDirectory: pool/main/h/hello\n"
                b"Checksums-Sha256:\n " + b"ab" * 32 + b" 0 hello.dsc\n"
            )
        self.assertEqual(str(ctx.exception), "refusing empty size")
        rows = "\n".join(f" {'ab' * 32} 4 f{index:02d}.dsc" for index in range(33))
        with self.assertRaises(Refused) as ctx:
            parse_sources(
                f"Package: hello\nVersion: 1\nDirectory: pool/main/h/hello\n"
                f"Checksums-Sha256:\n{rows}\n".encode()
            )
        self.assertEqual(str(ctx.exception), "refusing source file count")
        with self.assertRaises(Refused) as ctx:
            parse_sources(b"x" * (DEFAULT_LIMIT + 1))
        self.assertEqual(str(ctx.exception), f"input exceeds {DEFAULT_LIMIT} bytes")


def _export_files(kind: str = "autotools", version: str = "2.10-3ubuntu1", older: str = "2.10-2") -> dict[str, bytes]:
    debian = _debian_tar("hello", version, kind, older)
    orig = b"orig-bytes"
    dsc = b"BOGUS-DOWNLOADED-DSC\n"
    return {
        "hello_2.10-3ubuntu1.dsc": dsc,
        "hello_2.10.orig.tar.gz": orig,
        "hello_2.10-3ubuntu1.debian.tar.xz": debian,
    }


class UbuntuExportTests(unittest.TestCase):
    def _keyring(self, root: Path) -> Path:
        path = root / "keyring.gpg"
        path.write_bytes(b"not-a-real-keyring")
        return path

    def test_plan_does_not_download_or_create_dest(self) -> None:
        files = _export_files()
        sources = _sources(list(files.items()), bad_md5=True)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out"

            def opener(*_args: object) -> tuple[bytes, str]:
                raise AssertionError("opener")

            plan = export_ubuntu(
                sources,
                "hello",
                ubuntu_suite="noble",
                distro="debian",
                debian_suite="trixie",
                execute=False,
                dest=dest,
                opener=opener,
            )
            self.assertEqual(plan.version, "2.10-3ubuntu1~trixie1")
            self.assertFalse(dest.exists())

    def test_special_packages_and_userinfo_stop_before_download(self) -> None:
        files = _export_files()
        calls: list[str] = []

        def opener(url: str, timeout: int, limit: int) -> tuple[bytes, str]:
            calls.append(url)
            return files[url.rsplit("/", 1)[-1]], url

        base = _sources(list(files.items()), package="libc6", binary="libc6")
        with self.assertRaises(Refused) as ctx:
            export_ubuntu(
                base, "libc6", ubuntu_suite="noble", distro="debian", debian_suite="trixie", opener=opener
            )
        self.assertEqual(str(ctx.exception), "base package")
        branded = _sources(list(files.items()), binary="libc6")
        with self.assertRaises(Refused) as ctx:
            export_ubuntu(
                branded,
                "hello",
                ubuntu_suite="noble",
                distro="debian",
                debian_suite="trixie",
                opener=opener,
            )
        self.assertEqual(str(ctx.exception), "base package")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(Refused) as ctx:
                export_ubuntu(
                    _sources(list(files.items())),
                    "hello",
                    ubuntu_suite="noble",
                    distro="debian",
                    debian_suite="trixie",
                    execute=True,
                    license_text="MIT",
                    signature=b"sig",
                    keyring=self._keyring(root),
                    dest=root / "out",
                    archive="https://user:s3cret-token@archive.ubuntu.com/ubuntu",
                    opener=opener,
                    runner=lambda _argv: 0,
                )
            self.assertEqual(str(ctx.exception), "refusing URL with userinfo")
            self.assertNotIn("s3cret-token", str(ctx.exception))
            self.assertEqual(calls, [])
            self.assertFalse((root / "out").exists())

    def test_license_signature_and_classify_before_opener(self) -> None:
        files = _export_files()
        calls: list[str] = []

        def opener(url: str, timeout: int, limit: int) -> tuple[bytes, str]:
            calls.append(url)
            return b"x", url

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keyring = self._keyring(root)
            common = dict(
                ubuntu_suite="noble",
                distro="debian",
                debian_suite="trixie",
                execute=True,
                signature=b"sig",
                keyring=keyring,
                dest=root / "out",
                opener=opener,
                runner=lambda _argv: 0,
            )
            with self.assertRaises(Refused) as ctx:
                export_ubuntu(_sources(list(files.items())), "hello", license_text="", **common)
            self.assertEqual(str(ctx.exception), "missing license")
            with self.assertRaises(Refused) as ctx:
                export_ubuntu(
                    _sources(list(files.items())),
                    "hello",
                    license_text="Proprietary",
                    **common,
                )
            self.assertEqual(str(ctx.exception), "license unknown")
            with self.assertRaises(Refused) as ctx:
                export_ubuntu(
                    _sources(list(files.items())),
                    "hello",
                    license_text="MIT",
                    runner=lambda _argv: 1,
                    ubuntu_suite="noble",
                    distro="debian",
                    debian_suite="trixie",
                    execute=True,
                    signature=b"sig",
                    keyring=keyring,
                    dest=root / "out",
                    opener=opener,
                )
            self.assertEqual(str(ctx.exception), "signature rejected by gpgv")
            doubled = list(files.items()) + [("hello_2.10.orig.tar.xz", b"second-orig")]
            with self.assertRaises(Refused) as ctx:
                export_ubuntu(
                    _sources(doubled),
                    "hello",
                    license_text="MIT",
                    **common,
                )
            self.assertEqual(str(ctx.exception), "multiple source tarballs")
            debian_only = {"hello_2.10-3ubuntu1.debian.tar.xz": files["hello_2.10-3ubuntu1.debian.tar.xz"]}
            with self.assertRaises(Refused) as ctx:
                export_ubuntu(
                    _sources(list(debian_only.items())),
                    "hello",
                    license_text="MIT",
                    **common,
                )
            self.assertEqual(str(ctx.exception), "source tarball missing")
            self.assertEqual(calls, [])
            self.assertFalse((root / "out").exists())

    def test_checksum_failure_publishes_nothing(self) -> None:
        files = _export_files()
        calls: list[str] = []

        def opener(url: str, timeout: int, limit: int) -> tuple[bytes, str]:
            calls.append(url)
            return b"nope", url

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "out"
            with self.assertRaises(Refused) as ctx:
                export_ubuntu(
                    _sources(list(files.items()), bad_md5=True),
                    "hello",
                    ubuntu_suite="noble",
                    distro="debian",
                    debian_suite="trixie",
                    execute=True,
                    license_text="MIT",
                    signature=b"sig",
                    keyring=self._keyring(root),
                    dest=dest,
                    opener=opener,
                    runner=lambda _argv: 0,
                )
            self.assertEqual(str(ctx.exception), "checksum mismatch")
            self.assertTrue(calls)
            self.assertFalse(dest.exists())
            self.assertEqual(list(root.glob("*.partial")), [])

        huge = (
            "Package: hello\nBinary: hello\nVersion: 2.10-3ubuntu1\n"
            "Directory: pool/main/h/hello\nChecksums-Sha256:\n "
            + "ab" * 32
            + f" {DEFAULT_LIMIT + 1} hello_2.10-3ubuntu1.debian.tar.xz\n"
        )
        calls.clear()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(Refused) as ctx:
                export_ubuntu(
                    huge.encode(),
                    "hello",
                    ubuntu_suite="noble",
                    distro="rocky",
                    rocky_major=9,
                    execute=True,
                    license_text="MIT",
                    signature=b"sig",
                    keyring=self._keyring(root),
                    dest=root / "out",
                    opener=opener,
                    runner=lambda _argv: 0,
                )
            self.assertEqual(str(ctx.exception), "checksum mismatch")
            self.assertEqual(calls, [])

        def long_opener(url: str, timeout: int, limit: int) -> tuple[bytes, str]:
            return b"012345678", url

        short = (
            "Package: hello\nBinary: hello\nVersion: 2.10-3ubuntu1\n"
            "Directory: pool/main/h/hello\nChecksums-Sha256:\n "
            + "ab" * 32
            + " 4 hello_2.10-3ubuntu1.debian.tar.xz\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(Refused) as ctx:
                export_ubuntu(
                    short.encode(),
                    "hello",
                    ubuntu_suite="noble",
                    distro="rocky",
                    rocky_major=9,
                    execute=True,
                    license_text="MIT",
                    signature=b"sig",
                    keyring=self._keyring(root),
                    dest=root / "out",
                    opener=long_opener,
                    runner=lambda _argv: 0,
                )
            self.assertEqual(str(ctx.exception), "checksum mismatch")

    def test_execute_repacks_debian_and_does_not_run_rules(self) -> None:
        stray = Path.cwd() / "SHOULD_NOT_EXIST"
        self.addCleanup(lambda: stray.unlink(missing_ok=True))
        files = _export_files()
        sources = _sources(list(files.items()), bad_md5=True)
        calls: list[str] = []

        def opener(url: str, timeout: int, limit: int) -> tuple[bytes, str]:
            calls.append(url)
            name = url.rsplit("/", 1)[-1]
            body = files[name]
            return body, url

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "out"
            plan = export_ubuntu(
                sources,
                "hello",
                ubuntu_suite="noble",
                distro="debian",
                debian_suite="trixie",
                execute=True,
                license_text="MIT",
                signature=b"sig",
                keyring=self._keyring(root),
                dest=dest,
                opener=opener,
                runner=lambda _argv: 0,
            )
            self.assertEqual(plan.version, "2.10-3ubuntu1~trixie1")
            self.assertTrue(calls)
            self.assertFalse(stray.exists())
            dsc_path = dest / "hello_2.10-3ubuntu1~trixie1.dsc"
            orig_path = dest / "hello_2.10.orig.tar.gz"
            self.assertTrue(dsc_path.is_file())
            self.assertTrue(orig_path.is_file())
            self.assertEqual(orig_path.read_bytes(), b"orig-bytes")
            dsc = dsc_path.read_text(encoding="utf-8")
            self.assertTrue(dsc.startswith("Format: 3.0"))
            self.assertNotIn("BOGUS-DOWNLOADED-DSC", dsc)
            for path in dest.iterdir():
                self.assertFalse(b"BOGUS-DOWNLOADED-DSC" in path.read_bytes())
                self.assertEqual(path.stat().st_mode & 0o777, 0o644)
                self.assertFalse(path.name.endswith(".partial"))
            debian_tar = next(path for path in dest.iterdir() if ".debian.tar." in path.name)
            with tarfile.open(debian_tar, mode="r:xz") as archive:
                names = archive.getnames()
                self.assertNotIn("SHOULD_NOT_EXIST", names)
                changelog = archive.extractfile("debian/changelog")
                self.assertIsNotNone(changelog)
                assert changelog is not None
                text = changelog.read().decode()
                rules = archive.extractfile("debian/rules")
                self.assertIsNotNone(rules)
                assert rules is not None
                rules_text = rules.read().decode()
                rules_info = archive.getmember("debian/rules")
            self.assertEqual(text.count("~trixie1"), 1)
            self.assertIn("2.10-2", text)
            self.assertIn("touch SHOULD_NOT_EXIST", rules_text)
            self.assertEqual(rules_info.mode & 0o777, 0o755)
            for line in dsc.splitlines():
                parts = line.split()
                if len(parts) == 3 and len(parts[0]) == 64:
                    digest, size, name = parts
                    body = (dest / name).read_bytes()
                    self.assertEqual(len(body), int(size))
                    self.assertEqual(hashlib.sha256(body).hexdigest(), digest)

    def test_epoch_filename_drops_the_epoch(self) -> None:
        version = "1:2.10-3ubuntu1"
        files = _export_files(version=version, older="")
        sources = _sources(list(files.items()), version=version)

        def opener(url: str, timeout: int, limit: int) -> tuple[bytes, str]:
            return files[url.rsplit("/", 1)[-1]], url

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "out"
            export_ubuntu(
                sources,
                "hello",
                ubuntu_suite="noble",
                distro="debian",
                debian_suite="trixie",
                execute=True,
                license_text="MIT",
                signature=b"sig",
                keyring=self._keyring(root),
                dest=dest,
                opener=opener,
                runner=lambda _argv: 0,
            )
            dsc = dest / "hello_2.10-3ubuntu1~trixie1.dsc"
            self.assertTrue(dsc.is_file())
            self.assertTrue((dest / "hello_2.10.orig.tar.gz").is_file())
            text = dsc.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("Format: 3.0"))
            self.assertIn("Version: 1:2.10-3ubuntu1~trixie1", text)
            self.assertFalse(any(":" in path.name for path in dest.iterdir()))

    def test_symlink_member_and_changelog_mismatch_leave_dest_empty(self) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:xz") as archive:
            info = tarfile.TarInfo("debian/rules")
            info.type = tarfile.SYMTYPE
            info.linkname = "debian/control"
            archive.addfile(info)
        debian = buffer.getvalue()
        files = {"hello_2.10-3ubuntu1.debian.tar.xz": debian, "hello_2.10.orig.tar.gz": b"orig-bytes"}
        calls: list[str] = []

        def opener(url: str, timeout: int, limit: int) -> tuple[bytes, str]:
            calls.append(url)
            return files[url.rsplit("/", 1)[-1]], url

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "out"
            with self.assertRaises(Refused) as ctx:
                export_ubuntu(
                    _sources(list(files.items())),
                    "hello",
                    ubuntu_suite="noble",
                    distro="debian",
                    debian_suite="trixie",
                    execute=True,
                    license_text="MIT",
                    signature=b"sig",
                    keyring=self._keyring(root),
                    dest=dest,
                    opener=opener,
                    runner=lambda _argv: 0,
                )
            self.assertEqual(str(ctx.exception), "refusing symlink in debian tarball")
            self.assertTrue(calls)
            self.assertFalse(dest.exists())

        mismatched = _debian_tar("goodbye", "2.10-3ubuntu1", older="")
        files = {
            "hello_2.10-3ubuntu1.debian.tar.xz": mismatched,
            "hello_2.10.orig.tar.gz": b"orig-bytes",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "out"
            with self.assertRaises(Refused) as ctx:
                export_ubuntu(
                    _sources(list(files.items())),
                    "hello",
                    ubuntu_suite="noble",
                    distro="debian",
                    debian_suite="trixie",
                    execute=True,
                    license_text="MIT",
                    signature=b"sig",
                    keyring=self._keyring(root),
                    dest=dest,
                    opener=opener,
                    runner=lambda _argv: 0,
                )
            self.assertEqual(str(ctx.exception), "changelog package does not match source")
            self.assertFalse(dest.exists())

    def test_folded_version_refuses_without_the_injected_text(self) -> None:
        text = (
            "Package: hello\nBinary: hello\nVersion: 2.10-3ubuntu1\n"
            " Evil: injected\nDirectory: pool/main/h/hello\nChecksums-Sha256:\n "
            + "ab" * 32
            + " 4 hello_2.10-3ubuntu1.debian.tar.xz\n"
        )
        with self.assertRaises(Refused) as ctx:
            export_ubuntu(
                text.encode(),
                "hello",
                ubuntu_suite="noble",
                distro="debian",
                debian_suite="trixie",
            )
        self.assertEqual(str(ctx.exception), "refusing version")
        self.assertNotIn("injected", str(ctx.exception))
        self.assertNotIn("Evil", str(ctx.exception))

    def test_rocky_spec_and_unemittable_dh(self) -> None:
        calls: list[str] = []

        def opener(url: str, timeout: int, limit: int) -> tuple[bytes, str]:
            calls.append(url)
            return files[url.rsplit("/", 1)[-1]], url

        files = {
            "hello_2.10-3ubuntu1.debian.tar.xz": _debian_tar("hello", "2.10-3ubuntu1", "autotools", ""),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "out"
            export_ubuntu(
                _sources(list(files.items())),
                "hello",
                ubuntu_suite="noble",
                distro="rocky",
                rocky_major=9,
                execute=True,
                license_text="MIT",
                signature=b"sig",
                keyring=self._keyring(root),
                dest=dest,
                opener=opener,
                runner=lambda _argv: 0,
            )
            spec = (dest / "hello.spec").read_text(encoding="utf-8")
            self.assertIn("%configure", spec)
            self.assertIn("3ubuntu1~el91", spec)
            self.assertIn("License: MIT", spec)
            self.assertFalse(stray_exists())
            self.assertEqual(list(dest.glob("*.rpm")), [])

        calls.clear()
        files = {
            "hello_2.10-3ubuntu1.debian.tar.xz": _debian_tar("hello", "2.10-3ubuntu1", "dh", ""),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "out"
            with self.assertRaises(SpecUnemittable) as ctx:
                export_ubuntu(
                    _sources(list(files.items())),
                    "hello",
                    ubuntu_suite="noble",
                    distro="rocky",
                    rocky_major=9,
                    execute=True,
                    license_text="MIT",
                    signature=b"sig",
                    keyring=self._keyring(root),
                    dest=dest,
                    opener=opener,
                    runner=lambda _argv: 0,
                )
            self.assertIn("has no spec template", str(ctx.exception))
            self.assertTrue(calls)
            self.assertFalse(dest.exists())


def stray_exists() -> bool:
    return (Path.cwd() / "SHOULD_NOT_EXIST").exists()


class UbuntuCliTests(unittest.TestCase):
    def test_plan_and_export_refusals(self) -> None:
        files = _export_files(older="")
        sources_bytes = _sources(list(files.items()))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = root / "Sources"
            sources.write_bytes(sources_bytes)
            before = set(root.iterdir())
            code, out, err = run_ubuntu(
                [
                    "plan",
                    "--sources",
                    str(sources),
                    "--package",
                    "hello",
                    "--ubuntu",
                    "noble",
                    "--suite",
                    "trixie",
                ]
            )
            self.assertEqual(code, 0, err)
            self.assertIn("2.10-3ubuntu1~trixie1", out)
            digest = hashlib.sha256(files["hello_2.10.orig.tar.gz"]).hexdigest()
            self.assertIn(digest, out)
            self.assertEqual(set(root.iterdir()), before)
            dest = root / "out"
            code, out, err = run_ubuntu(
                [
                    "export",
                    "--sources",
                    str(sources),
                    "--package",
                    "hello",
                    "--ubuntu",
                    "noble",
                    "--suite",
                    "trixie",
                    "--dest",
                    str(dest),
                ]
            )
            self.assertEqual(code, 3)
            self.assertIn("without --execute", err)
            self.assertFalse(dest.exists())
            code, out, err = run_ubuntu(
                [
                    "export",
                    "--sources",
                    str(sources),
                    "--package",
                    "hello",
                    "--ubuntu",
                    "noble",
                    "--suite",
                    "trixie",
                    "--archive",
                    "https://user:s3cret-token@archive.ubuntu.com/ubuntu",
                ]
            )
            self.assertEqual(code, 3)
            self.assertIn("refusing URL with userinfo", err)
            self.assertNotIn("s3cret-token", err)
            self.assertNotIn("without --execute", err)
            link = root / "ZEBRA-sources"
            link.symlink_to(sources)
            code, out, err = run_ubuntu(
                [
                    "plan",
                    "--sources",
                    str(link),
                    "--package",
                    "hello",
                    "--ubuntu",
                    "noble",
                    "--suite",
                    "trixie",
                ]
            )
            self.assertEqual(code, 3)
            self.assertIn("missing sources", err)
            self.assertNotIn("ZEBRA", err)
            signature = root / "sig.asc"
            signature.write_bytes(b"sig")
            key = root / "keyring.gpg"
            key.write_bytes(b"key")
            key_link = root / "ZEBRA-keyring"
            key_link.symlink_to(key)
            code, out, err = run_ubuntu(
                [
                    "export",
                    "--execute",
                    "--sources",
                    str(sources),
                    "--package",
                    "hello",
                    "--ubuntu",
                    "noble",
                    "--suite",
                    "trixie",
                    "--license",
                    "MIT",
                    "--signature",
                    str(signature),
                    "--keyring",
                    str(key_link),
                    "--dest",
                    str(dest),
                ]
            )
            self.assertEqual(code, 3)
            self.assertIn("missing keyring", err)
            self.assertNotIn("ZEBRA", err)
            self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
