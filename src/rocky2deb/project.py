"""Load and create a migration project. The project stores decisions, not host secrets."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from rocky2deb.errors import Refused
from rocky2deb.profiles import get_profile

_DIRS = (
    "inventory",
    "maps/config",
    "ir",
    "src",
    "artifacts/deb",
    "artifacts/rpm",
    "plan",
    "bundle",
)


@dataclass
class Project:
    target_suite: str
    rocky_major: int
    pins: list[str] = field(default_factory=list)
    root: Path | None = None


def _validate(suite: str, rocky_major: int) -> None:
    get_profile(suite)
    if rocky_major not in (8, 9, 10):
        raise Refused("rocky_major must be 8, 9, or 10")


def load_project(path: Path) -> Project:
    path = Path(path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    suite = str(data.get("target_suite", ""))
    if not suite:
        raise Refused("project is missing target_suite")
    try:
        major = int(data.get("rocky_major"))
    except (TypeError, ValueError) as exc:
        raise Refused("rocky_major must be 8, 9, or 10") from exc
    _validate(suite, major)
    pins = [str(item) for item in data.get("pins", [])]
    return Project(suite, major, pins, path.parent)


def init_project(dest: Path, suite: str, rocky_major: int) -> Path:
    _validate(suite, rocky_major)
    root = Path(dest)
    cfg = root / "rocky2deb.toml"
    if cfg.exists():
        raise Refused("rocky2deb.toml already exists")
    for rel in _DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        f'target_suite = "{suite}"\nrocky_major = {rocky_major}\npins = []\n',
        encoding="utf-8",
    )
    waves = root / "plan" / "waves.toml"
    if not waves.exists():
        waves.write_text('canaries = []\nhosts = []\ncritical_maps = ["sshd", "network"]\n', encoding="utf-8")
    return cfg
