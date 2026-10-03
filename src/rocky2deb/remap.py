"""Apply a declarative config map. Unmapped keys are kept and reported.

A map with a non-empty `known` list reports every other key. The key stays in
the rewritten body and is copied to `aside`. A map with an empty `known` list
keeps every key and reports none: sysctl and sudoers are open vocabularies, and
a closed list would hide real settings or flag every line.

Reports name keys and paths. They do not include configuration values.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from rocky2deb.errors import Refused
from rocky2deb.resources import package_data

SFTP_ROCKY = "/usr/libexec/openssh/sftp-server"
SFTP_DEBIAN = "/usr/lib/openssh/sftp-server"

_COPY_FORMATS = frozenset({"copy", "xml", "keyfile", "text", "cron-spool"})
_LINE_FORMATS = frozenset({"sshd", "chrony", "sysctl"})


@dataclass
class ConfigMap:
    id: str
    rocky: str
    debian: str
    format: str
    critical: bool
    mode: str
    keys: dict[str, str] = field(default_factory=dict)
    packages: list[str] = field(default_factory=list)
    known: list[str] = field(default_factory=list)


@dataclass
class RemapResult:
    map_id: str
    debian_path: str
    body: str
    aside: str
    unmapped: list[str]
    critical: bool


def _parse_map(data: dict, source: str) -> ConfigMap:
    if "id" not in data or "rocky" not in data or "debian" not in data:
        raise Refused(f"config map {source} needs id, rocky, and debian")
    keys = data.get("keys", {})
    if not isinstance(keys, dict):
        raise Refused(f"config map {data.get('id', source)} keys must be a table")
    return ConfigMap(
        id=str(data["id"]),
        rocky=str(data["rocky"]),
        debian=str(data["debian"]),
        format=str(data.get("format", "copy")),
        critical=bool(data.get("critical", False)),
        mode=str(data.get("mode", "copy")),
        keys={str(key): str(value) for key, value in keys.items()},
        packages=[str(item) for item in data.get("packages", [])],
        known=[str(item) for item in data.get("known", [])],
    )


def load_maps(extra_dir: Path | None = None) -> dict[str, ConfigMap]:
    maps: dict[str, ConfigMap] = {}
    for path in sorted(package_data("maps").glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        parsed = _parse_map(data, path.name)
        maps[parsed.id] = parsed
    if extra_dir is not None:
        for path in sorted(Path(extra_dir).glob("*.toml")):
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            parsed = _parse_map(data, path.name)
            maps[parsed.id] = parsed
    return maps


def translate_config_path(path: str, cmap: ConfigMap) -> str:
    """Map a Rocky path onto the Debian path. A second pass stays put."""
    if path == cmap.rocky or path == cmap.debian:
        return cmap.debian
    debian_prefix = cmap.debian.rstrip("/") + "/"
    rocky_prefix = cmap.rocky.rstrip("/") + "/"
    if path.startswith(debian_prefix):
        return path
    if path.startswith(rocky_prefix):
        return debian_prefix + path[len(rocky_prefix) :]
    return path


def _split_key(line: str) -> tuple[str, str, str]:
    if "=" in line:
        key, rest = line.split("=", 1)
        return key.strip(), "=", rest
    parts = line.split(None, 1)
    if len(parts) == 1:
        return parts[0], "", ""
    return parts[0], " ", parts[1]


def remap_text(text: str, cmap: ConfigMap) -> RemapResult:
    if cmap.format in _COPY_FORMATS or cmap.format not in _LINE_FORMATS:
        if cmap.format not in _COPY_FORMATS | _LINE_FORMATS:
            raise Refused(f"unknown config format {cmap.format!r}")
        return RemapResult(cmap.id, cmap.debian, text, "", [], cmap.critical)
    known = set(cmap.known)
    out_lines: list[str] = []
    aside: list[str] = []
    unmapped: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            out_lines.append(line)
            continue
        key, sep, rest = _split_key(line.strip())
        if cmap.format == "sshd":
            rest = rest.replace(SFTP_ROCKY, SFTP_DEBIAN)
        renamed = cmap.keys.get(key, key)
        rendered = f"{renamed}{sep}{rest}" if sep else renamed
        if known and key not in known and key not in cmap.keys:
            unmapped.append(key)
            aside.append(rendered)
        out_lines.append(rendered)
    body = "\n".join(out_lines)
    if text.endswith("\n") or body:
        body += "\n"
    aside_body = "\n".join(aside)
    if aside_body:
        aside_body += "\n"
    return RemapResult(cmap.id, cmap.debian, body, aside_body, unmapped, cmap.critical)


def format_remap_report(result: RemapResult) -> str:
    """Key names and paths only. Configuration values stay out of the report."""
    lines = [f"map {result.map_id} -> {result.debian_path}"]
    for key in result.unmapped:
        lines.append(f"unmapped {key}")
    return "\n".join(lines) + "\n"
