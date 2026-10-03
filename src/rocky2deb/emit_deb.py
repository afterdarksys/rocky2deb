"""Write a Debian source tree from Package IR.

The tree has debian/control, debian/rules, debian/copyright, and a quilt
series. debhelper compat 13 and newer is a Build-Depends constraint. Older
suites get a debian/compat file instead. EL library paths are rewritten with
the suite profile.
"""

from __future__ import annotations

from pathlib import Path

from rocky2deb.ir import Dep, PackageIR
from rocky2deb.names import deb_package_name, is_base, load_name_map
from rocky2deb.profiles import debian_path, get_profile

_CHANGELOG_DATE = "Sat, 03 Oct 2026 00:00:00 +0000"
_ARCH = {"x86_64": "amd64", "aarch64": "arm64", "noarch": "all", "i686": "i386"}


def debian_version(package: PackageIR) -> str:
    epoch = f"{package.epoch}:" if package.epoch not in ("", "0") else ""
    version = package.version or "0"
    release = package.release or "1"
    return f"{epoch}{version}-{release}"


def _deb_arch(arch: str) -> str:
    return _ARCH.get(arch, "any")


def _runtime_deps(deps: list[Dep], names: dict) -> list[str]:
    found: list[str] = []
    for dep in deps:
        if is_base(dep.name):
            continue
        if dep.name in names:
            found.append(names[dep.name].deb)
            continue
        found.append(deb_package_name(dep.name))
    return found


def _synopsis(summary: str, description: str, fallback: str) -> tuple[str, str]:
    short = (summary or "").strip() or fallback
    # Debian synopsis is a single line.
    short = short.splitlines()[0]
    body = (description or "").strip() or short
    return short, body


def _format_description(summary: str, description: str, fallback: str) -> str:
    short, body = _synopsis(summary, description, fallback)
    lines = [f"Description: {short}"]
    for line in body.splitlines():
        lines.append(" ." if not line.strip() else f" {line}")
    return "\n".join(lines)


def _control(package: PackageIR, suite: str, names: dict) -> str:
    profile = get_profile(suite)
    source = deb_package_name(package.name)
    build_deps = _runtime_deps(package.build_requires, names)
    if profile.debhelper_compat >= 13:
        helper = f"debhelper-compat (= {profile.debhelper_compat})"
    else:
        helper = f"debhelper (>= {profile.debhelper_compat})"
    build_line = ", ".join([helper, *build_deps]) if build_deps else helper
    standards = "4.7.0" if profile.debian_version >= 13 else "4.5.1"
    source_lines = [
        f"Source: {source}",
        "Section: misc",
        "Priority: optional",
        "Maintainer: rocky2deb <rocky2deb@localhost>",
        f"Build-Depends: {build_line}",
        f"Standards-Version: {standards}",
    ]
    if profile.debhelper_compat >= 13:
        source_lines.append("Rules-Requires-Root: no")
    blocks = ["\n".join(source_lines)]
    blocks.append(_binary_block(package.name, package.summary, package.description, package.arch, package.requires, names))
    for sub in package.subpackages:
        blocks.append(
            _binary_block(sub.name, sub.summary, sub.description, "noarch", sub.requires, names)
        )
    return "\n\n".join(blocks) + "\n"


def _binary_block(name, summary, description, arch, requires, names) -> str:
    deb = deb_package_name(name)
    depends = ["${shlibs:Depends}", "${misc:Depends}", *_runtime_deps(requires, names)]
    # Preserve order while dropping duplicates.
    seen: list[str] = []
    for item in depends:
        if item not in seen:
            seen.append(item)
    lines = [
        f"Package: {deb}",
        f"Architecture: {_deb_arch(arch)}",
        f"Depends: {', '.join(seen)}",
        _format_description(summary, description, deb),
    ]
    return "\n".join(lines)


_BUILD_SYSTEM = {
    "autotools": "autotools",
    "cmake": "cmake",
    "meson": "meson",
    "python": "pybuild",
}


