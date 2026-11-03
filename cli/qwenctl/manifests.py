"""生成 Karpenter 的 EC2NodeClass / NodePool（JSON，kubectl 直接接受，不依赖 PyYAML）。

节点分层见 docs/01 §3.1：
- general：客户的非 GPU 业务，不带污点
- gpu-<profile>：每个 GPU profile 一个，带 nvidia.com/gpu 污点，weight 决定回退顺序
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Sequence

KARPENTER_API = "karpenter.sh/v1"
NODECLASS_API = "karpenter.k8s.aws/v1"
NODE_ROLE_LABEL = "qwenctl/node-role"
GPU_PROFILE_LABEL = "qwenctl/gpu-profile"
GPU_TAINT = {"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}
GENERAL_INSTANCE_CATEGORIES = ["c", "m", "r"]
MIN_INSTANCE_GENERATION = "5"  # 只用第 6 代及以后
NODE_EXPIRE_AFTER = "720h"

Manifest = Dict[str, Any]


class ManifestError(Exception):
    """CDK 输出里缺少生成节点池所需的值。"""


def gpu_limit(profile: Mapping[str, Any], max_replicas: int) -> int:
    """NodePool 的 GPU 上限 = 跑满 max_replicas 需要的节点数 × 每节点 GPU 数。"""
    per_node = profile["replicasPerNode"]
    gpus_per_node = per_node * profile["tensorParallel"]
    return math.ceil(max_replicas / per_node) * gpus_per_node


def render(config: Mapping[str, Any], outputs: Mapping[str, str], bottlerocket: str) -> List[Manifest]:
    """config 是 qwenctl 保存的部署配置（JSON 契约），outputs 是 EKS 栈的 CloudFormation 输出。"""
    node_role = _required(outputs, "KarpenterNodeRoleName")
    security_group = _required(outputs, "ClusterSecurityGroupId")
    subnets = [s for s in _required(outputs, "PrivateSubnetIds").split(",") if s]
    # 系统 tag 放在最后，客户的 tag 不能覆盖它
    tags = {**config["advanced"]["compliance"]["tags"], "qwenctl:deployment": config["name"]}
    base = {
        "role": node_role,
        "amiSelectorTerms": [{"alias": f"bottlerocket@{bottlerocket}"}],
        "subnetSelectorTerms": [{"id": s} for s in subnets],
        "securityGroupSelectorTerms": [{"id": security_group}],
        "metadataOptions": {
            "httpEndpoint": "enabled",
            "httpProtocolIPv6": "disabled",
            "httpPutResponseHopLimit": 1,
            "httpTokens": "required",
        },
        "tags": tags,
    }
    zone_ids = [z["zoneId"] for z in config["zones"]]
    manifests = [
        _nodeclass("general", base, data_volume={"volumeSize": "50Gi", "volumeType": "gp3"}),
        _nodeclass(
            "gpu",
            base,
            # 后续会挂上预缓存 vLLM 镜像的 EBS 快照（docs/01 §4.2）
            data_volume={"volumeSize": "150Gi", "volumeType": "gp3", "iops": 16000, "throughput": 1000},
        ),
        _general_nodepool(config["advanced"]["general_nodes"], zone_ids),
    ]
    warm = config["advanced"]["scaling"]["warm_node_minutes"]
    max_replicas = config["gpu"]["maxReplicas"]
    manifests.extend(_gpu_nodepool(p, max_replicas, warm) for p in config["gpu"]["profiles"])
    return manifests


def _required(outputs: Mapping[str, str], key: str) -> str:
    value = outputs.get(key)
    if not value:
        raise ManifestError(f"EKS 栈的输出里没有 {key}，请先完成 CloudFormation 部署")
    return value


def _nodeclass(name: str, base: Mapping[str, Any], data_volume: Mapping[str, Any]) -> Manifest:
    # Bottlerocket：xvda 是只读的系统盘，xvdb 放容器镜像和数据
    devices = [
        {"deviceName": "/dev/xvda", "ebs": {"volumeSize": "4Gi", "volumeType": "gp3", "encrypted": True}},
        {"deviceName": "/dev/xvdb", "ebs": {**data_volume, "encrypted": True}},
    ]
    return {
        "apiVersion": NODECLASS_API,
        "kind": "EC2NodeClass",
        "metadata": {"name": name},
        "spec": {**base, "blockDeviceMappings": devices},
    }


def _requirement(key: str, values: Sequence[str], operator: str = "In") -> Dict[str, Any]:
    return {"key": key, "operator": operator, "values": list(values)}


def _general_nodepool(general: Mapping[str, Any], zone_ids: Sequence[str]) -> Manifest:
    return {
        "apiVersion": KARPENTER_API,
        "kind": "NodePool",
        "metadata": {"name": "general"},
        "spec": {
            "template": {
                "metadata": {"labels": {NODE_ROLE_LABEL: "general"}},
                "spec": {
                    "nodeClassRef": {"group": "karpenter.k8s.aws", "kind": "EC2NodeClass", "name": "general"},
                    "requirements": [
                        _requirement("kubernetes.io/arch", general["arch"]),
                        _requirement("kubernetes.io/os", ["linux"]),
                        _requirement("karpenter.sh/capacity-type", general["capacity_types"]),
                        _requirement("karpenter.k8s.aws/instance-category", GENERAL_INSTANCE_CATEGORIES),
                        _requirement(
                            "karpenter.k8s.aws/instance-generation", [MIN_INSTANCE_GENERATION], "Gt"
                        ),
                        _requirement("topology.k8s.aws/zone-id", zone_ids),
                    ],
                    "expireAfter": NODE_EXPIRE_AFTER,
                },
            },
            "limits": {"cpu": str(general["cpu_limit"])},
            "disruption": {"consolidationPolicy": "WhenEmptyOrUnderutilized", "consolidateAfter": "1m"},
        },
    }


def _gpu_nodepool(profile: Mapping[str, Any], max_replicas: int, warm_minutes: int) -> Manifest:
    labels = {NODE_ROLE_LABEL: "gpu", GPU_PROFILE_LABEL: profile["name"]}
    return {
        "apiVersion": KARPENTER_API,
        "kind": "NodePool",
        "metadata": {"name": f"gpu-{profile['name']}", "labels": {GPU_PROFILE_LABEL: profile["name"]}},
        "spec": {
            "weight": profile["nodepoolWeight"],
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "nodeClassRef": {"group": "karpenter.k8s.aws", "kind": "EC2NodeClass", "name": "gpu"},
                    "taints": [GPU_TAINT],
                    "requirements": [
                        _requirement("node.kubernetes.io/instance-type", profile["instanceTypes"]),
                        _requirement("karpenter.sh/capacity-type", ["reserved", "on-demand"]),
                        _requirement("topology.k8s.aws/zone-id", profile["zoneIds"]),
                    ],
                    "expireAfter": NODE_EXPIRE_AFTER,
                },
            },
            "limits": {"nvidia.com/gpu": str(gpu_limit(profile, max_replicas))},
            # 只回收空节点，绝不为了装箱驱逐正在推理的 Pod；空节点保留一段时间作为保温节点
            "disruption": {"consolidationPolicy": "WhenEmpty", "consolidateAfter": f"{warm_minutes}m"},
        },
    }
