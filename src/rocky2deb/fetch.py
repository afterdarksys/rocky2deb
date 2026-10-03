"""Accept a downloaded SRPM or repo file only after checksum and signature checks.

Threats: stops a tampered SRPM or repo metadata blob from being stored and
later built. Does not protect against a compromised pinned keyring, a
malicious but correctly signed upstream, or a hostile spec once a chroot
builds it. This module never executes package scripts and does not implement
signature cryptography; gpgv is the verifier.

Checksums are compared with hmac.compare_digest. A failed check leaves the
destination uncreated.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from rocky2deb.errors import Refused

READ_BLOCK = 64 * 1024
DEFAULT_LIMIT = 64 * 1024 * 1024
MAX_TIMEOUT = 120

Runner = Callable[[list[str]], int]
Opener = Callable[[str, int, int], tuple[bytes, str]]


def checksums_match(actual: str, expected: str) -> bool:
    left = actual.strip().lower().encode()
    right = expected.strip().lower().encode()
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def assert_https(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password:
        raise Refused("refusing URL with userinfo")
    if parsed.scheme != "https" or not parsed.hostname:
        raise Refused("refusing URL that is not https with a host")
    return url


def read_limited(source, limit: int = DEFAULT_LIMIT) -> bytes:
    if limit < 1:
        raise Refused("size limit must be positive")
    chunks: list[bytes] = []
    total = 0
    while True:
        block = source.read(READ_BLOCK)
        if not block:
            break
        total += len(block)
        if total > limit:
            raise Refused(f"input exceeds {limit} bytes")
        chunks.append(block)
    return b"".join(chunks)


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _gpgv(argv: list[str]) -> int:
    completed = subprocess.run(argv, check=False, capture_output=True)
    return completed.returncode


def verify_detached(
    payload: bytes,
    signature: bytes,
    keyring: Path,
    runner: Runner | None = None,
) -> None:
    if not signature:
        raise Refused("missing signature")
    keyring_path = Path(keyring)
    if not keyring_path.is_file():
        raise Refused("missing keyring")
    verify = runner or _gpgv
    with tempfile.TemporaryDirectory(prefix="rocky2deb-gpg-") as tmp:
        body_path = Path(tmp) / "payload"
        sig_path = Path(tmp) / "payload.asc"
        body_path.write_bytes(payload)
        sig_path.write_bytes(signature)
        argv = ["gpgv", "--keyring", str(keyring_path), str(sig_path), str(body_path)]
        if verify(argv) != 0:
            raise Refused("signature rejected by gpgv")


def accept_download(
    body: bytes,
    *,
    expected_sha256: str,
    signature: bytes,
    keyring: Path,
    runner: Runner | None = None,
) -> bytes:
    if not expected_sha256:
        raise Refused("missing checksum")
    if not checksums_match(sha256_hex(body), expected_sha256):
        raise Refused("checksum mismatch")
    verify_detached(body, signature, keyring, runner)
    return body


def store_download(
    dest: Path,
    body: bytes,
    *,
    expected_sha256: str,
    signature: bytes,
    keyring: Path,
    runner: Runner | None = None,
    limit: int | None = None,
) -> None:
    """Write dest only after accept_download returns. Failures leave dest absent."""
    if limit is not None and len(body) > limit:
        raise Refused(f"input exceeds {limit} bytes")
    accepted = accept_download(
        body,
        expected_sha256=expected_sha256,
        signature=signature,
        keyring=keyring,
        runner=runner,
    )
    destination = Path(dest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    partial.write_bytes(accepted)
    os.replace(partial, destination)


def _open_https(url: str, timeout: int, limit: int) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "rocky2deb"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final = response.geturl()
            assert_https(final)
            return read_limited(response, limit), final
    except Refused:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Refused("download failed") from exc


def download_https(
    url: str,
    *,
    timeout: int = 60,
    limit: int = DEFAULT_LIMIT,
    opener: Opener | None = None,
) -> tuple[bytes, str]:
    """Read an https URL with the default certificate store. Return body and final URL."""
    assert_https(url)
    if timeout < 1 or timeout > MAX_TIMEOUT:
        raise Refused("timeout out of range")
    if limit < 1:
        raise Refused("size limit must be positive")
    if opener is None:
        return _open_https(url, timeout, limit)
    body, final = opener(url, timeout, limit)
    assert_https(final)
    if len(body) > limit:
        raise Refused(f"input exceeds {limit} bytes")
    return body, final


def fetch_verified(
    url: str,
    dest: Path,
    *,
    expected_sha256: str,
    signature: bytes,
    keyring: Path,
    timeout: int = 60,
    limit: int = DEFAULT_LIMIT,
    opener: Opener | None = None,
    runner: Runner | None = None,
) -> None:
    """Download, then store only when the checksum and gpgv signature match."""
    body, _final = download_https(url, timeout=timeout, limit=limit, opener=opener)
    store_download(
        dest,
        body,
        expected_sha256=expected_sha256,
        signature=signature,
        keyring=keyring,
        runner=runner,
        limit=limit,
    )
