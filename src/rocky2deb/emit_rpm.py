"""Emit an RPM spec from Package IR for build systems with a known template.

A Debian rules file that is only `dh`, or a custom script, is spec-unemittable.
The planner must not select a binary repack unless the operator pins it.
"""

from __future__ import annotations

from rocky2deb.errors import SpecUnemittable
from rocky2deb.ir import PackageIR

_BUILD = {
    "cmake": ("%cmake\n%cmake_build", "%cmake_install"),
    "meson": ("%meson\n%meson_build", "%meson_install"),
    "python": ("%py3_build", "%py3_install"),
    "autotools": ("%configure\n%make_build", "%make_install"),
}


def emit_spec(package: PackageIR) -> str:
    system = package.build_system
    if system not in _BUILD:
        raise SpecUnemittable(
            f"{package.name} build system {system!r} has no spec template"
        )
    build, install = _BUILD[system]
    lines = [
        f"Name: {package.name}",
        f"Version: {package.version or '0'}",
        f"Release: {package.release or '1'}",
        f"Summary: {package.summary or package.name}",
        f"License: {package.license or 'unknown'}",
    ]
    if package.epoch not in ("", "0"):
        lines.append(f"Epoch: {package.epoch}")
    if package.url:
        lines.append(f"URL: {package.url}")
    for source in package.sources:
        lines.append(f"Source0: {source}")
    lines.extend(["", "%description", package.description or package.summary or package.name, ""])
    lines.extend(["%prep", "%autosetup", "", "%build", build, "", "%install", install, ""])
    file_lines = [entry.path for entry in package.files if entry.path]
    lines.append("%files")
    if file_lines:
        lines.extend(file_lines)
    else:
        lines.append("# warning: no files recorded")
    lines.append("")
    return "\n".join(lines)
