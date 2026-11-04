"""扩缩容：Prometheus 抓 vLLM 指标（PodMonitor），每个 GPU profile 一个 KEDA ScaledObject（docs/01 §5）。

- 主力 profile：按负载扩缩容。running + waiting 超过 target_utilization × C_sla 就加副本；出现排队也加。
- 回退 profile 按顺序接力：前一个 profile 的 Pod 调度不上（没有节点，通常是 ICE）超过 STUCK_SECONDS，
  就 1:1 扩出下一个 profile 的副本。只有上一级也没货，才会轮到再下一级，GPU 不会成倍扩出。
  上一级恢复后指标归零，回退副本在冷却期后缩回 0。
- 扩得激进、缩得保守：每次冷启动都很贵。
"""

from __future__ import annotations

import copy
import math
from typing import Any, Dict, List, Mapping

from .addons import PROMETHEUS_URL
from .catalog import DEFAULT_C_SLA
from .names import is_k8s_name
from .serving import APP_LABEL

Manifest = Dict[str, Any]

# 判定「调度不上」的时间。正常扩容时 Karpenter 约 40~60 s 就把节点拉起来、Pod 被调度
# （之后的镜像拉取、加载权重不算在内），超过 3 分钟还调度不上，基本就是没有容量
STUCK_SECONDS = 180
POLLING_INTERVAL = 10
# 回退副本在上一级恢复后再留 10 分钟：新节点 + 冷启动要 5~8 分钟（docs/03 §9.2），缩早了会断流
COOLDOWN_SECONDS = 600
_HPA_BEHAVIOR = {
    "scaleUp": {
        "stabilizationWindowSeconds": 0,
        "selectPolicy": "Max",
        "policies": [
            {"type": "Percent", "value": 100, "periodSeconds": 30},
            {"type": "Pods", "value": 4, "periodSeconds": 30},
        ],
    },
    "scaleDown": {
        "stabilizationWindowSeconds": 900,
        "policies": [{"type": "Pods", "value": 1, "periodSeconds": 300}],
    },
}


def pod_monitor(config: Mapping[str, Any]) -> Manifest:
    """vLLM 的 /metrics 每 5 秒抓一次（扩容决策要快），带上 GPU profile 标签。"""
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PodMonitor",
        "metadata": {"name": "vllm", "namespace": config["serving"]["namespace"]},
        "spec": {
            "selector": {"matchLabels": dict(APP_LABEL)},
            "podMetricsEndpoints": [
                {
                    "port": "http",
                    "path": "/metrics",
                    "interval": "5s",
                    "relabelings": [
                        {
                            "sourceLabels": ["__meta_kubernetes_pod_label_qwenctl_gpu_profile"],
                            "targetLabel": "gpu_profile",
                        }
                    ],
                }
            ],
        },
    }


def c_sla(config: Mapping[str, Any], profile: Mapping[str, Any]) -> int:
    """单副本在 SLA 内能承受的并发：压测后用 advanced.scaling.c_sla 覆盖，否则用机型目录的占位值。"""
    override = config["advanced"]["scaling"]["c_sla"].get(profile["name"])
    return int(override) if override else int(profile.get("cSla", DEFAULT_C_SLA))


def load_threshold(config: Mapping[str, Any], profile: Mapping[str, Any]) -> int:
    """每个副本的目标负载（running + waiting），四舍五入，至少 1。"""
    target = config["advanced"]["scaling"]["target_utilization"] * c_sla(config, profile)
    return max(1, math.floor(target + 0.5))


def _waiting(namespace: str) -> str:
    return f'(sum(vllm:num_requests_waiting{{namespace="{namespace}"}}) or vector(0))'


def _demand_query(namespace: str) -> str:
    running = f'(sum(vllm:num_requests_running{{namespace="{namespace}"}}) or vector(0))'
    return f"{running} + {_waiting(namespace)}"


def _queue_query(namespace: str) -> str:
    """过去 1 分钟的最大排队数：短暂排队也要扩。"""
    return f"max_over_time({_waiting(namespace)}[1m:5s])"


