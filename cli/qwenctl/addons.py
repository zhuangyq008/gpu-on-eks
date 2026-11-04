"""集群插件的 Helm values：集群内 Prometheus（kube-prometheus-stack 精简版）和 KEDA。

都跑在系统节点组上（带 CriticalAddonsOnly 污点），不依赖通用节点池扩容。
Prometheus 只做 KEDA 的扩缩容查询：保留 6 小时、不对外写；可观测性（CloudWatch 模式）另行接入（docs/02 §1.2）。

扩缩容指标决定 GPU 副本数（也就是成本），所以 Prometheus 只读两个可信命名空间里的监控对象：
推理服务命名空间（vLLM 的 PodMonitor）和 monitoring（kube-state-metrics 等自带的 ServiceMonitor）。
客户业务命名空间里的 PodMonitor / 规则不会被加载，没法伪造 vllm:* 或 kube_pod_* 指标。
"""

from __future__ import annotations

from typing import Any, Dict

PROMETHEUS_NAMESPACE = "monitoring"
PROMETHEUS_RELEASE = "kps"
PROMETHEUS_URL = f"http://{PROMETHEUS_RELEASE}-prometheus.{PROMETHEUS_NAMESPACE}.svc:9090"
KEDA_NAMESPACE = "keda"


def _on_system_nodes() -> Dict[str, Any]:
    """每次返回新对象：同一个 dict 嵌进多处 values，下游原地修改会互相影响。"""
    return {
        "nodeSelector": {"qwenctl/node-role": "system"},
        "tolerations": [{"key": "CriticalAddonsOnly", "operator": "Exists"}],
    }


def namespace_manifest(name: str) -> Dict[str, Any]:
    """强制 Pod Security baseline（禁止特权、hostPath、host 网络等），对 restricted 只告警。"""
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": name,
            "labels": {
                "pod-security.kubernetes.io/enforce": "baseline",
                "pod-security.kubernetes.io/warn": "restricted",
            },
        },
    }


def _only(*namespaces: str) -> Dict[str, Any]:
    return {
        "matchExpressions": [
            {"key": "kubernetes.io/metadata.name", "operator": "In", "values": list(namespaces)}
        ]
    }


def prometheus_values(serving_namespace: str) -> Dict[str, Any]:
    return {
        "fullnameOverride": PROMETHEUS_RELEASE,
        "grafana": {"enabled": False},
        "alertmanager": {"enabled": False},
        "defaultRules": {"create": False},
        # 只保留 KEDA 要用的：vLLM 指标（PodMonitor）+ kube-state-metrics（Pod 状态）。
        # 控制面组件在 EKS 上抓不到；kubelet/cAdvisor、API server、CoreDNS 序列多、占内存；
        # node-exporter 要 hostNetwork + hostPID 跑在每个节点（包括客户业务节点），都不需要
        "kubeControllerManager": {"enabled": False},
        "kubeScheduler": {"enabled": False},
        "kubeEtcd": {"enabled": False},
        "kubeProxy": {"enabled": False},
        "kubelet": {"enabled": False},
        "kubeApiServer": {"enabled": False},
        "coreDns": {"enabled": False},
        "nodeExporter": {"enabled": False},
        "prometheusOperator": {
            **_on_system_nodes(),
            "admissionWebhooks": {"patch": _on_system_nodes()},
            "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"memory": "256Mi"}},
        },
        "kube-state-metrics": {
            **_on_system_nodes(),
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
        },
        "prometheus": {
            "prometheusSpec": {
                **_on_system_nodes(),
                "retention": "6h",
                "scrapeInterval": "15s",
                "evaluationInterval": "30s",
                # 不要求 release 标签（vLLM 的 PodMonitor 不是这个 chart 建的），但只看可信命名空间
                "serviceMonitorSelectorNilUsesHelmValues": False,
                "podMonitorSelectorNilUsesHelmValues": False,
                "podMonitorNamespaceSelector": _only(serving_namespace),
                "serviceMonitorNamespaceSelector": _only(PROMETHEUS_NAMESPACE),
                "ruleNamespaceSelector": _only(PROMETHEUS_NAMESPACE),
                "probeNamespaceSelector": _only(PROMETHEUS_NAMESPACE),
                "scrapeConfigNamespaceSelector": _only(PROMETHEUS_NAMESPACE),
                "resources": {"requests": {"cpu": "300m", "memory": "1Gi"}, "limits": {"memory": "2Gi"}},
            }
        },
    }


def keda_values() -> Dict[str, Any]:
    return {
        **_on_system_nodes(),
        # 只用 Prometheus 触发器、不用 TriggerAuthentication：不需要读全集群的 Secret
        "permissions": {
            "operator": {"restrict": {"secret": True}},
            "metricServer": {"restrict": {"secret": True}},
        },
        "resources": {
            "operator": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
            "metricServer": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
            "webhooks": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
        },
    }
