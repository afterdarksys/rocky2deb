"""Package intermediate representation shared by both emitters."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class Dep:
    name: str
    relation: str = ""
    version: str = ""


@dataclass
class FileEntry:
    path: str
    config: bool = False
    noreplace: bool = False
    doc: bool = False
    license_file: bool = False
    ghost: bool = False
    is_dir: bool = False
    mode: str = ""
    user: str = ""
    group: str = ""


@dataclass
class Scriptlets:
    pre: str = ""
    post: str = ""
    preun: str = ""
    postun: str = ""


@dataclass
class Subpackage:
    name: str
    summary: str = ""
    description: str = ""
    requires: list[Dep] = field(default_factory=list)
    provides: list[Dep] = field(default_factory=list)
    conflicts: list[Dep] = field(default_factory=list)
    files: list[FileEntry] = field(default_factory=list)
    scripts: Scriptlets = field(default_factory=Scriptlets)


@dataclass
class PackageIR:
    ir_version: int = 1
    name: str = ""
    version: str = ""
    release: str = ""
    epoch: str = ""
    arch: str = "x86_64"
    license: str = ""
    summary: str = ""
    url: str = ""
    description: str = ""
    source_kind: str = "spec"
    source_id: str = ""
    checksum: str = ""
    build_system: str = "custom"
    build_requires: list[Dep] = field(default_factory=list)
    requires: list[Dep] = field(default_factory=list)
    provides: list[Dep] = field(default_factory=list)
    conflicts: list[Dep] = field(default_factory=list)
    patches: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    prep: str = ""
    build: str = ""
    install: str = ""
    check: str = ""
    files: list[FileEntry] = field(default_factory=list)
    scripts: Scriptlets = field(default_factory=Scriptlets)
    subpackages: list[Subpackage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def identity(self) -> str:
        epoch = f"{self.epoch}:" if self.epoch not in ("", "0") else ""
        release = f"-{self.release}" if self.release else ""
        return f"{self.name}-{epoch}{self.version}{release}"


def _dep(data: dict) -> Dep:
    return Dep(
        name=data["name"],
        relation=data.get("relation", ""),
        version=data.get("version", ""),
    )


def _file(data: dict) -> FileEntry:
    return FileEntry(
        path=data["path"],
        config=bool(data.get("config", False)),
        noreplace=bool(data.get("noreplace", False)),
        doc=bool(data.get("doc", False)),
        license_file=bool(data.get("license_file", False)),
        ghost=bool(data.get("ghost", False)),
        is_dir=bool(data.get("is_dir", False)),
        mode=data.get("mode", ""),
        user=data.get("user", ""),
        group=data.get("group", ""),
    )


def _scripts(data: dict | None) -> Scriptlets:
    data = data or {}
    return Scriptlets(
        pre=data.get("pre", ""),
        post=data.get("post", ""),
        preun=data.get("preun", ""),
        postun=data.get("postun", ""),
    )


def _sub(data: dict) -> Subpackage:
    return Subpackage(
        name=data["name"],
        summary=data.get("summary", ""),
        description=data.get("description", ""),
        requires=[_dep(item) for item in data.get("requires", [])],
        provides=[_dep(item) for item in data.get("provides", [])],
        conflicts=[_dep(item) for item in data.get("conflicts", [])],
        files=[_file(item) for item in data.get("files", [])],
        scripts=_scripts(data.get("scripts")),
    )


def ir_from_dict(data: dict) -> PackageIR:
    if data.get("ir_version") != 1:
        raise ValueError("ir_version must be 1")
    if not data.get("name"):
        raise ValueError("IR name is empty")
    return PackageIR(
        ir_version=1,
        name=data["name"],
        version=data.get("version", ""),
        release=data.get("release", ""),
        epoch=str(data.get("epoch", "") or ""),
        arch=data.get("arch", "x86_64"),
        license=data.get("license", ""),
        summary=data.get("summary", ""),
        url=data.get("url", ""),
        description=data.get("description", ""),
        source_kind=data.get("source_kind", "spec"),
        source_id=data.get("source_id", ""),
        checksum=data.get("checksum", ""),
        build_system=data.get("build_system", "custom"),
        build_requires=[_dep(item) for item in data.get("build_requires", [])],
        requires=[_dep(item) for item in data.get("requires", [])],
        provides=[_dep(item) for item in data.get("provides", [])],
        conflicts=[_dep(item) for item in data.get("conflicts", [])],
        patches=list(data.get("patches", [])),
        sources=list(data.get("sources", [])),
        prep=data.get("prep", ""),
        build=data.get("build", ""),
        install=data.get("install", ""),
        check=data.get("check", ""),
        files=[_file(item) for item in data.get("files", [])],
        scripts=_scripts(data.get("scripts")),
        subpackages=[_sub(item) for item in data.get("subpackages", [])],
        warnings=list(data.get("warnings", [])),
    )


def ir_to_json(package: PackageIR) -> str:
    return json.dumps(asdict(package), indent=2, sort_keys=True) + "\n"


def ir_from_json(text: str) -> PackageIR:
    return ir_from_dict(json.loads(text))
