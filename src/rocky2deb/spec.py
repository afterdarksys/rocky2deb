"""Import a Rocky RPM spec into Package IR.

The parser covers the preamble, script sections, and %files tags this project
emits. %if 0 is skipped. %ifarch is kept and recorded as a warning. A %files
path that contains a '..' component is refused.
"""

from __future__ import annotations

import re

from rocky2deb.errors import Refused
from rocky2deb.ir import Dep, FileEntry, PackageIR, Scriptlets

_SEED = {
    "_sysconfdir": "/etc",
    "_bindir": "/usr/bin",
    "_sbindir": "/usr/sbin",
    "_libdir": "/usr/lib64",
    "_libexecdir": "/usr/libexec",
    "_unitdir": "/usr/lib/systemd/system",
    "_datadir": "/usr/share",
    "_prefix": "/usr",
    "_mandir": "/usr/share/man",
    "_docdir": "/usr/share/doc",
    "_usr": "/usr",
    "_var": "/var",
}

_SECTIONS = (
    "description",
    "package",
    "prep",
    "build",
    "install",
    "check",
    "files",
    "preun",
    "postun",
    "pre",
    "post",
    "changelog",
)

_DEP_RE = re.compile(r"^(\S+)\s*(>=|<=|=>|=|>|<)\s*(\S+)$")


