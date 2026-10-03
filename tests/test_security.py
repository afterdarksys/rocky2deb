"""Download acceptance. A failed check must not create the destination."""

from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from pathlib import Path

from rocky2deb.buildcmd import assert_build_backend
from rocky2deb.errors import Refused
from rocky2deb.fetch import (
    accept_download,
    assert_https,
    read_limited,
    sha256_hex,
    store_download,
)


class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.keyring = self.root / "keyring.gpg"
        self.keyring.write_bytes(b"pinned")
        self.body = b"rpm-bytes"
        self.digest = sha256_hex(self.body)

    def test_success_replaces_partial(self) -> None:
        dest = self.root / "out" / "pkg.rpm"
        seen: list[list[str]] = []

        def runner(argv: list[str]) -> int:
            seen.append(argv)
            return 0

        store_download(
            dest,
            self.body,
            expected_sha256=self.digest.upper(),
            signature=b"SIG",
            keyring=self.keyring,
            runner=runner,
        )
        self.assertEqual(dest.read_bytes(), self.body)
        self.assertFalse(dest.with_name(dest.name + ".partial").exists())
        self.assertEqual(seen[0][0], "gpgv")
        self.assertIn(str(self.keyring), seen[0])

    def test_checksum_mismatch_writes_nothing(self) -> None:
        dest = self.root / "nested" / "pkg.rpm"
        with self.assertRaises(Refused) as caught:
            store_download(
                dest,
                self.body,
                expected_sha256="ab",
                signature=b"SIG",
                keyring=self.keyring,
                runner=lambda argv: 0,
            )
        self.assertEqual(str(caught.exception), "checksum mismatch")
        self.assertNotIn(self.body.decode(), str(caught.exception))
        self.assertFalse(dest.exists())
        self.assertFalse(dest.parent.exists())
        self.assertEqual(list(self.root.glob("*.partial")), [])

    def test_missing_signature_and_runner_failure(self) -> None:
        dest = self.root / "pkg.rpm"
        with self.assertRaises(Refused) as caught:
            accept_download(
                self.body,
                expected_sha256=self.digest,
                signature=b"",
                keyring=self.keyring,
            )
        self.assertEqual(str(caught.exception), "missing signature")
        with self.assertRaises(Refused) as caught:
            store_download(
                dest,
                self.body,
                expected_sha256=self.digest,
                signature=b"SIG",
                keyring=self.keyring,
                runner=lambda argv: 1,
            )
        self.assertEqual(str(caught.exception), "signature rejected by gpgv")
        self.assertFalse(dest.exists())
        missing = self.root / "absent.gpg"
        with self.assertRaises(Refused) as caught:
            store_download(
                dest,
                self.body,
                expected_sha256=self.digest,
                signature=b"SIG",
                keyring=missing,
                runner=lambda argv: 0,
            )
        self.assertEqual(str(caught.exception), "missing keyring")
        self.assertFalse(dest.exists())

    def test_oversize_and_limits(self) -> None:
        dest = self.root / "pkg.rpm"
        with self.assertRaises(Refused):
            store_download(
                dest,
                self.body,
                expected_sha256=self.digest,
                signature=b"SIG",
                keyring=self.keyring,
                runner=lambda argv: 0,
                limit=4,
            )
        self.assertFalse(dest.exists())
        with self.assertRaises(Refused) as caught:
            read_limited(io.BytesIO(b"abcdef"), limit=4)
        self.assertIn("exceeds 4 bytes", str(caught.exception))
        with self.assertRaises(Refused):
            accept_download(self.body, expected_sha256="", signature=b"SIG", keyring=self.keyring)

    def test_https_only(self) -> None:
        with self.assertRaises(Refused) as caught:
            assert_https("https://user:pass@example.com/pkg.src.rpm")
        self.assertEqual(str(caught.exception), "refusing URL with userinfo")
        self.assertNotIn("user:pass", str(caught.exception))
        with self.assertRaises(Refused):
            assert_https("http://example.com/pkg.src.rpm")

    def test_compare_digest_rejects_same_length_mismatch(self) -> None:
        other = "0" * 64
        self.assertEqual(len(other), len(hashlib.sha256(self.body).hexdigest()))
        with self.assertRaises(Refused):
            accept_download(
                self.body,
                expected_sha256=other,
                signature=b"SIG",
                keyring=self.keyring,
                runner=lambda argv: 0,
            )


class BuildTests(unittest.TestCase):
    def test_refuses_without_execute_and_when_present(self) -> None:
        with self.assertRaises(Refused) as caught:
            assert_build_backend("deb", False)
        self.assertEqual(str(caught.exception), "refusing to build without --execute")
        with self.assertRaises(Refused) as caught:
            assert_build_backend("deb", True, which=lambda _tool: None)
        self.assertIn("sbuild is not installed", str(caught.exception))
        found = assert_build_backend("rpm", True, which=lambda _tool: "/usr/bin/mock")
        self.assertEqual(found, "/usr/bin/mock")
        with self.assertRaises(Refused):
            assert_build_backend("alien", True)


if __name__ == "__main__":
    unittest.main()
