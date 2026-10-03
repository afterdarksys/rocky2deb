"""Load Rocky repo metadata from primary.xml or a JSON fixture.

The XML size is capped before parse. A DOCTYPE or entity is refused so the
parser cannot expand a bomb. External entities are not resolved.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from rocky2deb.errors import Refused

DEFAULT_XML_LIMIT = 50 * 1024 * 1024


@dataclass
class RepoPackage:
    name: str
    arch: str = "x86_64"
    epoch: str = ""
    version: str = ""
    release: str = ""
    checksum: str = ""
    href: str = ""
    license: str = ""
    sourcerpm: str = ""
    requires: list[str] = field(default_factory=list)
    build_requires: list[str] = field(default_factory=list)
    recommends: list[str] = field(default_factory=list)
    patches: list[str] = field(default_factory=list)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def parse_primary_xml(text: str, limit: int = DEFAULT_XML_LIMIT) -> list[RepoPackage]:
    encoded = text.encode("utf-8")
    if len(encoded) > limit:
        raise Refused("primary.xml exceeds size cap")
    folded = text.casefold()
    if "<!doctype" in folded or "<!entity" in folded:
        raise Refused("refusing primary.xml with a doctype or entity")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise Refused("primary.xml did not parse") from exc
    packages: list[RepoPackage] = []
    for element in root.iter():
        if _local(element.tag) != "package":
            continue
        name = _child_text(element, "name")
        if not name:
            continue
        version = ""
        release = ""
        epoch = ""
        checksum = ""
        href = ""
        license_text = ""
        sourcerpm = ""
        requires: list[str] = []
        for child in element:
            local = _local(child.tag)
            if local == "version":
                version = child.attrib.get("ver", "")
                release = child.attrib.get("rel", "")
                raw_epoch = child.attrib.get("epoch", "")
                epoch = "" if raw_epoch in {"", "0"} else raw_epoch
            elif local == "checksum" and child.text:
                checksum = child.text.strip()
            elif local == "location":
                href = child.attrib.get("href", "")
            elif local == "format":
                for fmt in child:
                    fmt_name = _local(fmt.tag)
                    if fmt_name == "license" and fmt.text:
                        license_text = fmt.text.strip()
                    elif fmt_name == "sourcerpm" and fmt.text:
                        sourcerpm = fmt.text.strip()
                    elif fmt_name == "requires":
                        for entry in fmt:
                            dep = entry.attrib.get("name", "")
                            if dep:
                                requires.append(dep)
        packages.append(
            RepoPackage(
                name=name,
                arch=_child_text(element, "arch") or "x86_64",
                epoch=epoch,
                version=version,
                release=release,
                checksum=checksum,
                href=href,
                license=license_text,
                sourcerpm=sourcerpm,
                requires=requires,
            )
        )
    return packages


def _one(item: dict) -> RepoPackage:
    if not item.get("name"):
        raise Refused("repo package is missing a name")
    epoch = str(item.get("epoch", "") or "")
    if epoch == "0":
        epoch = ""
    return RepoPackage(
        name=item["name"],
        arch=item.get("arch", "x86_64"),
        epoch=epoch,
        version=item.get("version", ""),
        release=item.get("release", ""),
        checksum=item.get("checksum", ""),
        href=item.get("href", ""),
        license=item.get("license", ""),
        sourcerpm=item.get("sourcerpm", ""),
        requires=list(item.get("requires", [])),
        build_requires=list(item.get("build_requires", [])),
        recommends=list(item.get("recommends", [])),
        patches=list(item.get("patches", [])),
    )


def parse_repo_json(text: str) -> list[RepoPackage]:
    data = json.loads(text)
    rows = data["packages"] if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise Refused("repo JSON needs a packages list")
    return [_one(item) for item in rows]


def load_repo(text: str, filename: str = "", limit: int = DEFAULT_XML_LIMIT) -> list[RepoPackage]:
    stripped = text.lstrip()
    if filename.endswith(".xml") or stripped.startswith("<"):
        return parse_primary_xml(text, limit=limit)
    return parse_repo_json(text)
