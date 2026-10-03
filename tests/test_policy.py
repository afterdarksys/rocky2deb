"""Resolver, license, remap, gates, inventory, project, and suite upgrade."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rocky2deb.applycheck import assert_debian_suite
from rocky2deb.errors import Refused
from rocky2deb.gates import assign_waves, next_wave_gate, pre_apply_gate
from rocky2deb.inventory import parse_inventory, ssh_argv
from rocky2deb.license import LedgerEntry, classify_text, load_ledger, rebuild_permission
from rocky2deb.names import load_name_map
from rocky2deb.project import init_project, load_project
from rocky2deb.remap import ConfigMap, format_remap_report, load_maps, remap_text, translate_config_path
from rocky2deb.resolve import Decision, package_list, resolve_one
from rocky2deb.upgrade import retarget, suite_upgrade_plan

BOOKWORM = 'ID=debian\nVERSION_CODENAME="bookworm"\n'
ROCKY = "ID=rocky\nVERSION_ID=9\n"


def resolve(name, **kwargs):
    return resolve_one(
        name,
        index=kwargs.get("index", {}),
        names=kwargs.get("names", load_name_map()),
        pins=kwargs.get("pins"),
        ledger=kwargs.get("ledger", load_ledger()),
        license_text=kwargs.get("license_text", ""),
        floors=kwargs.get("floors"),
    )


class LicenseTests(unittest.TestCase):
    def test_classify(self) -> None:
        self.assertEqual(classify_text("GPLv2+"), "dfsg")
        self.assertEqual(classify_text(""), "unknown")
        self.assertEqual(classify_text("Proprietary"), "unknown")
        self.assertEqual(classify_text("MIT and Proprietary"), "unknown")
        self.assertEqual(classify_text("MIT or BSD"), "dfsg")
        self.assertEqual(classify_text("MIT and BSD"), "dfsg")
        self.assertEqual(classify_text("MIT and BSD or GPL"), "unknown")
        self.assertNotEqual(classify_text("Proprietary"), "non-free")

    def test_ledger(self) -> None:
        ledger = load_ledger()
        allowed, klass, why = rebuild_permission("redis", "", ledger)
        self.assertTrue(allowed)
        self.assertEqual(klass, "non-free")
        self.assertEqual(why, "ledger allow_rebuild")
        allowed, klass, why = rebuild_permission("rocky-logos", "MIT", ledger)
        self.assertFalse(allowed)
        self.assertEqual(klass, "trademark")
        self.assertEqual(why, "trademark")
        custom = {"secretpkg": LedgerEntry("secretpkg", "non-free", False)}
        allowed, klass, why = rebuild_permission("secretpkg", "MIT", custom)
        self.assertFalse(allowed)
        self.assertEqual(why, "non-free rebuild not allowed")


class ResolveTests(unittest.TestCase):
    def test_order(self) -> None:
        mapped = resolve("httpd", index={"apache2": "2.4.62-1"})
        self.assertEqual(mapped.action, "debian")
        self.assertEqual(mapped.deb, "apache2")
        self.assertEqual(mapped.reason, "curated map")

        same = resolve("widget", index={"widget": "1.2.0-1"}, license_text="MIT")
        self.assertEqual(same.action, "debian")
        self.assertEqual(same.reason, "same name in suite index")

        rebuilt = resolve("widget", license_text="MIT")
        self.assertEqual(rebuilt.action, "rebuild")
        self.assertEqual(rebuilt.deb, "widget")
        self.assertEqual(rebuilt.license_class, "dfsg")

        brand = resolve(
            "rocky-logos",
            index={"rocky-logos": "1"},
            pins={"rocky-logos"},
            license_text="MIT",
        )
        self.assertEqual(brand.action, "gap")
        self.assertEqual(brand.license_class, "trademark")

        pinned = resolve("widget", index={"widget": "1"}, pins={"widget"}, license_text="MIT")
        self.assertEqual(pinned.action, "pin-rebuild")

        base = resolve("glibc", index={"glibc": "2.34"}, pins={"glibc"}, license_text="MIT")
        self.assertEqual(base.action, "base")
        self.assertEqual(base.deb, "libc6")
        self.assertEqual(base.reason, "satisfied by the Debian installer")

        kernel = resolve("kernel-core", pins={"kernel-core"})
        self.assertEqual(kernel.action, "base")
        self.assertEqual(kernel.deb, "linux-image-amd64")

    def test_floor_and_license_gaps(self) -> None:
        missed = resolve(
            "httpd",
            index={"apache2": "2.4.62-1"},
            floors={"httpd": "1:1"},
            license_text="MIT",
        )
        self.assertEqual(missed.action, "rebuild")
        unknown = resolve("widget")
        self.assertEqual(unknown.action, "gap")
        self.assertEqual(unknown.reason, "license unknown")
        pinned_unknown = resolve("widget", pins={"widget"})
        self.assertEqual(pinned_unknown.action, "gap")
        self.assertEqual(pinned_unknown.reason, "license unknown")
        redis = resolve("redis")
        self.assertEqual(redis.action, "rebuild")
        self.assertEqual(redis.license_class, "non-free")
        self.assertEqual(redis.reason, "ledger allow_rebuild")
        redis_shipped = resolve("redis", index={"redis": "7"})
        self.assertEqual(redis_shipped.action, "debian")
        blocked = resolve(
            "secretpkg",
            ledger={"secretpkg": LedgerEntry("secretpkg", "non-free", False)},
            license_text="MIT",
        )
        self.assertEqual(blocked.action, "gap")
        self.assertEqual(blocked.reason, "non-free rebuild not allowed")

    def test_package_list_omits_gaps(self) -> None:
        names = package_list(
            [
                Decision("glibc", "base", "libc6"),
                Decision("widget", "rebuild", "widget"),
                Decision("rocky-logos", "gap", ""),
                Decision("httpd", "debian", "apache2"),
                Decision("httpd", "debian", "apache2"),
            ]
        )
        self.assertEqual(names, ["apache2", "libc6", "widget"])


class RemapTests(unittest.TestCase):
    def test_chrony_keeps_unmapped_and_hides_value(self) -> None:
        cmap = load_maps()["chrony"]
        text = "pool pool.ntp.org iburst\nnotakey secret-value\n"
        result = remap_text(text, cmap)
        report = format_remap_report(result)
        self.assertEqual(result.debian_path, "/etc/chrony/chrony.conf")
        self.assertIn("notakey", result.unmapped)
        self.assertIn("notakey", result.body)
        self.assertIn("secret-value", result.body)
        self.assertIn("secret-value", result.aside)
        self.assertNotIn("secret-value", report)
        self.assertIn("unmapped notakey", report)

    def test_sshd_sftp_and_cron_spool(self) -> None:
        maps = load_maps()
        result = remap_text(
            "Subsystem sftp /usr/libexec/openssh/sftp-server\n",
            maps["sshd"],
        )
        self.assertIn("/usr/lib/openssh/sftp-server", result.body)
        self.assertNotIn("/usr/libexec/openssh/sftp-server", result.body)
        self.assertEqual(result.unmapped, [])
        spool = maps["cron_spool"]
        once = translate_config_path("/var/spool/cron/root", spool)
        self.assertEqual(once, "/var/spool/cron/crontabs/root")
        self.assertEqual(translate_config_path(once, spool), once)

    def test_open_vocabulary_and_unknown_format(self) -> None:
        result = remap_text("kernel.pid_max = 4194304\n", load_maps()["sysctl"])
        self.assertEqual(result.unmapped, [])
        self.assertIn("kernel.pid_max", result.body)
        strange = ConfigMap("x", "/a", "/b", "ini", False, "copy")
        with self.assertRaises(Refused):
            remap_text("a=b\n", strange)


class ProjectTests(unittest.TestCase):
    def test_init_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_project(root, "forky", 10)
            project = load_project(root / "rocky2deb.toml")
            self.assertEqual(project.target_suite, "forky")
            self.assertEqual(project.rocky_major, 10)
            self.assertEqual(project.pins, [])
            self.assertTrue((root / "inventory").is_dir())
            self.assertIn("sshd", (root / "plan" / "waves.toml").read_text(encoding="utf-8"))
            with self.assertRaises(Refused):
                init_project(root, "trixie", 9)
            self.assertIn('target_suite = "forky"', (root / "rocky2deb.toml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Refused):
                init_project(Path(tmp), "sid", 9)
            with self.assertRaises(Refused):
                init_project(Path(tmp), "trixie", 7)


class GateTests(unittest.TestCase):
    def test_waves(self) -> None:
        waves = assign_waves(["a", "b", "c"], ["b"])
        self.assertEqual(waves["canary"], ["b"])
        self.assertEqual(waves["rest"], ["a", "c"])
        with self.assertRaises(Refused):
            assign_waves(["a"], ["z"])

    def test_pre_apply_and_next(self) -> None:
        clean = pre_apply_gate([Decision("httpd", "debian", "apache2")], [])
        self.assertTrue(clean.ok)
        blocked = pre_apply_gate(
            [Decision("widget", "gap", "", "license unknown")],
            ["Port"],
        )
        self.assertFalse(blocked.ok)
        self.assertIn("gap: widget", blocked.reasons)
        self.assertIn("unmapped: Port", blocked.reasons)
        waiting = next_wave_gate(False, False)
        self.assertFalse(waiting.ok)
        self.assertIn("pre_apply has not passed", waiting.reasons)
        self.assertIn("canary verify has not passed", waiting.reasons)
        self.assertTrue(next_wave_gate(True, True).ok)


class InventoryTests(unittest.TestCase):
    def test_parse_and_ssh_argv(self) -> None:
        text = (
            "PKG\thello\t1.0\t1\t(none)\tx86_64\thello-1.0-1.src.rpm\n"
            "PKG\tother\t2\t1\t0\tx86_64\tother-2-1.src.rpm\n"
            "PKG\tepochd\t3\t1\t2\tx86_64\tepochd-3-1.src.rpm\n"
            "UNIT\tsshd.service\tenabled\n"
        )
        inventory = parse_inventory(text)
        self.assertEqual(inventory.packages[0].epoch, "")
        self.assertEqual(inventory.packages[1].epoch, "")
        self.assertEqual(inventory.packages[2].epoch, "2")
        self.assertEqual(inventory.units, ["sshd.service"])
        with self.assertRaises(Refused):
            parse_inventory("NOPE\n")
        argv = ssh_argv("web-1.example.com")
        self.assertIn("BatchMode=yes", argv)
        self.assertEqual(argv[-3], "web-1.example.com")
        self.assertEqual(argv[-2:], ["bash", "-s"])
        self.assertNotIn(" ", argv[-3])
        with self.assertRaises(Refused) as caught:
            ssh_argv("-oProxyCommand=evil")
        self.assertNotIn("ProxyCommand", str(caught.exception))
        with self.assertRaises(Refused) as caught:
            ssh_argv("bad host")
        self.assertNotIn("bad host", str(caught.exception))


class UpgradeTests(unittest.TestCase):
    def test_apply_and_diff(self) -> None:
        assert_debian_suite(BOOKWORM, "bookworm")
        with self.assertRaises(Refused) as caught:
            assert_debian_suite(ROCKY, "bookworm")
        self.assertIn("rocky", str(caught.exception))
        with self.assertRaises(Refused) as caught:
            assert_debian_suite('ID="debian"\nVERSION_CODENAME=trixie\n', "bookworm")
        self.assertIn("want bookworm", str(caught.exception))

        decisions = [
            Decision("glibc", "base", "libc6", "satisfied by the Debian installer"),
            Decision("widget", "rebuild", "widget", "free license token", "dfsg"),
            Decision("pinned", "pin-rebuild", "pinned", "free license token", "dfsg"),
            Decision("nginx", "debian", "nginx", "curated map"),
            Decision("gone", "debian", "gone", "curated map"),
            Decision("held", "gap", "", "license unknown", "unknown"),
            Decision("nowship", "gap", "", "license unknown", "unknown"),
        ]
        index = {"widget": "1.2-1", "pinned": "1", "nginx": "1.24-1", "nowship": "1"}
        with self.assertRaises(Refused):
            suite_upgrade_plan(ROCKY, "bookworm", "trixie", decisions, index)
        with self.assertRaises(Refused):
            suite_upgrade_plan(
                'ID=debian\nVERSION_CODENAME=trixie\n',
                "bookworm",
                "trixie",
                decisions,
                index,
            )
        rows = {
            row.name: row
            for row in suite_upgrade_plan(BOOKWORM, "bookworm", "trixie", decisions, index)
        }
        self.assertEqual(rows["glibc"].action, "keep")
        self.assertEqual(rows["widget"].action, "flip-to-debian")
        self.assertEqual(rows["pinned"].action, "rebuild")
        self.assertEqual(rows["pinned"].reason, "pin still requests a rebuild")
        self.assertEqual(rows["nginx"].action, "keep")
        self.assertEqual(rows["gone"].action, "drop")
        self.assertEqual(rows["held"].action, "blocked")
        self.assertEqual(rows["nowship"].action, "flip-to-debian")
        suite, retargeted = retarget("forky", decisions, index)
        self.assertEqual(suite, "forky")
        self.assertEqual(len(retargeted), len(decisions))


if __name__ == "__main__":
    unittest.main()
