"""Wave gates. A later wave waits until the canary is verified and the plan is clean.

pre_apply fails while any required package is a gap or a critical map still has
an unmapped key. next_wave fails until pre_apply passed and the canary verify
flag is set. Canaries that are not in the host set are refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rocky2deb.errors import Refused
from rocky2deb.resolve import Decision


@dataclass
class GateResult:
    name: str
    ok: bool
    reasons: list[str] = field(default_factory=list)


def assign_waves(hosts: list[str], canaries: list[str]) -> dict[str, list[str]]:
    unknown = [host for host in canaries if host not in hosts]
    if unknown:
        raise Refused("canary is not in the host set")
    canary_set = set(canaries)
    return {
        "canary": list(canaries),
        "rest": [host for host in hosts if host not in canary_set],
    }


def pre_apply_gate(decisions: list[Decision], unmapped_critical: list[str]) -> GateResult:
    reasons = []
    gaps = [item.rpm for item in decisions if item.action == "gap"]
    if gaps:
        reasons.append("gap: " + ", ".join(gaps))
    if unmapped_critical:
        reasons.append("unmapped: " + ", ".join(unmapped_critical))
    return GateResult("pre_apply", not reasons, reasons)


def next_wave_gate(pre_apply_ok: bool, canary_verified: bool) -> GateResult:
    reasons = []
    if not pre_apply_ok:
        reasons.append("pre_apply has not passed")
    if not canary_verified:
        reasons.append("canary verify has not passed")
    return GateResult("next_wave", not reasons, reasons)
