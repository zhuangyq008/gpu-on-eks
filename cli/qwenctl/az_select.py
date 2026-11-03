"""AZ 选择：在当前 region 里选出 2 个 AZ（算法见 docs/03 §4）。

全程用 AZ ID：同一个 AZ 名在不同账号里对应的物理 AZ 不一样。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AbstractSet, Mapping, Optional, Sequence, Tuple

from .catalog import GpuProfile

REQUIRED_ZONE_COUNT = 2


class AzSelectionError(Exception):
    """选不出满足要求的 AZ。"""


@dataclass(frozen=True)
class Zone:
    zone_id: str
    zone_name: str

    @property
    def label(self) -> str:
        return f"{self.zone_id} ({self.zone_name})"


@dataclass(frozen=True)
class ProfilePlacement:
    """某个 GPU profile 在选中的 AZ 里，有货的是哪几个。"""

    profile: GpuProfile
    zone_ids: Tuple[str, ...]


@dataclass(frozen=True)
class AzSelection:
    zones: Tuple[Zone, ...]
    placements: Tuple[ProfilePlacement, ...]  # 按优先级从高到低
    warnings: Tuple[str, ...]

    @property
    def primary(self) -> ProfilePlacement:
        return self.placements[0]

    @property
    def zone_ids(self) -> Tuple[str, ...]:
        return tuple(z.zone_id for z in self.zones)


Offerings = Mapping[str, AbstractSet[str]]  # zone_id -> 有货的机型


def _available(zone_id: str, offerings: Offerings, profiles: Sequence[GpuProfile]) -> Tuple[GpuProfile, ...]:
    offered = offerings.get(zone_id, frozenset())
    return tuple(p for p in profiles if p.instance_type in offered)


def _score(zone_id: str, offerings: Offerings, profiles: Sequence[GpuProfile]) -> Tuple[int, int]:
    """(最高档位, 可用机型数)。两种机型都有的 AZ 排在只有一种的前面。"""
    available = _available(zone_id, offerings, profiles)
    best = max((p.priority for p in available), default=0)
    return best, len(available)


def select_azs(
    zones: Sequence[Zone],
    offerings: Offerings,
    profiles: Sequence[GpuProfile],
    eligible_zone_ids: Optional[AbstractSet[str]] = None,
    count: int = REQUIRED_ZONE_COUNT,
) -> AzSelection:
    candidates = [z for z in zones if eligible_zone_ids is None or z.zone_id in eligible_zone_ids]
    if len(candidates) < count:
        raise AzSelectionError(f"至少需要 {count} 个可用的 AZ（ALB 的要求），当前只有 {len(candidates)} 个")

    def rank(zone: Zone) -> Tuple[int, int, str]:
        best, n = _score(zone.zone_id, offerings, profiles)
        return -best, -n, zone.zone_id

    chosen = sorted(candidates, key=rank)[:count]
    if _score(chosen[0].zone_id, offerings, profiles)[0] == 0:
        raise AzSelectionError("候选 AZ 里没有任何可用的 GPU 机型")

    chosen_sorted = tuple(sorted(chosen, key=lambda z: z.zone_id))
    placements = _placements(chosen_sorted, offerings, profiles)
    warnings = _warnings(chosen_sorted, placements)
    return AzSelection(zones=chosen_sorted, placements=placements, warnings=warnings)


def _placements(
    chosen: Sequence[Zone], offerings: Offerings, profiles: Sequence[GpuProfile]
) -> Tuple[ProfilePlacement, ...]:
    result = []
    for profile in sorted(profiles, key=lambda p: -p.priority):
        ids = tuple(z.zone_id for z in chosen if profile.instance_type in offerings.get(z.zone_id, ()))
        if ids:
            result.append(ProfilePlacement(profile=profile, zone_ids=ids))
    return tuple(result)


def _warnings(chosen: Sequence[Zone], placements: Sequence[ProfilePlacement]) -> Tuple[str, ...]:
    labels = {z.zone_id: z.label for z in chosen}
    warnings = []
    gpu_zone_ids = sorted({zid for pl in placements for zid in pl.zone_ids})
    if len(gpu_zone_ids) < len(chosen):
        warnings.append(f"GPU 副本只能放在 {labels[gpu_zone_ids[0]]} 一个 AZ，没有 AZ 级高可用")
    primary = placements[0]
    if len(primary.zone_ids) < len(gpu_zone_ids) and len(placements) > 1:
        only = ", ".join(labels[z] for z in primary.zone_ids)
        fallback = placements[1].profile.instance_type
        warnings.append(f"{primary.profile.instance_type} 只在 {only} 有货；另一个 AZ 由 {fallback} 兜底")
    for placement in placements:
        if placement.profile.experimental:
            warnings.append(f"{placement.profile.instance_type}（{placement.profile.note}）需要 PoC 验证")
    return tuple(warnings)
