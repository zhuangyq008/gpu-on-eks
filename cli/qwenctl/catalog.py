"""锁定的机型目录：GPU profile、系统节点候选机型、配额代码。

这些值不对客户开放（见 docs/03 §3.4）。新增机型时在这里加一条 profile。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class GpuProfile:
    """一种 GPU 机型，以及它对应的锁定部署参数。"""

    name: str
    instance_type: str
    gpu_model: str
    gpus_per_node: int
    vcpus_per_node: int
    tensor_parallel: int
    priority: int
    experimental: bool = False
    extra_instance_types: Tuple[str, ...] = ()
    note: str = ""

    @property
    def replicas_per_node(self) -> int:
        return self.gpus_per_node // self.tensor_parallel

    @property
    def instance_types(self) -> Tuple[str, ...]:
        return (self.instance_type,) + self.extra_instance_types

    @property
    def nodepool_weight(self) -> int:
        return _WEIGHT_BY_PRIORITY.get(self.priority, 1)

    def nodes_for(self, replicas: int) -> int:
        return math.ceil(replicas / self.replicas_per_node)

    def vcpus_for(self, replicas: int) -> int:
        return self.nodes_for(replicas) * self.vcpus_per_node


_WEIGHT_BY_PRIORITY = {3: 100, 2: 50, 1: 10}

PROFILES: Tuple[GpuProfile, ...] = (
    GpuProfile(
        name="g7e-1gpu",
        instance_type="g7e.2xlarge",
        gpu_model="RTX PRO 6000 96GB",
        gpus_per_node=1,
        vcpus_per_node=8,
        tensor_parallel=1,
        priority=3,
        extra_instance_types=("g7e.4xlarge",),
    ),
    GpuProfile(
        name="g6e-1gpu",
        instance_type="g6e.2xlarge",
        gpu_model="L40S 48GB",
        gpus_per_node=1,
        vcpus_per_node=8,
        tensor_parallel=1,
        priority=2,
        extra_instance_types=("g6e.4xlarge",),
        note="显存余量约 6 GB",
    ),
    GpuProfile(
        name="g6-tp2",
        instance_type="g6.12xlarge",
        gpu_model="4x L4 24GB",
        gpus_per_node=4,
        vcpus_per_node=48,
        tensor_parallel=2,
        priority=1,
        experimental=True,
        note="TP=2，每个节点 2 个副本，KV 余量小",
    ),
)

# 系统节点（托管节点组，跑 Karpenter 和插件），按顺序取第一个在两个 AZ 都有货的
SYSTEM_INSTANCE_CANDIDATES: Tuple[str, ...] = ("m7g.large", "m6g.large", "m8g.large")
SYSTEM_NODE_COUNT = 2
SYSTEM_NODE_VCPUS = 2

GPU_QUOTA_CODE = "L-DB2E81BA"  # Running On-Demand G and VT instances
STANDARD_QUOTA_CODE = "L-1216C47A"  # Running On-Demand Standard (A, C, D, H, I, M, R, T, Z)
GPU_FAMILY_PREFIXES: Tuple[str, ...] = ("g", "vt")
STANDARD_FAMILY_PREFIXES: Tuple[str, ...] = ("a", "c", "d", "h", "i", "m", "r", "t", "z")
STANDARD_EXCLUDED_PREFIXES: Tuple[str, ...] = ("inf", "trn", "dl", "hpc", "mac")

BEDROCK_OVERFLOW_MODEL_ID = "qwen.qwen3-vl-235b-a22b"


def gpu_instance_types() -> Tuple[str, ...]:
    return tuple(t for p in PROFILES for t in p.instance_types)


def profile_by_name(name: str) -> GpuProfile:
    for profile in PROFILES:
        if profile.name == name:
            return profile
    raise KeyError(f"未知的 GPU profile：{name}")


def is_gpu_family(instance_type: str) -> bool:
    return instance_type.startswith(GPU_FAMILY_PREFIXES)


def is_standard_family(instance_type: str) -> bool:
    if instance_type.startswith(STANDARD_EXCLUDED_PREFIXES):
        return False
    return instance_type.startswith(STANDARD_FAMILY_PREFIXES)
