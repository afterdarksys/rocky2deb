"""Spec import, Debian and RPM emit, versions, names, dsc, and repo metadata."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rocky2deb.dsc import import_dsc_tree
from rocky2deb.emit_deb import emit_deb_tree
from rocky2deb.emit_rpm import emit_spec
from rocky2deb.errors import Refused, SpecUnemittable
from rocky2deb.ir import FileEntry, PackageIR, ir_from_json, ir_to_json
from rocky2deb.names import deb_package_name
from rocky2deb.primary import load_repo, parse_primary_xml
from rocky2deb.profiles import REQUESTED_SUITES, ROCKY_GLIBC, get_profile
from rocky2deb.spec import parse_spec
from rocky2deb.version import cmp_debian, cmp_glibc

ROOT = Path(__file__).resolve().parents[1]
HELLO = ROOT / "tests" / "fixtures" / "hello.spec"

PRIMARY = """\
<metadata xmlns="http://linux.duke.edu/metadata/common" xmlns:rpm="http://linux.duke.edu/metadata/rpm">
  <package type="rpm">
    <name>widget</name>
    <arch>x86_64</arch>
    <version epoch="0" ver="1.2.0" rel="1"/>
    <checksum type="sha256">abc</checksum>
    <location href="Packages/w/widget-1.2.0-1.x86_64.rpm"/>
    <format>
      <rpm:license>MIT</rpm:license>
      <rpm:sourcerpm>widget-1.2.0-1.src.rpm</rpm:sourcerpm>
      <rpm:requires>
        <rpm:entry name="glibc"/>
        <rpm:entry name="rpmlib(CompressedFileNames)"/>
      </rpm:requires>
    </format>
  </package>
</metadata>
"""


class SpecTests(unittest.TestCase):
    def test_hello_round_trip(self) -> None:
        package = parse_spec(HELLO.read_text(encoding="utf-8"), source_id="hello.spec")
        self.assertEqual(package.name, "hello")
        self.assertEqual(package.version, "2.12")
        self.assertEqual(package.release, "1")
        self.assertEqual(package.license, "GPL-3.0-or-later")
        self.assertEqual(
            package.sources,
            ["https://ftp.gnu.org/gnu/hello/hello-2.12.tar.gz"],
        )
        self.assertEqual([dep.name for dep in package.build_requires], ["gcc", "make"])
        self.assertEqual([dep.name for dep in package.requires], ["glibc"])
        self.assertEqual(package.build_system, "autotools")
        self.assertEqual(package.identity(), "hello-2.12-1")
        self.assertIn("echo installed", package.scripts.post)
        self.assertEqual([sub.name for sub in package.subpackages], ["hello-utils"])
        paths = {entry.path: entry for entry in package.files}
        self.assertIn("/usr/bin/hello", paths)
        conf = paths["/etc/hello.conf"]
        self.assertTrue(conf.config)
        self.assertTrue(conf.noreplace)
        utils = package.subpackages[0]
        self.assertEqual(utils.files[0].path, "/usr/bin/hello-utils")
        self.assertEqual(utils.summary, "Hello utilities")
        again = ir_from_json(ir_to_json(package))
        self.assertEqual(again.identity(), package.identity())
        self.assertEqual(again.build_system, "autotools")
        self.assertTrue(again.files[1].noreplace)

    def test_if_zero_skips_and_ifarch_keeps(self) -> None:
        text = """
Name: cond
Version: 1
Release: 1
License: MIT
%if 0
Requires: hidden
%endif
%if 0%{?rhel}
Requires: also-hidden
%endif
%ifarch i686
Requires: kept
%endif
"""
        package = parse_spec(text)
        names = [dep.name for dep in package.requires]
        self.assertEqual(names, ["kept"])
        self.assertIn("%ifarch body kept", package.warnings)

    def test_files_parent_segment_refused(self) -> None:
        text = """