def _rules(build_system: str) -> str:
    flag = _BUILD_SYSTEM.get(build_system, "")
    extra = f" --buildsystem={flag}" if flag else ""
    return f"#!/usr/bin/make -f\n%:\n\tdh $@{extra}\n"


def _copyright(package: PackageIR) -> str:
    source = package.url or "unknown"
    license_name = package.license or "unknown"
    return (
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        f"Upstream-Name: {package.name}\n"
        f"Source: {source}\n"
        "\n"
        "Files: *\n"
        "Copyright: upstream authors\n"
        f"License: {license_name}\n"
        " The Rocky spec declared this license. The upstream license text is in the\n"
        " source tarball named by Source.\n"
    )


def _changelog(package: PackageIR, suite: str) -> str:
    name = deb_package_name(package.name)
    version = debian_version(package)
    return (
        f"{name} ({version}) {suite}; urgency=medium\n"
        "\n"
        f"  * Import {package.identity()} from Rocky source.\n"
        "\n"
        f" -- rocky2deb <rocky2deb@localhost>  {_CHANGELOG_DATE}\n"
    )


def _install_lines(package_files, profile) -> str:
    lines = []
    for entry in package_files:
        if entry.ghost:
            continue
        lines.append(debian_path(entry.path, profile).lstrip("/"))
    return "\n".join(lines) + ("\n" if lines else "")


def _conffiles(package_files, profile) -> str:
    lines = [
        debian_path(entry.path, profile)
        for entry in package_files
        if entry.config and not entry.ghost
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def _readme(package: PackageIR) -> str:
    parts = [
        "debian/rules uses dh. The spec scripts below are recorded so the",
        "conversion can be reviewed. They are not executed on the controller.",
        "",
        "prep:",
        package.prep or "(none)",
        "",
        "build:",
        package.build or "(none)",
        "",
        "install:",
        package.install or "(none)",
        "",
    ]
    if package.warnings:
        parts.append("warnings:")
        parts.extend(f"- {item}" for item in package.warnings)
        parts.append("")
    return "\n".join(parts)


def emit_deb_tree(package: PackageIR, suite: str, dest: Path) -> Path:
    profile = get_profile(suite)
    names = load_name_map()
    name = deb_package_name(package.name)
    root = Path(dest) / name
    debian = root / "debian"
    (debian / "source").mkdir(parents=True, exist_ok=True)
    (debian / "control").write_text(_control(package, suite, names), encoding="utf-8")
    rules = debian / "rules"
    rules.write_text(_rules(package.build_system), encoding="utf-8")
    rules.chmod(0o755)
    (debian / "copyright").write_text(_copyright(package), encoding="utf-8")
    (debian / "changelog").write_text(_changelog(package, suite), encoding="utf-8")
    (debian / "source" / "format").write_text("3.0 (quilt)\n", encoding="utf-8")
    (debian / "README.source").write_text(_readme(package), encoding="utf-8")
    if profile.debhelper_compat < 13:
        (debian / "compat").write_text(f"{profile.debhelper_compat}\n", encoding="utf-8")
    install = _install_lines(package.files, profile)
    if install:
        (debian / f"{name}.install").write_text(install, encoding="utf-8")
    conffiles = _conffiles(package.files, profile)
    if conffiles:
        (debian / f"{name}.conffiles").write_text(conffiles, encoding="utf-8")
    for sub in package.subpackages:
        sub_name = deb_package_name(sub.name)
        sub_install = _install_lines(sub.files, profile)
        if sub_install:
            (debian / f"{sub_name}.install").write_text(sub_install, encoding="utf-8")
    if package.scripts.post:
        (debian / f"{name}.postinst").write_text(
            "#!/bin/sh\nset -e\n" + package.scripts.post.strip() + "\n",
            encoding="utf-8",
        )
    series = []
    for patch in package.patches:
        patch_name = patch.rsplit("/", 1)[-1]
        if not patch_name or "/" in patch_name or ".." in patch_name:
            continue
        series.append(patch_name)
    (debian / "patches").mkdir(exist_ok=True)
    (debian / "patches" / "series").write_text(
        ("\n".join(series) + "\n") if series else "",
        encoding="utf-8",
    )
    return root
