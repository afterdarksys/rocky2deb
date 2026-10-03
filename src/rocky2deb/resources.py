"""Paths to data files shipped inside the package."""

from __future__ import annotations

from pathlib import Path


def package_data(*parts: str) -> Path:
    return Path(__file__).resolve().parent.joinpath("data", *parts)