def _stuck_query(namespace: str, profile: str) -> str:
    """profile 里创建超过 STUCK_SECONDS 还调度不上的 Pod 数。

    Pod 名是 vllm-<profile>-<ReplicaSet 哈希>-<后缀>：精确匹配两段，profile 名互为前缀时（g6 / g6-tp2）不会串。
    """
    created = f'kube_pod_created{{namespace="{namespace}",pod=~"vllm-{profile}-[a-z0-9]+-[a-z0-9]+"}}'
    unschedulable = f'kube_pod_status_unschedulable{{namespace="{namespace}"}} == 1'
    return f"count(((time() - {created}) > {STUCK_SECONDS}) and on(pod) ({unschedulable})) or vector(0)"


def _prometheus(query: str, threshold: int, activation: float = 0) -> Dict[str, Any]:
    metadata = {"serverAddress": PROMETHEUS_URL, "query": query, "threshold": str(threshold)}
    if activation:
        metadata["activationThreshold"] = str(activation)
    return {"type": "prometheus", "metadata": metadata}


def scaled_object(
    name: str, namespace: str, minimum: int, maximum: int, triggers: List[Dict[str, Any]]
) -> Manifest:
    # 轮询间隔和冷却期只在能缩到 0 时起作用（0↔1 由 KEDA 自己判断，其余交给 HPA），否则 KEDA 会告警
    to_zero = (
        {"pollingInterval": POLLING_INTERVAL, "cooldownPeriod": COOLDOWN_SECONDS} if minimum == 0 else {}
    )
    return {
        "apiVersion": "keda.sh/v1alpha1",
        "kind": "ScaledObject",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "scaleTargetRef": {"name": name},
            "minReplicaCount": minimum,
            "maxReplicaCount": maximum,
            **to_zero,
            # Prometheus 查不到时（扩容「失明」）：保持当前副本数，至少不低于最小值。
            # 不能降到最小值：高负载时会砍掉正在服务的副本；回退副本也不能被误杀
            "fallback": {"failureThreshold": 3, "replicas": minimum, "behavior": "currentReplicasIfHigher"},
            "advanced": {"horizontalPodAutoscalerConfig": {"behavior": copy.deepcopy(_HPA_BEHAVIOR)}},
            "triggers": triggers,
        },
    }


def _checked(name: Any) -> str:
    """名字会拼进 PromQL：只允许 K8s 名称字符，防止被篡改的配置注入查询。"""
    if not is_k8s_name(name):
        raise ValueError(f"不合法的名称（不能用于 PromQL）：{name!r}。请重新运行 ./qwenctl deploy 生成配置")
    return name


def _check_c_sla_keys(config: Mapping[str, Any], names: List[str]) -> None:
    """压测值是客户手填的：profile 名写错会被静默忽略、悄悄用回占位值，所以直接报错。"""
    unknown = sorted(set(config["advanced"]["scaling"]["c_sla"]) - set(names))
    if unknown:
        raise ValueError(f"advanced.scaling.c_sla 里的 {unknown} 不是这个部署的 GPU profile（可选：{names}）")


def scaled_objects(config: Mapping[str, Any]) -> List[Manifest]:
    namespace = _checked(config["serving"]["namespace"])
    profiles = [{**p, "name": _checked(p["name"])} for p in config["gpu"]["profiles"]]
    _check_c_sla_keys(config, [p["name"] for p in profiles])
    minimum, maximum = config["gpu"]["minReplicas"], config["gpu"]["maxReplicas"]
    primary = profiles[0]
    demand = _prometheus(_demand_query(namespace), load_threshold(config, primary))
    queue = _prometheus(_queue_query(namespace), config["advanced"]["scaling"]["waiting_threshold"])
    result = [scaled_object(f"vllm-{primary['name']}", namespace, minimum, maximum, [demand, queue])]
    for previous, fallback in zip(profiles, profiles[1:]):
        stuck = _prometheus(_stuck_query(namespace, previous["name"]), 1, activation=0.5)
        result.append(scaled_object(f"vllm-{fallback['name']}", namespace, 0, maximum, [stuck]))
    return result
