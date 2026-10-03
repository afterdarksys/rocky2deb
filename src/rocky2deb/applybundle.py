"""Copy a remapped bundle onto a Debian root.

Threats: a manifest path can escape the root with '..' or an absolute name,
and a symlink in the bundle can point at a file outside it. Those are refused.
File contents are not logged. / is refused unless the operator sets the live
flag. The copy is idempotent: a second run writes the same bytes.
"""

from __future__ import annotations

from pathlib import Path

from rocky2deb.applycheck import assert_debian_suite
from rocky2deb.errors import Refused


def assert_root(root: Path, *, allow_live: bool) -> Path:
    candidate = Path(root)
    if candidate.is_symlink():
        raise Refused("refusing symlink root")
    resolved = candidate.resolve()
    if resolved == Path("/").resolve() and not allow_live:
        raise Refused("refusing to write /")
    if not resolved.is_dir():
        raise Refused("root is not a directory")
    return resolved


def _member(root: Path, rel: str) -> Path:
    if not rel or rel.startswith("/") or "\\" in rel:
        raise Refused("refusing bundle path")
    parts = Path(rel).parts
    if any(part in {"..", ""} for part in parts):
        raise Refused("refusing bundle path")
    target = root.joinpath(*parts)
    if root.resolve() not in target.resolve().parents:
        raise Refused("refusing bundle path")
    return target


def apply_bundle(
    root: Path,
    suite: str,
    bundle: Path,
    os_release: str,
    *,
    allow_live: bool = False,
) -> list[str]:
    """Copy manifest members into root. Return the relative paths written."""
    assert_debian_suite(os_release, suite)
    dest_root = assert_root(root, allow_live=allow_live)
    source = Path(bundle)
    manifest = source / "manifest"
    if not manifest.is_file() or manifest.is_symlink():
        raise Refused("bundle needs a manifest")
    written: list[str] = []
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        rel = raw.strip()
        if not rel or rel.startswith("#"):
            continue
        src = _member(source, rel)
        if not src.is_file() or src.is_symlink():
            raise Refused("bundle member is missing")
        target = _member(dest_root, rel)
        data = src.read_bytes()
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        partial.write_bytes(data)
        partial.chmod(0o644)
        partial.replace(target)
        written.append(rel)
    return written
