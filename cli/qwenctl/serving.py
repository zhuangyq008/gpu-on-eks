"""推理服务的 K8s 清单：命名空间、ServiceAccount、权重同步 Job、每个 GPU profile 一个 vLLM Deployment。

- 一个 Deployment 对应一个 GPU profile（TP 大小、GPU 数不同，不能共用一个 Pod 模板）；共用一个 Service。
- 主力 profile 的副本数 = minReplicas，回退 profile 从 0 开始；回退的自动扩容由 KEDA 负责（docs/03 §9 待实现）。
- 权重用 Run:ai Model Streamer 直接从 S3 流式加载（docs/01 §4.2），不经过本地磁盘。
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import AbstractSet, Any, Dict, List, Mapping, Optional

from .addons import namespace_manifest

Manifest = Dict[str, Any]

APP_LABEL = {"app": "qwen-vl"}
SERVED_MODEL_NAME = "qwen-vl"
VLLM_PORT = 8000
STREAMER_CONCURRENCY = 16
STARTUP_TIMEOUT_SECONDS = 1800
SYNC_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "model_sync.py")


def model_prefix(config: Mapping[str, Any], versions: Mapping[str, Any]) -> str:
    """s3 前缀里带上 revision：换模型版本不会覆盖正在使用的权重。"""
    model = versions["model"]
    name = model["id"].split("/")[-1].lower()
    return f"{config['serving']['modelPrefix']}{name}/{model['revision'][:12]}/"


def image_ref(versions: Mapping[str, Any]) -> str:
    """按 digest 引用镜像（tag 可以被重新推送）。"""
    image = versions["images"]["vllm"]
    repository = image["tag"].rsplit(":", 1)[0]
    return f"{repository}@{image['digest']}"


# 去掉全部 capabilities + seccomp：两者都不影响 GPU 访问（设备由 NVIDIA 运行时挂载）
BASE_SECURITY_CONTEXT = {
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
    "seccompProfile": {"type": "RuntimeDefault"},
}
SYNC_USER = 1000


def _sync_script() -> str:
    with open(SYNC_SCRIPT, encoding="utf-8") as handle:
        return handle.read()


def _sync_digest(versions: Mapping[str, Any]) -> str:
    return hashlib.sha256((_sync_script() + image_ref(versions)).encode()).hexdigest()[:6]


def sync_job_name(versions: Mapping[str, Any], template: Mapping[str, Any]) -> str:
    """Job 的 Pod 模板不可变：名字里带上整个模板的哈希，模板有任何变化就换一个新 Job。"""
    digest = hashlib.sha256(json.dumps(template, sort_keys=True).encode()).hexdigest()[:6]
    return f"model-sync-{versions['model']['revision'][:8]}-{digest}"


def base_manifests(config: Mapping[str, Any]) -> List[Manifest]:
    serving = config["serving"]
    ns = serving["namespace"]
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": SERVED_MODEL_NAME, "namespace": ns, "labels": APP_LABEL},
        "spec": {
            "selector": APP_LABEL,
            "ports": [{"name": "http", "port": VLLM_PORT, "targetPort": VLLM_PORT}],
        },
    }
    return [
        namespace_manifest(ns),
        _service_account(ns, serving["vllmServiceAccount"]),
        _service_account(ns, serving["syncServiceAccount"]),
        service,
    ]


def _service_account(namespace: str, name: str) -> Manifest:
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": name, "namespace": namespace},
        "automountServiceAccountToken": False,
    }


def sync_job(config: Mapping[str, Any], versions: Mapping[str, Any], bucket: str) -> List[Manifest]:
    """权重同步：跑在通用 CPU 节点上，用 vLLM 镜像（自带 huggingface_hub + boto3）。"""
    ns = config["serving"]["namespace"]
    script = _sync_script()
    env = {
        "MODEL_ID": versions["model"]["id"],
        "MODEL_REVISION": versions["model"]["revision"],
        "S3_BUCKET": bucket,
        "S3_PREFIX": model_prefix(config, versions),
        "AWS_REGION": config["region"],
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HOME": "/tmp/hf",  # 非 root 用户没有可写的 HOME
        "HOME": "/tmp",
    }
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        # 名字带哈希：脚本更新时不会改掉正在运行的旧 Job 用的脚本
        "metadata": {"name": f"model-sync-script-{_sync_digest(versions)}", "namespace": ns},
        "data": {"model_sync.py": script},
    }
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"namespace": ns, "labels": {"app": "model-sync"}},
        "spec": {
            "backoffLimit": 2,
            "activeDeadlineSeconds": 3600,
            "ttlSecondsAfterFinished": 86400,
            "template": {
                "metadata": {"labels": {"app": "model-sync"}},
                "spec": {
                    "serviceAccountName": config["serving"]["syncServiceAccount"],
                    "restartPolicy": "Never",
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": SYNC_USER,
                        "runAsGroup": SYNC_USER,
                    },
                    "nodeSelector": {"qwenctl/node-role": "general", "kubernetes.io/arch": "amd64"},
                    "containers": [
                        {
                            "name": "sync",
                            "image": image_ref(versions),
                            "command": ["python3", "/scripts/model_sync.py"],
                            "env": [{"name": k, "value": v} for k, v in env.items()],
                            "resources": {
                                "requests": {"cpu": "2", "memory": "4Gi"},
                                "limits": {"memory": "8Gi"},
                            },
                            "securityContext": BASE_SECURITY_CONTEXT,
                            "volumeMounts": [
                                {"name": "script", "mountPath": "/scripts", "readOnly": True},
                                {"name": "work", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "script",
                            "configMap": {"name": f"model-sync-script-{_sync_digest(versions)}"},
                        },
                        {"name": "work", "emptyDir": {"sizeLimit": "20Gi"}},
                    ],
                },
            },
        },
    }
    name = sync_job_name(versions, job["spec"]["template"])
    return [configmap, {**job, "metadata": {"name": name, **job["metadata"]}}]


def effective_limits(config: Mapping[str, Any], profile: Mapping[str, Any]) -> Dict[str, int]:
    """高级配置的值和 profile 的上限取较小者（显存小的回退机型需要收紧）。"""
    serving = config["advanced"]["serving"]
    caps = profile.get("servingLimits") or {}

    def pick(value: int, cap: Any) -> int:
        return min(value, cap) if isinstance(cap, int) else value

    return {
        "max_model_len": pick(serving["max_model_len"], caps.get("maxModelLen")),
        "max_num_seqs": pick(serving["max_num_seqs"], caps.get("maxNumSeqs")),
        "max_images": pick(serving["max_images_per_request"], caps.get("maxImages")),
    }


def vllm_args(
    config: Mapping[str, Any], versions: Mapping[str, Any], bucket: str, profile: Mapping[str, Any]
) -> List[str]:
    serving = config["advanced"]["serving"]
    limits = effective_limits(config, profile)
    mm_limit = {"image": limits["max_images"], "video": 0}
    return [
        f"s3://{bucket}/{model_prefix(config, versions)}",
        "--load-format=runai_streamer",
        f"--model-loader-extra-config={json.dumps({'concurrency': STREAMER_CONCURRENCY})}",
        f"--served-model-name={SERVED_MODEL_NAME}",
        f"--tensor-parallel-size={profile['tensorParallel']}",
        f"--max-model-len={limits['max_model_len']}",
        f"--max-num-seqs={limits['max_num_seqs']}",
        f"--gpu-memory-utilization={serving['gpu_memory_utilization']}",
        f"--limit-mm-per-prompt={json.dumps(mm_limit)}",
        "--host=0.0.0.0",
        f"--port={VLLM_PORT}",
    ]


def vllm_deployment(
    config: Mapping[str, Any],
    versions: Mapping[str, Any],
    bucket: str,
    profile: Mapping[str, Any],
    replicas: Optional[int],
) -> Manifest:
    labels = {**APP_LABEL, "qwenctl/gpu-profile": profile["name"]}
    gpus = profile["tensorParallel"]
    env = {
        "AWS_REGION": config["region"],
        "HF_HUB_OFFLINE": "1",  # 启动过程不访问外网
        "VLLM_CACHE_ROOT": "/models/vllm-cache",
    }
    container = {
        "name": "vllm",
        "image": image_ref(versions),
        "command": ["vllm", "serve"],
        "args": vllm_args(config, versions, bucket, profile),
        "env": [{"name": k, "value": v} for k, v in env.items()],
        "ports": [{"name": "http", "containerPort": VLLM_PORT}],
        "resources": {
            "requests": {"cpu": profile["podCpu"], "memory": profile["podMemory"], "nvidia.com/gpu": gpus},
            "limits": {"memory": profile["podMemory"], "nvidia.com/gpu": gpus},
        },
        "startupProbe": _probe(10, STARTUP_TIMEOUT_SECONDS // 10),
        "readinessProbe": _probe(5, 3),
        # 先从网关摘掉流量，再停止接收请求
        "lifecycle": {"preStop": {"exec": {"command": ["sleep", "15"]}}},
        "volumeMounts": [{"name": "cache", "mountPath": "/models"}, {"name": "shm", "mountPath": "/dev/shm"}],
        "securityContext": BASE_SECURITY_CONTEXT,
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": f"vllm-{profile['name']}",
            "namespace": config["serving"]["namespace"],
            "labels": labels,
        },
        "spec": {
            # 不多开：多开一个副本就要多一台 GPU 节点
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": config["serving"]["vllmServiceAccount"],
                    "nodeSelector": {"qwenctl/gpu-profile": profile["name"]},
                    "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
                    "topologySpreadConstraints": [
                        {
                            "maxSkew": 1,
                            "topologyKey": "topology.k8s.aws/zone-id",
                            "whenUnsatisfiable": "ScheduleAnyway",
                            "labelSelector": {"matchLabels": APP_LABEL},
                        }
                    ],
                    "terminationGracePeriodSeconds": 180,
                    "containers": [container],
                    "volumes": [
                        {"name": "cache", "emptyDir": {}},
                        {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "16Gi"}},
                    ],
                },
            },
        },
    }
    if replicas is None:  # 已存在的 Deployment：副本数归 KEDA 管，不覆盖
        return deployment
    return {**deployment, "spec": {"replicas": replicas, **deployment["spec"]}}


def _probe(period: int, failures: int) -> Dict[str, Any]:
    return {
        "httpGet": {"path": "/health", "port": VLLM_PORT},
        "periodSeconds": period,
        "failureThreshold": failures,
    }


def vllm_deployments(
    config: Mapping[str, Any],
    versions: Mapping[str, Any],
    bucket: str,
    existing: AbstractSet[str] = frozenset(),
) -> List[Manifest]:
    """首次创建时：主力 profile 跑 minReplicas，其余从 0 开始。已经存在的 Deployment 不写副本数（由 KEDA 管）。"""
    profiles = config["gpu"]["profiles"]
    minimum = config["gpu"]["minReplicas"]
    result = []
    for index, profile in enumerate(profiles):
        name = f"vllm-{profile['name']}"
        initial = None if name in existing else (minimum if index == 0 else 0)
        result.append(vllm_deployment(config, versions, bucket, profile, initial))
    return result
