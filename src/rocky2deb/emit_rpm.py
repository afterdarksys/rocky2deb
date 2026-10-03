"""Emit an RPM spec from Package IR for build systems with a known template.

A Debian rules file that is only `dh`, or a custom script, is spec-unemittable.
emit_repack_spec writes a noarch source install for those systems. It does
not run debian/rules and it does not claim a template build ran.
"""

from __future__ import annotations

import re

from rocky2deb.errors import Refused, SpecUnemittable
from rocky2deb.ir import PackageIR
from rocky2deb.srpm_pack import refuse_spec_field

_SOURCE_NAME = re.compile(r"^[A-Za-z0-9._+-]+$")

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


def emit_repack_spec(
    *,
    name: str,
    version: str,
    release: str,
    license_text: str,
    summary: str,
    sources: list[str],
    epoch: str = "",
) -> str:
    """Write a noarch spec that installs the upstream tree and builds nothing.

    The ``cp`` line is spec text. This function does not run it.
    """
    if not sources:
        raise Refused("repack needs a source tarball")
    for value in (name, version, release, epoch):
        if value:
            refuse_spec_field(value, allow_space=False)
    if not name or not version or not release:
        raise Refused("refusing spec field")
    refuse_spec_field(summary, allow_space=True)
    refuse_spec_field(license_text, allow_space=True)
    for source in sources:
        if _SOURCE_NAME.fullmatch(source) is None:
            raise Refused("refusing source file")
    lines = [
        f"Name: {name}",
        f"Version: {version}",
        f"Release: {release}",
        f"Summary: {summary}",
        f"License: {license_text}",
        "BuildArch: noarch",
    ]
    if epoch not in ("", "0"):
        lines.append(f"Epoch: {epoch}")
    for index, source in enumerate(sources):
        lines.append(f"Source{index}: {source}")
    lines.extend(
        [
            "",
            "%description",
            "The upstream build system has no spec template. debian/rules is not run.",
            "",
            "%prep",
            "%autosetup",
            "",
            "%build",
            "",
            "%install",
            "mkdir -p %{buildroot}/usr/src/repack/%{name}",
            "cp -a . %{buildroot}/usr/src/repack/%{name}",
            "",
            "%files",
            "/usr/src/repack/%{name}",
            "",
        ]
    )
    text = "\n".join(lines)
    for banned in ("%configure", "%cmake", "%meson", "%py3_build"):
        if banned in text:
            raise Refused("refusing spec field")
    return text
