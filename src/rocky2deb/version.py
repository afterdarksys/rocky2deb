"""Debian version comparison (Policy 5.6.12)."""

from __future__ import annotations


def _order(ch: str) -> int:
    if ch == "~":
        return -1
    if ch.isalpha():
        return ord(ch)
    return ord(ch) + 256


def _cmp_fragment(left: str, right: str) -> int:
    i = 0
    j = 0
    while i < len(left) or j < len(right):
        while (i < len(left) and not left[i].isdigit()) or (
            j < len(right) and not right[j].isdigit()
        ):
            left_order = 0 if i >= len(left) else _order(left[i])
            right_order = 0 if j >= len(right) else _order(right[j])
            if left_order != right_order:
                return (left_order > right_order) - (left_order < right_order)
            i += 1
            j += 1
        left_num = ""
        while i < len(left) and left[i].isdigit():
            left_num += left[i]
            i += 1
        right_num = ""
        while j < len(right) and right[j].isdigit():
            right_num += right[j]
            j += 1
        left_value = int(left_num) if left_num else 0
        right_value = int(right_num) if right_num else 0
        if left_value != right_value:
            return (left_value > right_value) - (left_value < right_value)
    return 0


def split_debian_version(version: str) -> tuple[int, str, str]:
    epoch = 0
    rest = version
    if ":" in rest:
        epoch_text, rest = rest.split(":", 1)
        epoch = int(epoch_text)
    if "-" in rest:
        upstream, revision = rest.rsplit("-", 1)
    else:
        upstream, revision = rest, ""
    return epoch, upstream, revision


def cmp_debian(left: str, right: str) -> int:
    """Return -1, 0, or 1. Epoch 0 equals a missing epoch."""
    left_epoch, left_up, left_rev = split_debian_version(left)
    right_epoch, right_up, right_rev = split_debian_version(right)
    if left_epoch != right_epoch:
        return (left_epoch > right_epoch) - (left_epoch < right_epoch)
    upstream = _cmp_fragment(left_up, right_up)
    if upstream != 0:
        return upstream
    return _cmp_fragment(left_rev, right_rev)


def debian_at_least(have: str, minimum: str) -> bool:
    return cmp_debian(have, minimum) >= 0


def cmp_glibc(left: str, right: str) -> int:
    """Compare glibc symbol versions such as 2.34 and 2.41."""
    left_parts = tuple(int(part) for part in left.split("."))
    right_parts = tuple(int(part) for part in right.split("."))
    width = max(len(left_parts), len(right_parts))
    left_full = left_parts + (0,) * (width - len(left_parts))
    right_full = right_parts + (0,) * (width - len(right_parts))
    return (left_full > right_full) - (left_full < right_full)
