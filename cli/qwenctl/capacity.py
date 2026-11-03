"""配额 → 副本上限，以及成本估算。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .catalog import GpuProfile

HOURS_PER_MONTH = 730
DEFAULT_MAX_REPLICAS = 8
DEFAULT_MIN_REPLICAS = 2


@dataclass(frozen=True)
class Quota:
    limit_vcpus: float
    used_vcpus: float

    @property
    def available_vcpus(self) -> float:
        return max(0.0, self.limit_vcpus - self.used_vcpus)


@dataclass(frozen=True)
class ReplicaDefaults:
    min_replicas: int
    max_replicas: int
    upper_bound: int


@dataclass(frozen=True)
class CostEstimate:
    baseline_monthly: Optional[float]
    peak_hourly: Optional[float]


def max_replicas_by_quota(profile: GpuProfile, available_vcpus: float) -> int:
    nodes = int(available_vcpus // profile.vcpus_per_node)
    return nodes * profile.replicas_per_node


def replica_upper_bound(profile: GpuProfile, available_vcpus: Optional[float]) -> int:
    """配额读不到时（available_vcpus 为 None）按默认上限处理。"""
    if available_vcpus is None:
        return DEFAULT_MAX_REPLICAS
    return max_replicas_by_quota(profile, available_vcpus)


def replica_defaults(upper_bound: int) -> ReplicaDefaults:
    if upper_bound < 1:
        raise ValueError("副本上限必须至少为 1")
    max_replicas = min(DEFAULT_MAX_REPLICAS, upper_bound)
    min_replicas = min(DEFAULT_MIN_REPLICAS, max_replicas)
    return ReplicaDefaults(min_replicas, max_replicas, upper_bound)


def validate_replicas(min_replicas: int, max_replicas: int, upper_bound: int) -> Optional[str]:
    """返回错误信息；合法时返回 None。"""
    if min_replicas < 1:
        return "最少副本数至少为 1"
    if max_replicas < min_replicas:
        return "最多副本数不能小于最少副本数"
    if max_replicas > upper_bound:
        return f"最多副本数不能超过 {upper_bound}（受 GPU 配额限制）"
    return None


def estimate_cost(
    profile: GpuProfile,
    price_per_node_hour: Optional[float],
    min_replicas: int,
    max_replicas: int,
) -> CostEstimate:
    """按主力机型估算：常驻部分的月成本、满载时每小时成本。"""
    if price_per_node_hour is None:
        return CostEstimate(None, None)
    baseline = profile.nodes_for(min_replicas) * price_per_node_hour * HOURS_PER_MONTH
    peak = profile.nodes_for(max_replicas) * price_per_node_hour
    return CostEstimate(baseline_monthly=baseline, peak_hourly=peak)