Name: bad
Version: 1
Release: 1
%files
/usr/bin/../../etc/passwd
"""
        with self.assertRaises(Refused):
            parse_spec(text)


class EmitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.package = parse_spec(HELLO.read_text(encoding="utf-8"))

    def test_trixie_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = emit_deb_tree(self.package, "trixie", Path(tmp))
            debian = root / "debian"
            control = (debian / "control").read_text(encoding="utf-8")
            self.assertIn("debhelper-compat (= 13)", control)
            self.assertNotIn("debian/compat", control)
            self.assertFalse((debian / "compat").exists())
            self.assertIn("Rules-Requires-Root: no", control)
            self.assertIn("Standards-Version: 4.7.0", control)
            self.assertIn("Package: hello-utils", control)
            self.assertNotIn("glibc", control)
            self.assertNotIn("libc6", control)
            self.assertIn("gcc", control)
            rules = (debian / "rules").read_text(encoding="utf-8")
            self.assertIn("dh $@", rules)
            self.assertNotIn("%configure", rules)
            readme = (debian / "README.source").read_text(encoding="utf-8")
            self.assertIn("%configure", readme)
            self.assertIn("not executed", readme)
            self.assertEqual((debian / "source" / "format").read_text(encoding="utf-8"), "3.0 (quilt)\n")
            install = (debian / "hello.install").read_text(encoding="utf-8")
            self.assertIn("usr/bin/hello", install)
            conffiles = (debian / "hello.conffiles").read_text(encoding="utf-8")
            self.assertIn("/etc/hello.conf", conffiles)
            utils = (debian / "hello-utils.install").read_text(encoding="utf-8")
            self.assertIn("usr/bin/hello-utils", utils)
            postinst = (debian / "hello.postinst").read_text(encoding="utf-8")
            self.assertTrue(postinst.startswith("#!/bin/sh\nset -e\n"))
            self.assertIn("echo installed", postinst)
            changelog = (debian / "changelog").read_text(encoding="utf-8")
            self.assertIn("Sat, 03 Oct 2026", changelog)
            self.assertIn("hello-2.12-1", changelog)
            copyright_text = (debian / "copyright").read_text(encoding="utf-8")
            self.assertIn("GPL-3.0-or-later", copyright_text)
            self.assertNotIn("GNU GENERAL PUBLIC LICENSE", copyright_text)

    def test_stretch_compat_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = emit_deb_tree(self.package, "stretch", Path(tmp))
            debian = root / "debian"
            self.assertEqual((debian / "compat").read_text(encoding="utf-8"), "10\n")
            control = (debian / "control").read_text(encoding="utf-8")
            self.assertIn("debhelper (>= 10)", control)
            self.assertNotIn("debhelper-compat", control)
            self.assertNotIn("Rules-Requires-Root", control)
            self.assertIn("Standards-Version: 4.5.1", control)

    def test_library_paths(self) -> None:
        package = PackageIR(
            name="libhello",
            version="1",
            release="1",
            license="MIT",
            files=[
                FileEntry("/usr/lib64/libhello.so"),
                FileEntry("/usr/libexec/hello"),
            ],
            requires=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            trixie = emit_deb_tree(package, "trixie", Path(tmp) / "t")
            stretch = emit_deb_tree(package, "stretch", Path(tmp) / "s")
            trixie_install = (trixie / "debian" / "libhello.install").read_text(encoding="utf-8")
            stretch_install = (stretch / "debian" / "libhello.install").read_text(encoding="utf-8")
        self.assertIn("usr/lib/x86_64-linux-gnu/libhello.so", trixie_install)
        self.assertIn("usr/libexec/hello", trixie_install)
        self.assertIn("usr/lib/x86_64-linux-gnu/libhello.so", stretch_install)
        self.assertIn("usr/lib/hello", stretch_install)
        self.assertNotIn("libexec", stretch_install)


class EmitRpmTests(unittest.TestCase):
    def test_autotools_template(self) -> None:
        package = parse_spec(HELLO.read_text(encoding="utf-8"))
        spec = emit_spec(package)
        self.assertIn("%autosetup", spec)
        self.assertIn("%configure", spec)
        self.assertIn("%make_build", spec)
        self.assertIn("%make_install", spec)

    def test_known_systems(self) -> None:
        for system, marker in (
            ("cmake", "%cmake_build"),
            ("meson", "%meson_build"),
            ("python", "%py3_build"),
        ):
            spec = emit_spec(PackageIR(name="demo", version="1", release="1", build_system=system))
            self.assertIn(marker, spec)
        spec = emit_spec(PackageIR(name="empty", version="1", release="1", build_system="cmake"))
        self.assertIn("# warning: no files recorded", spec)

    def test_dh_is_unemittable(self) -> None:
        with self.assertRaises(SpecUnemittable) as caught:
            emit_spec(PackageIR(name="hello", version="1", release="1", build_system="dh"))
        self.assertIn("has no spec template", str(caught.exception))


class VersionTests(unittest.TestCase):
    def test_policy_order(self) -> None:
        self.assertGreater(cmp_debian("1:1.0", "2.0"), 0)
        self.assertLess(cmp_debian("1.0", "1.0-1"), 0)
        self.assertLess(cmp_debian("1.0~rc1", "1.0"), 0)
        self.assertEqual(cmp_debian("0:1.0-1", "1.0-1"), 0)
        self.assertGreater(cmp_glibc("2.34", "2.24"), 0)
        self.assertEqual(cmp_glibc("2.28", "2.28"), 0)
        self.assertLess(cmp_glibc("2.28", "2.36"), 0)


class NameTests(unittest.TestCase):
    def test_rejects_path_like(self) -> None:
        with self.assertRaises(Refused):
            deb_package_name("../evil")
        with self.assertRaises(Refused):
            deb_package_name("foo/bar")
        self.assertEqual(deb_package_name("Foo_Bar"), "foo-bar")


class ProfileTests(unittest.TestCase):
    def test_suites(self) -> None:
        self.assertEqual(REQUESTED_SUITES, ("stretch", "buster", "bullseye", "bookworm", "trixie"))
        self.assertEqual(ROCKY_GLIBC, {8: "2.28", 9: "2.34", 10: "2.39"})
        stretch = get_profile("stretch")
        trixie = get_profile("trixie")
        forky = get_profile("forky")
        self.assertEqual(stretch.debhelper_compat, 10)
        self.assertEqual(get_profile("buster").debhelper_compat, 12)
        self.assertEqual(get_profile("bullseye").debhelper_compat, 13)
        self.assertFalse(stretch.usrmerge)
        self.assertTrue(get_profile("bookworm").usrmerge)
        self.assertFalse(get_profile("bookworm").time64)
        self.assertTrue(trixie.time64)
        self.assertEqual(trixie.glibc, "2.41")
        self.assertEqual(forky.glibc, "2.43")
        self.assertEqual(forky.debian_version, 14)
        self.assertTrue(stretch.archive.startswith("https://archive.debian.org/"))
        self.assertTrue(trixie.archive.startswith("https://deb.debian.org/"))
        self.assertFalse(stretch.check_valid_until)
        self.assertTrue(trixie.check_valid_until)
        with self.assertRaises(Refused):
            get_profile("sid")


class DscTests(unittest.TestCase):
    def test_cmake_imports_and_emits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            debian = root / "debian"
            debian.mkdir()
            (debian / "control").write_text(
                "Source: hello\n"
                "Build-Depends: debhelper-compat (= 13), cmake\n"
                "\n"
                "Package: hello\n"
                "Architecture: any\n"
                "Depends: ${shlibs:Depends}, zlib\n"
                "Description: Hello\n"
                " A greeting.\n",
                encoding="utf-8",
            )
            (debian / "changelog").write_text(
                "hello (2.10-2) unstable; urgency=medium\n",
                encoding="utf-8",
            )
            (debian / "rules").write_text(
                "#!/usr/bin/make -f\n%:\n\tdh $@ --buildsystem=cmake\n",
                encoding="utf-8",
            )
            package = import_dsc_tree(root)
        self.assertEqual(package.build_system, "cmake")
        self.assertEqual(package.version, "2.10")
        self.assertEqual(package.release, "2")
        self.assertEqual([dep.name for dep in package.requires], ["zlib"])
        self.assertIn("%cmake", emit_spec(package))

    def test_dh_only_is_unemittable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            debian = root / "debian"
            debian.mkdir()
            (debian / "control").write_text(
                "Source: hello\n"
                "Build-Depends: debhelper-compat (= 13)\n"
                "\n"
                "Package: hello\n"
                "Architecture: any\n"
                "Description: Hello\n",
                encoding="utf-8",
            )
            (debian / "changelog").write_text(
                "hello (1.0-1) unstable; urgency=medium\n",
                encoding="utf-8",
            )
            (debian / "rules").write_text("#!/usr/bin/make -f\n%:\n\tdh $@\n", encoding="utf-8")
            package = import_dsc_tree(root)
        self.assertEqual(package.build_system, "dh")
        with self.assertRaises(SpecUnemittable):
            emit_spec(package)


class PrimaryTests(unittest.TestCase):
    def test_namespaced_primary(self) -> None:
        packages = parse_primary_xml(PRIMARY)
        self.assertEqual(len(packages), 1)
        package = packages[0]
        self.assertEqual(package.name, "widget")
        self.assertEqual(package.epoch, "")
        self.assertEqual(package.version, "1.2.0")
        self.assertEqual(package.license, "MIT")
        self.assertEqual(package.href, "Packages/w/widget-1.2.0-1.x86_64.rpm")
        self.assertIn("rpmlib(CompressedFileNames)", package.requires)

    def test_doctype_and_size_refused(self) -> None:
        with self.assertRaises(Refused) as caught:
            parse_primary_xml("<!DOCTYPE foo [<!ENTITY x 'y'>]><metadata/>")
        self.assertIn("doctype", str(caught.exception))
        with self.assertRaises(Refused):
            parse_primary_xml(PRIMARY, limit=20)

    def test_json_fixture(self) -> None:
        packages = load_repo((ROOT / "tests" / "fixtures" / "repo.json").read_text(encoding="utf-8"))
        self.assertEqual([item.name for item in packages], ["widget", "libwidget", "gcc"])


if __name__ == "__main__":
    unittest.main()