def _expand(text: str, defines: dict[str, str], fields: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        body = match.group(1)
        optional = body.startswith("?")
        if optional:
            body = body[1:]
        extra = ""
        name = body
        if optional and ":" in body:
            name, extra = body.split(":", 1)
        if name in defines and defines[name] != "":
            return defines[name] + (extra if optional else "")
        if name in fields and fields[name]:
            return fields[name]
        if optional or name in defines:
            return ""
        return match.group(0)

    return re.sub(r"%\{([^}]+)\}", repl, text)


def _section(line: str) -> str | None:
    for name in _SECTIONS:
        if line == f"%{name}" or line.startswith(f"%{name} ") or line.startswith(f"%{name}\t"):
            return name
    return None


def _parse_dep(text: str) -> Dep:
    stripped = text.strip()
    match = _DEP_RE.match(stripped)
    if match:
        return Dep(match.group(1), match.group(2), match.group(3))
    return Dep(stripped)


def _new_bucket() -> dict:
    return {
        "summary": "",
        "description": "",
        "requires": [],
        "provides": [],
        "conflicts": [],
        "files": [],
        "scripts": Scriptlets(),
    }


def _script_field(kind: str) -> str:
    return {"pre": "pre", "post": "post", "preun": "preun", "postun": "postun"}[kind]


def _parse_file(line: str, defines: dict[str, str], fields: dict[str, str]) -> FileEntry:
    config = noreplace = doc = license_file = ghost = is_dir = False
    mode = user = group = ""
    rest = line.strip()
    while rest.startswith("%"):
        if rest.startswith("%config(noreplace)"):
            config = noreplace = True
            rest = rest[len("%config(noreplace)") :].strip()
        elif rest.startswith("%config"):
            config = True
            rest = rest[len("%config") :].strip()
        elif rest.startswith("%attr("):
            end = rest.find(")")
            if end < 0:
                raise Refused(f"unclosed %attr in {line!r}")
            inside = rest[len("%attr(") : end]
            bits = [part.strip() for part in inside.split(",")]
            mode = bits[0] if bits else ""
            user = bits[1] if len(bits) > 1 else ""
            group = bits[2] if len(bits) > 2 else ""
            rest = rest[end + 1 :].strip()
        elif rest.startswith("%dir"):
            is_dir = True
            rest = rest[len("%dir") :].strip()
        elif rest.startswith("%doc"):
            doc = True
            rest = rest[len("%doc") :].strip()
        elif rest.startswith("%license"):
            license_file = True
            rest = rest[len("%license") :].strip()
        elif rest.startswith("%ghost"):
            ghost = True
            rest = rest[len("%ghost") :].strip()
        else:
            break
    path = _expand(rest, defines, fields).strip()
    if not path:
        raise Refused("empty %files path")
    if ".." in path.split("/"):
        raise Refused(f"refusing %files path {path}")
    return FileEntry(
        path=path,
        config=config,
        noreplace=noreplace,
        doc=doc,
        license_file=license_file,
        ghost=ghost,
        is_dir=is_dir,
        mode=mode,
        user=user,
        group=group,
    )


def _sniff_build(prep: str, build: str) -> str:
    blob = f"{prep}\n{build}"
    if "%cmake" in blob or "cmake" in blob:
        return "cmake"
    if "%meson" in blob or "meson" in blob:
        return "meson"
    if "%py3_" in blob or "pyproject" in blob:
        return "python"
    if "%configure" in blob or "%autosetup" in blob:
        return "autotools"
    return "custom"


def parse_spec(text: str, source_id: str = "") -> PackageIR:
    defines = dict(_SEED)
    fields = {"name": "", "version": "", "release": "", "epoch": ""}
    main = _new_bucket()
    subs: dict[str, dict] = {}
    short_to_full: dict[str, str] = {}
    warnings: list[str] = []
    sources: list[str] = []
    patches: list[str] = []
    license_text = ""
    url = ""
    arch = "x86_64"
    prep: list[str] = []
    build: list[str] = []
    install: list[str] = []
    check: list[str] = []
    section = "preamble"
    current = main
    file_target = main
    script_target = main
    skip_depth = 0
    ifarch_warned = False
    desc_lines: list[str] = []
    desc_bucket = main

    def finish_description() -> None:
        if section == "description":
            desc_bucket["description"] = "\n".join(desc_lines).strip()
            desc_lines.clear()

    def bucket_for(arg: str) -> dict:
        arg = arg.strip()
        if not arg:
            return main
        if arg.startswith("-n "):
            full = arg[3:].strip()
        else:
            full = short_to_full.get(arg, "")
            if not full:
                raise Refused(f"unknown subpackage {arg!r}")
        if full not in subs:
            raise Refused(f"unknown subpackage {full!r}")
        return subs[full]

    def open_subpackage(arg: str) -> None:
        if arg.startswith("-n "):
            full = arg[3:].strip()
            short = full
        else:
            if not fields["name"]:
                raise Refused("%package appears before Name")
            short = arg
            full = f"{fields['name']}-{short}"
        subs[full] = _new_bucket()
        short_to_full[short] = full
        nonlocal current
        current = subs[full]

    for raw in text.splitlines():
        stripped = raw.strip()
        if skip_depth:
            if stripped.startswith("%if"):
                skip_depth += 1
            elif stripped.startswith("%endif"):
                skip_depth -= 1
            continue
        if stripped.startswith("%ifarch") or stripped.startswith("%ifnarch"):
            if not ifarch_warned:
                warnings.append("%ifarch body kept")
                ifarch_warned = True
            continue
        if stripped == "%endif":
            continue
        if stripped.startswith("%if ") or stripped == "%if":
            cond = _expand(stripped[3:].strip(), defines, fields).strip()
            if cond in {"", "0"}:
                skip_depth = 1
            elif cond not in warnings:
                warnings.append(f"treating condition as true: {stripped}")
            continue
        if stripped.startswith("%define ") or stripped.startswith("%global "):
            parts = stripped.split(None, 2)
            defines[parts[1]] = _expand(parts[2], defines, fields) if len(parts) > 2 else ""
            continue
        if not stripped or (stripped.startswith("#") and section in {"preamble", "files"}):
            if section == "description":
                desc_lines.append(raw)
            elif section in {"prep", "build", "install", "check", "pre", "post", "preun", "postun"}:
                _append_script(section, raw, prep, build, install, check, script_target)
            continue

        kind = _section(stripped)
        if kind is not None:
            finish_description()
            section = kind
            arg = stripped[len(kind) + 1 :].strip()
            if kind == "package":
                open_subpackage(arg)
                section = "preamble"
            elif kind == "description":
                desc_bucket = bucket_for(arg) if arg else current
                section = "description"
            elif kind == "files":
                file_target = bucket_for(arg) if arg else main
            elif kind in {"pre", "post", "preun", "postun"}:
                script_target = bucket_for(arg) if arg else main
            elif kind == "changelog":
                section = "changelog"
            continue
        if section == "changelog":
            continue
        if section == "description":
            desc_lines.append(raw)
            continue
        if section in {"prep", "build", "install", "check"}:
            _append_script(section, raw, prep, build, install, check, script_target)
            continue
        if section in {"pre", "post", "preun", "postun"}:
            _append_script(section, raw, prep, build, install, check, script_target)
            continue
        if section == "files":
            file_target["files"].append(_parse_file(stripped, defines, fields))
            continue
        if ":" not in stripped:
            warnings.append(f"ignored preamble line: {stripped}")
            continue
        key, value = stripped.split(":", 1)
        key_name = key.strip().lower()
        expanded = _expand(value.strip(), defines, fields)
        _apply_tag(
            current,
            fields,
            key_name,
            expanded,
            sources,
            patches,
        )
        if current is main and key_name == "license":
            license_text = expanded
        elif current is main and key_name == "url":
            url = expanded
        elif current is main and key_name in {"buildarch", "arch"}:
            arch = expanded or arch

    finish_description()
    if not fields["name"]:
        raise Refused("spec has no Name")
    subpackages = []
    for full, bucket in subs.items():
        from rocky2deb.ir import Subpackage

        subpackages.append(
            Subpackage(
                name=full,
                summary=bucket["summary"],
                description=bucket["description"],
                requires=bucket["requires"],
                provides=bucket["provides"],
                conflicts=bucket["conflicts"],
                files=bucket["files"],
                scripts=bucket["scripts"],
            )
        )
    prep_text = "\n".join(prep).strip()
    build_text = "\n".join(build).strip()
    return PackageIR(
        name=fields["name"],
        version=fields["version"],
        release=fields["release"],
        epoch=fields["epoch"],
        arch=arch,
        license=license_text,
        summary=main["summary"],
        url=url,
        description=main["description"],
        source_kind="spec",
        source_id=source_id,
        build_system=_sniff_build(prep_text, build_text),
        build_requires=main["build_requires"] if "build_requires" in main else [],
        requires=main["requires"],
        provides=main["provides"],
        conflicts=main["conflicts"],
        patches=patches,
        sources=sources,
        prep=prep_text,
        build=build_text,
        install="\n".join(install).strip(),
        check="\n".join(check).strip(),
        files=main["files"],
        scripts=main["scripts"],
        subpackages=subpackages,
        warnings=warnings,
    )


def _append_script(section, raw, prep, build, install, check, script_target) -> None:
    if section == "prep":
        prep.append(raw.rstrip())
    elif section == "build":
        build.append(raw.rstrip())
    elif section == "install":
        install.append(raw.rstrip())
    elif section == "check":
        check.append(raw.rstrip())
    elif section in {"pre", "post", "preun", "postun"}:
        field = _script_field(section)
        existing = getattr(script_target["scripts"], field)
        line = raw.rstrip()
        setattr(script_target["scripts"], field, f"{existing}\n{line}".strip())


def _apply_tag(bucket, fields, key, value, sources, patches) -> None:
    if key == "name" and bucket is not None and fields["name"] == "" and "build_requires" not in bucket:
        # Name only sticks on the main package, which is the bucket that owns build_requires.
        pass
    if key in {"buildrequires", "requires", "provides", "conflicts"}:
        attr = {
            "buildrequires": "build_requires",
            "requires": "requires",
            "provides": "provides",
            "conflicts": "conflicts",
        }[key]
        if attr == "build_requires":
            bucket.setdefault("build_requires", [])
        for part in value.split(","):
            if part.strip():
                bucket.setdefault(attr, []).append(_parse_dep(part))
        return
    if key.startswith("source"):
        if value:
            sources.append(value)
        return
    if key.startswith("patch"):
        if value:
            patches.append(value)
        return
    if key == "name":
        fields["name"] = value
        return
    if key == "version":
        fields["version"] = value
        return
    if key == "release":
        fields["release"] = value
        return
    if key == "epoch":
        fields["epoch"] = "" if value in {"", "0"} else value
        return
    if key == "summary":
        bucket["summary"] = value
        return
    if key in {"license", "url", "buildarch"}:
        return
    if key in {"group", "vendor", "packager", "autoreq", "autoprov"}:
        return
