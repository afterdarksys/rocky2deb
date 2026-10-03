"""Pack, repack, dnf metadata, and Debian archive fetch. No network, rpm, or mock."""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from rocky2deb.d2r import repack_from_tree, resolve_deb, rocky_package_list
from rocky2deb.debian2rocky_cli import main as debian2rocky_main
from rocky2deb.debian_src import fetch_debian_source
from rocky2deb.errors import Refused
from rocky2deb.fetch import DEFAULT_LIMIT, checksums_match
from rocky2deb.names import load_name_map
from rocky2deb.primary import parse_primary_xml
from rocky2deb.repodata import write_rpm_repo
from rocky2deb.srpm import _LEAD, _skip_header
from rocky2deb.srpm_pack import pack_src_rpm, read_rpm_identity
from rocky2deb.srpm import extract_src_rpm

ROOT = Path(__file__).resolve().parents[1]


def run_d2r(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = debian2rocky_main(argv)
    return code, out.getvalue(), err.getvalue()


def _control(name: str = "hello") -> str:
    return (
        f"Source: {name}\nSection: devel\n\n"
        f"Package: {name}\nArchitecture: any\nDescription: hi\n long\n"
    )


def _changelog(name: str, version: str) -> str:
    return (
        f"{name} ({version}) unstable; urgency=low\n\n"
        "  * test\n\n"
        " -- rocky2deb <rocky2deb@localhost>  Sat, 03 Oct 2026 00:00:00 +0000\n"
    )


def _rules(kind: str = "autotools") -> str:
    if kind == "dh":
        body = "\tdh $@\n"
    elif kind == "custom":
        body = "\ttouch SHOULD_NOT_EXIST\n"
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


def _spec(*, name: str = "hello", epoch: str = "", release: str = "1") -> bytes:
    epoch_line = f"Epoch: {epoch}\n" if epoch else ""
    release_line = f"Release: {release}\n" if release else ""
    return (
        f"Name: {name}\n"
        "Version: 2.10\n"
        f"{release_line}"
        "Summary: hello < world\n"
        f"{epoch_line}"
        "License: MIT\n"
        "\n"
        "%description\n"
        "a packaged hello\n"
        "\n"
        "%build\n"
        "touch SHOULD_NOT_EXIST\n"
    ).encode()


def _header(blob: bytes, offset: int) -> tuple[list[int], int, int, bytes, dict[int, int]]:
    count = int.from_bytes(blob[offset + 8 : offset + 12], "big")
    store = int.from_bytes(blob[offset + 12 : offset + 16], "big")
    tags: list[int] = []
    fields: dict[int, int] = {}
    region_off = 0
    for index in range(count):
        at = offset + 16 + index * 16
        tag = int.from_bytes(blob[at : at + 4], "big")
        field_off = int.from_bytes(blob[at + 8 : at + 12], "big", signed=True)
        tags.append(tag)
        fields[tag] = field_off
        if index == 0:
            region_off = field_off
    data_at = offset + 16 + count * 16
    return tags, region_off, store, blob[data_at : data_at + store], fields


def _cstr(store: bytes, offset: int) -> str:
    end = store.find(b"\x00", offset)
    return store[offset:end].decode("ascii")


def _decide(name: str, **kwargs: object):
    pins = kwargs.pop("pins", set())
    return resolve_deb(
        name,
        index=kwargs.pop("index", {}),  # type: ignore[arg-type]
        names=load_name_map(),
        pins=pins,  # type: ignore[arg-type]
        license_text=str(kwargs.pop("license_text", "")),
        build_system=str(kwargs.pop("build_system", "")),
    )


class PackTests(unittest.TestCase):
    def test_round_trip_header_and_payload(self) -> None:
        stray = Path.cwd() / "SHOULD_NOT_EXIST"
        self.addCleanup(lambda: stray.unlink(missing_ok=True))
        spec = _spec()
        extra = b"upstream-bytes"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "hello-2.10-1.src.rpm"
            written = pack_src_rpm(spec, [("hello-2.10.tar.gz", extra)], dest)
            blob = written.read_bytes()
            self.assertEqual(blob[:4], b"\xed\xab\xee\xdb")
            self.assertEqual(blob[4], 3)
            self.assertEqual(blob[5], 0)
            self.assertEqual(int.from_bytes(blob[6:8], "big"), 1)
            self.assertEqual(int.from_bytes(blob[8:10], "big"), 1)
            self.assertEqual(int.from_bytes(blob[76:78], "big"), 1)
            self.assertEqual(int.from_bytes(blob[78:80], "big"), 5)
            self.assertTrue(blob[10:76].startswith(b"hello-2.10-1.src.rpm\x00"))
            sig_tags, sig_region, sig_store, sig_bytes, sig_fields = _header(blob, _LEAD)
            self.assertEqual(sig_tags, [62, 269, 273, 1000, 1004, 1007])
            self.assertGreater(sig_region, 0)
            self.assertEqual(sig_store % 8, 0)
            main_at = _skip_header(blob, _LEAD, DEFAULT_LIMIT)
            payload_at = _skip_header(blob, main_at, DEFAULT_LIMIT)
            main_tags, main_region, main_store, _main_bytes, _main_fields = _header(blob, main_at)
            self.assertEqual(
                main_tags,
                [
                    63, 100, 1000, 1001, 1002, 1004, 1005, 1006, 1007, 1009,
                    1014, 1016, 1021, 1022, 1044, 1124, 1125, 1126, 5062,
                ],
            )
            self.assertGreater(main_region, 0)
            self.assertEqual(main_store % 8, 0)
            self.assertEqual(payload_at, main_at + 16 + len(main_tags) * 16 + main_store)
            self.assertEqual(blob[payload_at : payload_at + 2], b"\x1f\x8b")
            main = blob[main_at:payload_at]
            self.assertTrue(
                checksums_match(_cstr(sig_bytes, sig_fields[273]), hashlib.sha256(main).hexdigest())
            )
            identity = read_rpm_identity(blob)
            self.assertEqual(identity.name, "hello")
            self.assertEqual(identity.version, "2.10")
            self.assertEqual(identity.release, "1")
            self.assertEqual(identity.epoch, "")
            self.assertEqual(identity.arch, "src")
            self.assertEqual(identity.license, "MIT")
            self.assertEqual(identity.summary, "hello < world")
            self.assertEqual(identity.sourcerpm, "")
            unpacked = Path(tmp) / "unpacked"
            names = extract_src_rpm(blob, unpacked)
            self.assertEqual(names, ["hello.spec", "hello-2.10.tar.gz"])
            spec_path = unpacked / "hello.spec"
            self.assertEqual(spec_path.read_bytes(), spec)
            self.assertEqual(spec_path.stat().st_mode & 0o777, 0o644)
            self.assertEqual((unpacked / "hello-2.10.tar.gz").read_bytes(), extra)
            self.assertIn(b"touch SHOULD_NOT_EXIST", spec_path.read_bytes())
            self.assertFalse(stray.exists())

    def test_epoch_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.src.rpm"
            blob = pack_src_rpm(_spec(epoch="2"), None, dest).read_bytes()
            identity = read_rpm_identity(blob)
            self.assertEqual(identity.epoch, "2")
            tags, *_rest = _header(blob, _skip_header(blob, _LEAD, DEFAULT_LIMIT))
            self.assertIn(1003, tags)

    def test_refusals_leave_the_destination_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing.src.rpm"
            with self.assertRaises(Refused) as ctx:
                pack_src_rpm(b"Name: hello\nVersion: 2.10\nSummary: hi\n", None, missing)
            self.assertEqual(str(ctx.exception), "missing spec field")
            self.assertFalse(missing.exists())
            percent = root / "percent.src.rpm"
            with self.assertRaises(Refused) as ctx:
                pack_src_rpm(_spec(name="hel%lo"), None, percent)
            self.assertEqual(str(ctx.exception), "refusing spec field")
            self.assertNotIn("hel%lo", str(ctx.exception))
            self.assertFalse(percent.exists())
            raw = root / "raw.src.rpm"
            with self.assertRaises(Refused) as ctx:
                pack_src_rpm(b"\xff", None, raw)
            self.assertEqual(str(ctx.exception), "spec is not utf-8")
            self.assertFalse(raw.exists())
            capped = root / "capped.src.rpm"
            with self.assertRaises(Refused) as ctx:
                pack_src_rpm(_spec(), None, capped, limit=32)
            self.assertEqual(str(ctx.exception), "input exceeds 32 bytes")
            self.assertFalse(capped.exists())
            kept = root / "kept.src.rpm"
            kept.write_bytes(b"kept")
            link = root / "link.src.rpm"
            link.symlink_to(kept)
            with self.assertRaises(Refused) as ctx:
                pack_src_rpm(_spec(), None, link)
            self.assertEqual(str(ctx.exception), "refusing symlink destination")
            self.assertEqual(kept.read_bytes(), b"kept")
            duplicate = root / "dup.src.rpm"
            with self.assertRaises(Refused) as ctx:
                pack_src_rpm(
                    _spec(),
                    [("hello-2.10.tar.gz", b"a"), ("hello-2.10.tar.gz", b"b")],
                    duplicate,
                )
            self.assertEqual(str(ctx.exception), "duplicate source file")
            self.assertFalse(duplicate.exists())
            zero = root / "zero.src.rpm"
            pack_src_rpm(_spec(), [], zero)
            self.assertEqual(extract_src_rpm(zero.read_bytes(), root / "zero"), ["hello.spec"])


class RepackTests(unittest.TestCase):
    def test_pin_matrix(self) -> None:
        pinned = {"hello"}
        omitted = _decide("hello", pins=pinned, license_text="MIT")
        self.assertEqual(omitted.action, "pin-rebuild")
        plain = _decide("hello", license_text="MIT", build_system="dh")
        self.assertNotEqual(plain.action, "repack")
        self.assertEqual(plain.action, "rebuild")
        repack = _decide("hello", pins=pinned, license_text="MIT", build_system="dh")
        self.assertEqual(repack.action, "repack")
        self.assertEqual(repack.rpm, "hello")
        self.assertEqual(repack.reason, "free license token")
        self.assertEqual(repack.license_class, "dfsg")
        custom = _decide("hello", pins=pinned, license_text="MIT", build_system="custom")
        self.assertEqual(custom.action, "repack")
        autotools = _decide("hello", pins=pinned, license_text="MIT", build_system="autotools")
        self.assertEqual(autotools.action, "pin-rebuild")
        folded = _decide("hello", pins=pinned, license_text="MIT", build_system="DH")
        self.assertEqual(folded.action, "pin-rebuild")
        blocked = _decide("hello", pins=pinned, license_text="Proprietary", build_system="dh")
        self.assertEqual(blocked.action, "gap")
        self.assertEqual(blocked.reason, "license unknown")
        base = _decide("libc6", pins={"libc6"}, license_text="MIT", build_system="dh")
        self.assertEqual(base.action, "base")
        self.assertEqual(base.rpm, "glibc")
        brand = _decide("ubuntu-mono", pins={"ubuntu-mono"}, license_text="MIT", build_system="dh")
        self.assertEqual(brand.action, "gap")
        self.assertEqual(brand.reason, "trademark")
        logos = _decide("rocky-logos", pins={"rocky-logos"}, license_text="MIT", build_system="dh")
        self.assertEqual(logos.action, "gap")
        self.assertEqual(logos.reason, "trademark")
        self.assertEqual(rocky_package_list([repack, blocked]), ["hello"])

    def test_repack_from_tree_refuses_before_a_spec(self) -> None:
        with self.assertRaises(Refused) as ctx:
            repack_from_tree(Path("/no/such-tree"), "MIT", pinned=False)
        self.assertEqual(str(ctx.exception), "repack requires a pin")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            autotools = _tree(root / "auto", "hello", "2.10-1", "autotools")
            with self.assertRaises(Refused) as ctx:
                repack_from_tree(autotools, "MIT", pinned=True)
            self.assertEqual(str(ctx.exception), "build system has a spec template")
            brand = _tree(root / "brand", "ubuntu-mono", "1.0-1", "dh")
            with self.assertRaises(Refused) as ctx:
                repack_from_tree(brand, "MIT", pinned=True)
            self.assertEqual(str(ctx.exception), "trademark")
            bare = _tree(root / "bare", "hello", "2.10-1", "dh")
            with self.assertRaises(Refused) as ctx:
                repack_from_tree(bare, "", pinned=True)
            self.assertEqual(str(ctx.exception), "missing license")
            with self.assertRaises(Refused) as ctx:
                repack_from_tree(bare, "Proprietary", pinned=True)
            self.assertEqual(str(ctx.exception), "license unknown")
            with self.assertRaises(Refused) as ctx:
                repack_from_tree(bare, "MIT", pinned=True)
            self.assertEqual(str(ctx.exception), "repack needs a source tarball")
            link = bare / "hello-2.10.tar.gz"
            link.symlink_to(bare / "debian" / "control")
            with self.assertRaises(Refused) as ctx:
                repack_from_tree(bare, "MIT", pinned=True)
            self.assertEqual(str(ctx.exception), "refusing symlink source")

    def test_repack_spec_does_not_run_rules(self) -> None:
        stray = Path.cwd() / "SHOULD_NOT_EXIST"
        self.addCleanup(lambda: stray.unlink(missing_ok=True))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = _tree(root / "src", "hello", "2.10-1", "custom")
            (tree / "hello-2.10.tar.gz").write_bytes(b"tar")
            text = repack_from_tree(tree, "MIT", pinned=True)
            self.assertIn("BuildArch: noarch", text)
            self.assertIn("Source0: hello-2.10.tar.gz", text)
            self.assertIn("%autosetup", text)
            self.assertIn("cp -a . %{buildroot}/usr/src/repack/%{name}", text)
            self.assertIn("debian/rules is not run.", text)
            for banned in ("%configure", "%cmake", "%meson", "%py3_build"):
                self.assertNotIn(banned, text)
            self.assertFalse(stray.exists())
            self.assertFalse((root / "usr").exists())


class FetchTests(unittest.TestCase):
    def _files(self) -> tuple[dict[str, bytes], bytes]:
        changelog = _changelog("hello", "2.10-3ubuntu1").encode()
        debian = _tar_xz(
            [
                ("debian/control", _control().encode(), 0o644),
                ("debian/changelog", changelog, 0o644),
                ("debian/rules", _rules("autotools").encode(), 0o755),
            ]
        )
        files = {
            "hello_2.10-3ubuntu1.dsc": b"BOGUS-DOWNLOADED-DSC\n",
            "hello_2.10.orig.tar.gz": b"orig-bytes",
            "hello_2.10-3ubuntu1.debian.tar.xz": debian,
        }
        return files, changelog

    def _keyring(self, root: Path) -> Path:
        path = root / "keyring.gpg"
        path.write_bytes(b"not-a-real-keyring")
        return path

    def test_plan_keeps_the_archive_version(self) -> None:
        files, _changelog_bytes = self._files()

        def opener(*_args: object) -> tuple[bytes, str]:
            raise AssertionError("opener")

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out"
            plan = fetch_debian_source(
                _sources(list(files.items()), bad_md5=True),
                "hello",
                suite="trixie",
                execute=False,
                dest=dest,
                opener=opener,
            )
            self.assertEqual(plan.version, "2.10-3ubuntu1")
            self.assertFalse(dest.exists())

    def test_execute_copies_debian_and_orig_without_rewrite(self) -> None:
        stray = Path.cwd() / "SHOULD_NOT_EXIST"
        self.addCleanup(lambda: stray.unlink(missing_ok=True))
        files, changelog = self._files()
        calls: list[str] = []

        def opener(url: str, timeout: int, limit: int) -> tuple[bytes, str]:
            calls.append(url)
            return files[url.rsplit("/", 1)[-1]], url

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "out"
            plan = fetch_debian_source(
                _sources(list(files.items()), bad_md5=True),
                "hello",
                suite="stretch",
                execute=True,
                license_text="MIT",
                signature=b"sig",
                keyring=self._keyring(root),
                dest=dest,
                opener=opener,
                runner=lambda _argv: 0,
            )
            self.assertEqual(plan.version, "2.10-3ubuntu1")
            self.assertTrue(calls)
            self.assertTrue(calls[0].startswith("https://archive.debian.org/debian/"))
            self.assertFalse(stray.exists())
            self.assertEqual((dest / "debian" / "changelog").read_bytes(), changelog)
            self.assertNotIn(b"~trixie", changelog)
            self.assertNotIn(b"~el", changelog)
            self.assertEqual((dest / "debian" / "rules").stat().st_mode & 0o777, 0o755)
            self.assertEqual((dest / "hello_2.10.orig.tar.gz").read_bytes(), b"orig-bytes")
            names = {path.name for path in dest.rglob("*") if path.is_file()}
            self.assertNotIn("hello_2.10-3ubuntu1.dsc", names)
            self.assertFalse(any(name.endswith(".debian.tar.xz") for name in names))
            self.assertIn("touch SHOULD_NOT_EXIST", (dest / "debian" / "rules").read_text(encoding="utf-8"))

    def test_refusals_stop_before_a_download(self) -> None:
        files, _changelog_bytes = self._files()
        calls: list[str] = []

        def opener(url: str, timeout: int, limit: int) -> tuple[bytes, str]:
            calls.append(url)
            return files[url.rsplit("/", 1)[-1]], url

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "out"
            keyring = self._keyring(root)
            with self.assertRaises(Refused) as ctx:
                fetch_debian_source(
                    _sources(list(files.items())),
                    "hello",
                    suite="bookworm",
                    execute=True,
                    license_text="MIT",
                    signature=b"sig",
                    keyring=keyring,
                    dest=dest,
                    archive="https://user:s3cret-token@archive.debian.org/debian",
                    opener=opener,
                    runner=lambda _argv: 0,
                )
            self.assertEqual(str(ctx.exception), "refusing URL with userinfo")
            self.assertNotIn("s3cret-token", str(ctx.exception))
            self.assertEqual(calls, [])
            self.assertFalse(dest.exists())
            with self.assertRaises(Refused) as ctx:
                fetch_debian_source(
                    _sources(list(files.items()), package="libc6", binary="libc6"),
                    "libc6",
                    suite="bookworm",
                    execute=True,
                    license_text="MIT",
                    signature=b"sig",
                    keyring=keyring,
                    dest=dest,
                    opener=opener,
                    runner=lambda _argv: 0,
                )
            self.assertEqual(str(ctx.exception), "base package")
            with self.assertRaises(Refused) as ctx:
                fetch_debian_source(
                    _sources(list(files.items()), binary="ubuntu-mono"),
                    "hello",
                    suite="bookworm",
                    execute=True,
                    license_text="MIT",
                    signature=b"sig",
                    keyring=keyring,
                    dest=dest,
                    opener=opener,
                    runner=lambda _argv: 0,
                )
            self.assertEqual(str(ctx.exception), "trademark")
            with self.assertRaises(Refused) as ctx:
                fetch_debian_source(
                    _sources(list(files.items())),
                    "hello",
                    suite="bookworm",
                    execute=True,
                    license_text="MIT",
                    signature=b"sig",
                    keyring=keyring,
                    dest=dest,
                    opener=opener,
                    runner=lambda _argv: 1,
                )
            self.assertEqual(str(ctx.exception), "signature rejected by gpgv")
            debian_only = {
                "hello_2.10-3ubuntu1.debian.tar.xz": files["hello_2.10-3ubuntu1.debian.tar.xz"]
            }
            with self.assertRaises(Refused) as ctx:
                fetch_debian_source(
                    _sources(list(debian_only.items())),
                    "hello",
                    suite="bookworm",
                    execute=True,
                    license_text="MIT",
                    signature=b"sig",
                    keyring=keyring,
                    dest=dest,
                    opener=opener,
                    runner=lambda _argv: 0,
                )
            self.assertEqual(str(ctx.exception), "source tarball missing")
            link = root / "out-link"
            link.symlink_to(root / "elsewhere")
            with self.assertRaises(Refused) as ctx:
                fetch_debian_source(
                    _sources(list(files.items())),
                    "hello",
                    suite="bookworm",
                    execute=True,
                    license_text="MIT",
                    signature=b"sig",
                    keyring=keyring,
                    dest=link,
                    opener=opener,
                    runner=lambda _argv: 0,
                )
            self.assertEqual(str(ctx.exception), "refusing symlink destination")
            key_link = root / "keyring-link"
            key_link.symlink_to(keyring)
            with self.assertRaises(Refused) as ctx:
                fetch_debian_source(
                    _sources(list(files.items())),
                    "hello",
                    suite="bookworm",
                    execute=True,
                    license_text="MIT",
                    signature=b"sig",
                    keyring=key_link,
                    dest=root / "other",
                    opener=opener,
                    runner=lambda _argv: 0,
                )
            self.assertEqual(str(ctx.exception), "missing keyring")
            huge = (
                "Package: hello\nBinary: hello\nVersion: 2.10-3ubuntu1\n"
                "Directory: pool/main/h/hello\nChecksums-Sha256:\n "
                + "ab" * 32
                + f" {DEFAULT_LIMIT + 1} hello_2.10.orig.tar.gz\n "
                + "cd" * 32
                + f" {DEFAULT_LIMIT + 1} hello_2.10-3ubuntu1.debian.tar.xz\n"
            )
            with self.assertRaises(Refused) as ctx:
                fetch_debian_source(
                    huge.encode(),
                    "hello",
                    suite="bookworm",
                    execute=True,
                    license_text="MIT",
                    signature=b"sig",
                    keyring=keyring,
                    dest=root / "huge",
                    opener=opener,
                    runner=lambda _argv: 0,
                )
            self.assertEqual(str(ctx.exception), "checksum mismatch")
            self.assertEqual(calls, [])
            self.assertFalse((root / "huge").exists())
            self.assertFalse(dest.exists())


class RepoTests(unittest.TestCase):
    def _rpm(self, root: Path, *, epoch: str = "") -> Path:
        path = root / "hello-2.10-1.src.rpm"
        pack_src_rpm(_spec(epoch=epoch), None, path)
        return path

    def test_primary_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpm = self._rpm(root / "in")
            (root / "in" / "notes.txt").write_text("skip", encoding="utf-8")
            dest = root / "repo"
            write_rpm_repo(root / "in", dest)
            xml = gzip.decompress((dest / "repodata" / "primary.xml.gz").read_bytes()).decode()
            self.assertIn("&lt;", xml)
            self.assertNotIn("notes.txt", xml)
            summary = ET.fromstring(xml).find(".//{http://linux.duke.edu/metadata/common}summary")
            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(summary.text, "hello < world")
            packages = parse_primary_xml(xml)
            self.assertEqual(len(packages), 1)
            package = packages[0]
            self.assertEqual(package.name, "hello")
            self.assertEqual(package.arch, "src")
            self.assertEqual(package.version, "2.10")
            self.assertEqual(package.release, "1")
            self.assertEqual(package.epoch, "")
            self.assertTrue(checksums_match(package.checksum, hashlib.sha256(rpm.read_bytes()).hexdigest()))
            self.assertEqual(package.href, "Packages/hello-2.10-1.src.rpm")
            self.assertEqual(package.license, "MIT")
            self.assertEqual(package.sourcerpm, "")
            self.assertEqual((dest / "Packages" / rpm.name).read_bytes(), rpm.read_bytes())
            self.assertEqual((dest / "Packages" / rpm.name).stat().st_mode & 0o777, 0o644)

    def test_epoch_and_empty_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._rpm(root / "in", epoch="2")
            dest = root / "repo"
            write_rpm_repo(root / "in", dest)
            xml = gzip.decompress((dest / "repodata" / "primary.xml.gz").read_bytes()).decode()
            package = parse_primary_xml(xml)[0]
            self.assertEqual(package.epoch, "2")
            empty = root / "empty"
            empty.mkdir()
            bare = root / "bare"
            write_rpm_repo(empty, bare)
            bare_xml = gzip.decompress((bare / "repodata" / "primary.xml.gz").read_bytes()).decode()
            self.assertIn('packages="0"', bare_xml)
            self.assertEqual(parse_primary_xml(bare_xml), [])

    def test_refusals_do_not_create_dest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "in"
            source.mkdir()
            real = self._rpm(root / "stage")
            link = source / "hello-2.10-1.src.rpm"
            link.symlink_to(real)
            dest = root / "repo"
            with self.assertRaises(Refused) as ctx:
                write_rpm_repo(source, dest)
            self.assertEqual(str(ctx.exception), "refusing symlink source")
            self.assertFalse(dest.exists())
            source.iterdir()
            link.unlink()
            huge = source / "big.rpm"
            huge.write_bytes(b"x" * 101)
            with self.assertRaises(Refused) as ctx:
                write_rpm_repo(source, dest, limit=100)
            self.assertEqual(str(ctx.exception), "input exceeds 100 bytes")
            self.assertFalse(dest.exists())
            huge.unlink()
            (source / "bad.rpm").write_bytes(b"not-an-rpm")
            with self.assertRaises(Refused) as ctx:
                write_rpm_repo(source, dest)
            self.assertEqual(str(ctx.exception), "rpm header magic is wrong")
            self.assertFalse(dest.exists())


class FinishCliTests(unittest.TestCase):
    def test_pack_repack_publish_and_resolve(self) -> None:
        stray = Path.cwd() / "SHOULD_NOT_EXIST"
        self.addCleanup(lambda: stray.unlink(missing_ok=True))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = root / "hello.spec"
            spec.write_bytes(_spec())
            source_a = root / "a"
            source_b = root / "b"
            source_a.mkdir()
            source_b.mkdir()
            (source_a / "hello-2.10.tar.gz").write_bytes(b"one")
            (source_b / "hello-2.10.tar.gz").write_bytes(b"two")
            packed = root / "out.src.rpm"
            code, out, err = run_d2r(
                [
                    "pack",
                    "--spec",
                    str(spec),
                    "--source",
                    str(source_a / "hello-2.10.tar.gz"),
                    "--output",
                    str(packed),
                ]
            )
            self.assertEqual(code, 0, err)
            self.assertEqual(out.strip(), str(packed))
            self.assertIn(b"touch SHOULD_NOT_EXIST", extract_ready(packed, root))
            self.assertFalse(stray.exists())
            code, _out, err = run_d2r(
                ["pack", "--spec", str(root / "ZEBRA-spec"), "--output", str(root / "no.src.rpm")]
            )
            self.assertEqual(code, 3)
            self.assertIn("spec is missing", err)
            self.assertNotIn("ZEBRA", err)
            link = root / "ZEBRA-source.tar.gz"
            link.symlink_to(source_a / "hello-2.10.tar.gz")
            code, _out, err = run_d2r(
                ["pack", "--spec", str(spec), "--source", str(link), "--output", str(root / "no2.src.rpm")]
            )
            self.assertEqual(code, 3)
            self.assertIn("refusing symlink source", err)
            self.assertNotIn("ZEBRA", err)
            code, _out, err = run_d2r(
                [
                    "pack",
                    "--spec",
                    str(spec),
                    "--source",
                    str(source_a / "hello-2.10.tar.gz"),
                    "--source",
                    str(source_b / "hello-2.10.tar.gz"),
                    "--output",
                    str(root / "dup.src.rpm"),
                ]
            )
            self.assertEqual(code, 3)
            self.assertIn("duplicate source file", err)
            self.assertFalse((root / "dup.src.rpm").exists())
            tree = _tree(root / "tree", "hello", "2.10-1", "dh")
            (tree / "hello-2.10.tar.gz").write_bytes(b"tar")
            spec_out = root / "repack.spec"
            code, out, err = run_d2r(
                [
                    "repack",
                    "--pin",
                    "--tree",
                    str(tree),
                    "--license",
                    "MIT",
                    "--output",
                    str(spec_out),
                ]
            )
            self.assertEqual(code, 0, err)
            self.assertIn("BuildArch: noarch", spec_out.read_text(encoding="utf-8"))
            self.assertEqual(spec_out.stat().st_mode & 0o777, 0o644)
            code, _out, err = run_d2r(
                ["repack", "--tree", str(root / "ZEBRA-tree"), "--output", str(root / "no.spec")]
            )
            self.assertEqual(code, 3)
            self.assertIn("repack requires a pin", err)
            self.assertNotIn("ZEBRA", err)
            rpms = root / "rpms"
            rpms.mkdir()
            pack_src_rpm(_spec(), None, rpms / "hello-2.10-1.src.rpm")
            repo = root / "repo"
            code, out, err = run_d2r(["publish", "--rpms", str(rpms), "--output", str(repo)])
            self.assertEqual(code, 0, err)
            self.assertTrue((repo / "repodata" / "primary.xml.gz").is_file())
            code, _out, err = run_d2r(
                ["publish", "--rpms", str(root / "ZEBRA-rpms"), "--output", str(root / "no-repo")]
            )
            self.assertEqual(code, 3)
            self.assertIn("rpm directory is missing", err)
            self.assertNotIn("ZEBRA", err)
            code, out, err = run_d2r(
                ["resolve", "hello", "--pin", "hello", "--license", "MIT", "--build-system", "dh"]
            )
            self.assertEqual(code, 0, err)
            self.assertIn("repack", out)
            code, out, err = run_d2r(
                ["resolve", "hello", "--pin", "hello", "--license", "MIT"]
            )
            self.assertEqual(code, 0, err)
            self.assertIn("pin-rebuild", out)
            self.assertNotIn("repack", out)

    def test_fetch_source_refusals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = root / "Sources"
            sources.write_bytes(b"Package: hello\n")
            code, _out, err = run_d2r(
                [
                    "fetch-source",
                    "--suite",
                    "stonking",
                    "--sources",
                    str(sources),
                    "--package",
                    "hello",
                    "--archive",
                    "https://user:s3cret-token@archive.debian.org/debian",
                ]
            )
            self.assertEqual(code, 3)
            self.assertIn("refusing URL with userinfo", err)
            self.assertNotIn("s3cret-token", err)
            self.assertNotIn("without --execute", err)
            self.assertNotIn("stonking", err)
            code, _out, err = run_d2r(
                [
                    "fetch-source",
                    "--suite",
                    "stonking",
                    "--sources",
                    str(sources),
                    "--package",
                    "hello",
                    "--execute",
                ]
            )
            self.assertEqual(code, 3)
            self.assertIn("unknown Debian suite 'stonking'", err)
            self.assertNotIn("without --execute", err)
            code, _out, err = run_d2r(
                [
                    "fetch-source",
                    "--suite",
                    "trixie",
                    "--sources",
                    str(sources),
                    "--package",
                    "hello",
                ]
            )
            self.assertEqual(code, 3)
            self.assertIn("refusing to fetch without --execute", err)
            link = root / "ZEBRA-sources"
            link.symlink_to(sources)
            code, _out, err = run_d2r(
                [
                    "fetch-source",
                    "--execute",
                    "--suite",
                    "trixie",
                    "--sources",
                    str(link),
                    "--package",
                    "hello",
                ]
            )
            self.assertEqual(code, 3)
            self.assertIn("missing sources", err)
            self.assertNotIn("ZEBRA", err)


def extract_ready(packed: Path, root: Path) -> bytes:
    dest = root / "cli-unpacked"
    extract_src_rpm(packed.read_bytes(), dest)
    return (dest / "hello.spec").read_bytes()
