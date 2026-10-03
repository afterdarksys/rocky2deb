"""Import a Debian source tree (debian/control + changelog) into Package IR."""

from __future__ import annotations

import re
from pathlib import Path

from rocky2deb.errors import Refused
from rocky2deb.ir import Dep, PackageIR, Subpackage

_CHANGELOG_RE = re.compile(r"^(\S+)\s+\(([^)]+)\)")


def _paragraphs(text: str) -> list[dict[str, str]]:
    blocks = re.split(r"\n\s*\n", text.strip())
    parsed: list[dict[str, str]] = []
    for block in blocks:
        fields: dict[str, str] = {}
        current = ""
        for line in block.splitlines():
            if line.startswith((" ", "\t")) and current:
                fields[current] = fields[current] + "\n" + line.strip()
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            current = key.strip().lower()
            fields[current] = value.strip()
        if fields:
            parsed.append(fields)
    return parsed


def _deps(text: str) -> list[Dep]:
    deps: list[Dep] = []
    for part in text.split(","):
        token = part.strip()
        if not token or token.startswith("$"):
            continue
        name = token.split()[0]
        if name.startswith("$"):
            continue
        deps.append(Dep(name))
    return deps


def sniff_build_system(rules: str, build_depends: str) -> str:
    blob = f"{rules}\n{build_depends}".lower()
    if "cmake" in blob:
        return "cmake"
    if "meson" in blob:
        return "meson"
    if "pybuild" in blob or "dh-python" in blob:
        return "python"
    # An explicit autotools buildsystem has a spec template. Plain `dh` does not.
    if "buildsystem=autotools" in blob or "buildsystem=autoconf" in blob:
        return "autotools"
    if re.search(r"\bdh\b", rules):
        return "dh"
    return "custom"


def _split_version(version: str) -> tuple[str, str, str]:
    epoch = ""
    rest = version
    if ":" in rest:
        epoch, rest = rest.split(":", 1)
    if "-" in rest:
        upstream, release = rest.rsplit("-", 1)
    else:
        upstream, release = rest, ""
    return epoch, upstream, release


def import_dsc_tree(root: Path) -> PackageIR:
    root = Path(root)
    control_path = root / "debian" / "control"
    changelog_path = root / "debian" / "changelog"
    if not control_path.is_file() or not changelog_path.is_file():
        raise Refused("debian/control and debian/changelog are required")
    paragraphs = _paragraphs(control_path.read_text(encoding="utf-8"))
    if len(paragraphs) < 2 or "source" not in paragraphs[0]:
        raise Refused("debian/control needs a source paragraph and a binary package")
    source = paragraphs[0]
    first = changelog_path.read_text(encoding="utf-8").splitlines()[0]
    match = _CHANGELOG_RE.match(first.strip())
    if not match:
        raise Refused("debian/changelog first line is not name (version)")
    epoch, version, release = _split_version(match.group(2))
    rules_path = root / "debian" / "rules"
    rules = rules_path.read_text(encoding="utf-8") if rules_path.is_file() else ""
    build_depends = source.get("build-depends", "")
    binary = paragraphs[1]
    description = binary.get("description", "")
    summary = description.splitlines()[0] if description else ""
    long = "\n".join(description.splitlines()[1:]).strip()
    subs = []
    for extra in paragraphs[2:]:
        extra_desc = extra.get("description", "")
        subs.append(
            Subpackage(
                name=extra.get("package", ""),
                summary=extra_desc.splitlines()[0] if extra_desc else "",
                description="\n".join(extra_desc.splitlines()[1:]).strip(),
                requires=_deps(extra.get("depends", "")),
            )
        )
    return PackageIR(
        name=match.group(1),
        version=version,
        release=release,
        epoch=epoch,
        license="",
        summary=summary,
        description=long,
        source_kind="dsc",
        source_id=str(root),
        build_system=sniff_build_system(rules, build_depends),
        build_requires=_deps(build_depends),
        requires=_deps(binary.get("depends", "")),
        subpackages=subs,
    )
