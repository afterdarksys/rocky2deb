"""Rockify closure, shims, binary glibc check, and compat package."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rocky2deb.errors import CycleError
from rocky2deb.names import is_blocked_root
from rocky2deb.primary import RepoPackage, load_repo
from rocky2deb.rockify import emit_compat_source, plan_closure

ROOT = Path(__file__).resolve().parents[1]


def repo_packages() -> list[RepoPackage]:
    return load_repo((ROOT / "tests" / "fixtures" / "repo.json").read_text(encoding="utf-8"))


class ClosureTests(unittest.TestCase):
    def test_topo_order(self) -> None:
        plan = plan_closure(
            "widget",
            repo_packages(),
            suite="trixie",
            rocky_major=9,
            index={"gcc": "12.2.0-1"},
        )
        self.assertEqual(plan.order, ["libwidget", "widget"])
        self.assertEqual(plan.report, "runs")
        actions = {item.rpm: item.action for item in plan.decisions}
        self.assertEqual(actions["gcc"], "debian")
        self.assertEqual(actions["glibc"], "base")
        self.assertEqual(actions["libwidget"], "rebuild")
        self.assertNotIn("rpmlib(CompressedFileNames)", actions)
        self.assertIsNone(emit_compat_source(plan, Path(tempfile.mkdtemp())))

    def test_blocked_roots(self) -> None:
        self.assertTrue(is_blocked_root("kernel"))
        self.assertTrue(is_blocked_root("kmod-foo"))
        self.assertTrue(is_blocked_root("foo-kmod"))
        plan = plan_closure("glibc", [], suite="trixie", rocky_major=9)
        self.assertEqual(plan.report, "blocked")
        self.assertIn("Debian installer", plan.reason)
        self.assertEqual(plan.order, [])

    def test_cycle_names_both_nodes(self) -> None:
        packages = [
            RepoPackage(name="left", license="MIT", requires=["right"]),
            RepoPackage(name="right", license="MIT", requires=["left"]),
        ]
        with self.assertRaises(CycleError) as caught:
            plan_closure("left", packages, suite="trixie", rocky_major=9)
        self.assertEqual(caught.exception.nodes, ["left", "right"])
        self.assertIn("left", str(caught.exception))
        self.assertIn("right", str(caught.exception))

    def test_unknown_gap_blocks_without_cycle(self) -> None:
        packages = [
            RepoPackage(name="left", license="", requires=["right"]),
            RepoPackage(name="right", license="", requires=["left"]),
        ]
        plan = plan_closure("left", packages, suite="trixie", rocky_major=9)
        self.assertEqual(plan.report, "blocked")
        self.assertEqual(plan.reason, "license unknown")

    def test_httpd_unit_alias(self) -> None:
        plan = plan_closure(
            "httpd",
            [],
            suite="trixie",
            rocky_major=9,
            index={"apache2": "2.4.62-1"},
        )
        self.assertEqual(plan.report, "runs-with-shims")
        self.assertEqual(plan.order, [])
        alias = next(shim for shim in plan.shims if shim.kind == "unit-alias")
        self.assertEqual(
            alias.source,
            "/usr/lib/systemd/system/apache2.service",
        )
        self.assertEqual(alias.dest, "/etc/systemd/system/httpd.service")
        with tempfile.TemporaryDirectory() as tmp:
            root = emit_compat_source(plan, Path(tmp))
            assert root is not None
            control = (root / "debian" / "control").read_text(encoding="utf-8")
            links = (root / "debian" / "rockify-compat-httpd.links").read_text(encoding="utf-8")
        self.assertIn("Package: rockify-compat-httpd", control)
        self.assertIn("Provides: rockify-httpd", control)
        self.assertIn("apache2", control)
        self.assertEqual(
            links.strip(),
            "/usr/lib/systemd/system/apache2.service /etc/systemd/system/httpd.service",
        )

    def test_stretch_unit_root(self) -> None:
        plan = plan_closure(
            "httpd",
            [],
            suite="stretch",
            rocky_major=8,
            index={"apache2": "2.4.25-3"},
        )
        alias = next(shim for shim in plan.shims if shim.kind == "unit-alias")
        self.assertTrue(alias.source.startswith("/lib/systemd/system/"))

    def test_pin_keeps_own_unit(self) -> None:
        plan = plan_closure(
            "httpd",
            [RepoPackage(name="httpd", license="MIT")],
            suite="trixie",
            rocky_major=9,
            index={"apache2": "2.4.62-1"},
            pins={"httpd"},
        )
        self.assertEqual(plan.decisions[0].action, "pin-rebuild")
        self.assertFalse(any(shim.kind == "unit-alias" for shim in plan.shims))

    def test_sftp_and_chrony_shims(self) -> None:
        ssh = plan_closure(
            "openssh-server",
            [RepoPackage(name="openssh-server", license="MIT")],
            suite="bookworm",
            rocky_major=9,
            index={"openssh-server": "1:9.2p1-2"},
        )
        self.assertEqual(ssh.report, "runs-with-shims")
        sftp = next(shim for shim in ssh.shims if shim.kind == "sftp")
        self.assertEqual(sftp.dest, "/usr/lib/openssh/sftp-server")
        chrony = plan_closure(
            "chrony",
            [RepoPackage(name="chrony", license="MIT")],
            suite="trixie",
            rocky_major=9,
            index={"chrony": "4.3-1"},
        )
        path = next(shim for shim in chrony.shims if shim.kind == "config-path")
        self.assertEqual(path.dest, "/etc/chrony/chrony.conf")

    def test_binary_glibc_does_not_drop_source_plan(self) -> None:
        blocked = plan_closure(
            "widget",
            repo_packages(),
            suite="stretch",
            rocky_major=9,
            index={"gcc": "12.2.0-1"},
            binary_symbols=["GLIBC_2.34"],
        )
        self.assertEqual(blocked.binary, "blocked")
        self.assertIn("GLIBC_2.34", blocked.binary_reason)
        self.assertEqual(blocked.order, ["libwidget", "widget"])
        self.assertEqual(blocked.report, "runs")
        ok = plan_closure(
            "widget",
            repo_packages(),
            suite="bookworm",
            rocky_major=9,
            index={"gcc": "12.2.0-1"},
            binary_symbols=["GLIBC_2.28"],
        )
        self.assertEqual(ok.binary, "ok")

    def test_compiler_log_blocks_without_patch(self) -> None:
        plan = plan_closure(
            "widget",
            repo_packages(),
            suite="stretch",
            rocky_major=9,
            index={"gcc": "12.2.0-1"},
            compiler_log="error: glibc too new",
        )
        self.assertTrue(plan.unbuildable)
        self.assertEqual(plan.report, "blocked")
        self.assertEqual(
            plan.reason,
            "toolchain too old for the suite and the package has no patch",
        )
        patched = [package for package in repo_packages()]
        patched[0] = RepoPackage(
            name="widget",
            license="MIT",
            requires=["libwidget", "glibc"],
            build_requires=["gcc"],
            patches=["widget-old-glibc.patch"],
        )
        plan = plan_closure(
            "widget",
            patched,
            suite="stretch",
            rocky_major=9,
            index={"gcc": "12.2.0-1"},
            compiler_log="error: glibc too new",
        )
        self.assertFalse(plan.unbuildable)
        self.assertEqual(plan.report, "runs")

    def test_weak_recommends(self) -> None:
        packages = [
            RepoPackage(name="widget", license="MIT", recommends=["extra"]),
            RepoPackage(name="extra", license="MIT"),
        ]
        plain = plan_closure("widget", packages, suite="trixie", rocky_major=9)
        self.assertEqual(plain.order, ["widget"])
        weak = plan_closure("widget", packages, suite="trixie", rocky_major=9, weak=True)
        self.assertEqual(weak.order, ["extra", "widget"])

    def test_blocked_plan_emits_no_compat(self) -> None:
        plan = plan_closure(
            "httpd",
            [RepoPackage(name="httpd", license="MIT", requires=["missingthing"])],
            suite="trixie",
            rocky_major=9,
            index={"apache2": "2.4.62-1"},
        )
        self.assertEqual(plan.report, "blocked")
        self.assertTrue(plan.shims)
        self.assertIsNone(emit_compat_source(plan, Path(tempfile.mkdtemp())))


if __name__ == "__main__":
    unittest.main()
