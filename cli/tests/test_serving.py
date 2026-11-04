import json
import re

import pytest

from qwenctl import compile_cache
from qwenctl.serving import (
    base_manifests,
    image_ref,
    model_prefix,
    sync_job,
    sync_job_name,
    vllm_args,
    vllm_deployments,
)
from qwenctl.versions import load_versions

from .sample_config import generate

BUCKET = "qwen-vl-serving-modelbucket-abc"
VERSIONS = load_versions()


@pytest.fixture(scope="module")
def config():
    return json.loads(generate("new"))


def _profile(config, name):
    return next(p for p in config["gpu"]["profiles"] if p["name"] == name)


def test_image_is_pinned_by_digest_and_prefix_includes_revision(config):
    assert image_ref(VERSIONS) == f"docker.io/vllm/vllm-openai@{VERSIONS['images']['vllm']['digest']}"
    assert model_prefix(config, VERSIONS) == "models/qwen3.5-35b-a3b-fp8/9d1823d2dee6/"
    assert re.fullmatch(r"model-sync-9d1823d2-[0-9a-f]{6}", sync_job_name(VERSIONS, {"spec": {}}))


def test_vllm_args_follow_profile_and_advanced_config(config):
    args = vllm_args(config, VERSIONS, BUCKET, _profile(config, "g6-tp2"))
    assert args[0] == f"s3://{BUCKET}/models/qwen3.5-35b-a3b-fp8/9d1823d2dee6/"
    assert "--load-format=runai_streamer" in args
    assert '--model-loader-extra-config={"concurrency": 16}' in args
    assert "--tensor-parallel-size=2" in args
    assert "--gpu-memory-utilization=0.92" in args
    # g6-tp2（2 × L4）收紧到实测可用的组合
    assert "--max-model-len=8192" in args and "--max-num-seqs=16" in args
    assert '--limit-mm-per-prompt={"image": 2, "video": 0}' in args
    g7e = vllm_args(config, VERSIONS, BUCKET, _profile(config, "g7e-1gpu"))
    assert (
        "--tensor-parallel-size=1" in g7e and "--max-model-len=32768" in g7e and "--max-num-seqs=128" in g7e
    )
    assert '--limit-mm-per-prompt={"image": 4, "video": 0}' in g7e


def test_profile_caps_never_raise_customer_settings(config):
    from qwenctl.serving import effective_limits

    serving = {**config["advanced"]["serving"], "max_model_len": 4096}
    low = {**config, "advanced": {**config["advanced"], "serving": serving}}
    assert effective_limits(low, _profile(config, "g6-tp2"))["max_model_len"] == 4096
    unlimited = effective_limits(config, {"servingLimits": None})
    assert unlimited == {"max_model_len": 32768, "max_num_seqs": 128, "max_images": 4}


def test_primary_profile_runs_min_replicas_and_fallback_starts_at_zero(config):
    deployments = {d["metadata"]["name"]: d for d in vllm_deployments(config, VERSIONS, BUCKET)}
    assert deployments["vllm-g7e-1gpu"]["spec"]["replicas"] == config["gpu"]["minReplicas"]
    assert deployments["vllm-g6-tp2"]["spec"]["replicas"] == 0


def test_deployment_pod_spec(config):
    (g7e, g6) = vllm_deployments(config, VERSIONS, BUCKET)
    spec = g6["spec"]["template"]["spec"]
    container = spec["containers"][0]
    assert spec["nodeSelector"] == {"qwenctl/gpu-profile": "g6-tp2"}
    assert spec["serviceAccountName"] == "vllm"
    assert {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"} in spec["tolerations"]
    assert container["resources"]["requests"] == {"cpu": "20", "memory": "88Gi", "nvidia.com/gpu": 2}
    assert g7e["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == 1
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["HF_HUB_OFFLINE"] == "1" and env["AWS_REGION"] == "ap-southeast-5"
    assert container["startupProbe"]["failureThreshold"] * container["startupProbe"]["periodSeconds"] == 1800
    assert g6["spec"]["strategy"]["rollingUpdate"] == {"maxSurge": 0, "maxUnavailable": 1}
    assert g6["metadata"]["labels"]["app"] == "qwen-vl"


def test_base_manifests_share_one_service(config):
    kinds = [(m["kind"], m["metadata"]["name"]) for m in base_manifests(config)]
    assert kinds == [
        ("Namespace", "qwen-vl"),
        ("ServiceAccount", "vllm"),
        ("ServiceAccount", "model-sync"),
        ("Service", "qwen-vl"),
        ("ConfigMap", compile_cache.configmap_name()),
    ]
    service = next(m for m in base_manifests(config) if m["kind"] == "Service")
    assert service["spec"]["selector"] == {"app": "qwen-vl"}


def test_sync_job_runs_on_general_cpu_nodes_with_pinned_image(config):
    configmap, job = sync_job(config, VERSIONS, BUCKET)
    assert "hf_hub_download" in configmap["data"]["model_sync.py"]
    volume = next(v for v in job["spec"]["template"]["spec"]["volumes"] if v["name"] == "script")
    assert volume["configMap"]["name"] == configmap["metadata"]["name"]
    assert re.fullmatch(r"model-sync-script-[0-9a-f]{6}", configmap["metadata"]["name"])
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "model-sync"
    assert pod["nodeSelector"] == {"qwenctl/node-role": "general", "kubernetes.io/arch": "amd64"}
    assert "tolerations" not in pod
    container = pod["containers"][0]
    assert container["image"] == image_ref(VERSIONS)
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["MODEL_REVISION"] == VERSIONS["model"]["revision"]
    assert env["S3_PREFIX"].endswith("/") and env["S3_BUCKET"] == BUCKET


def test_containers_drop_capabilities_and_sync_runs_as_non_root(config):
    (g7e, _) = vllm_deployments(config, VERSIONS, BUCKET)
    _, job = sync_job(config, VERSIONS, BUCKET)
    for container in (
        g7e["spec"]["template"]["spec"]["containers"][0],
        job["spec"]["template"]["spec"]["containers"][0],
    ):
        assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
        assert container["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
    pod = job["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True and pod["securityContext"]["runAsUser"] == 1000
    env = {e["name"]: e["value"] for e in pod["containers"][0]["env"]}
    assert env["HF_HOME"].startswith("/tmp")
    assert job["spec"]["ttlSecondsAfterFinished"] == 86400


def test_namespace_enforces_pod_security_baseline(config):
    namespace = base_manifests(config)[0]
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "baseline"


def test_job_name_changes_whenever_the_pod_template_changes(config, monkeypatch, tmp_path):
    from qwenctl import serving

    def name(cfg=config):
        return next(m for m in sync_job(cfg, VERSIONS, BUCKET) if m["kind"] == "Job")["metadata"]["name"]

    before = name()
    assert name() == before  # 同样的输入，同样的名字（重复执行不会新建 Job）
    assert name({**config, "region": "us-east-1"}) != before  # 环境变量变了
    changed = tmp_path / "model_sync.py"
    changed.write_text("print('v2')", encoding="utf-8")
    monkeypatch.setattr(serving, "SYNC_SCRIPT", str(changed))
    assert name() != before  # 脚本变了（ConfigMap 名字也跟着变）


def _pod_spec(config, name="vllm-g6-tp2"):
    deployment = next(d for d in vllm_deployments(config, VERSIONS, BUCKET) if d["metadata"]["name"] == name)
    return deployment["spec"]["template"]


def test_vllm_restores_compile_cache_before_starting(config):
    template = _pod_spec(config)
    key = template["metadata"]["annotations"][compile_cache.ANNOTATION]
    assert key.startswith("compile-cache/g6-tp2/") and key.endswith("/vllm-cache.tar.gz")
    (init,) = template["spec"]["initContainers"]
    vllm = template["spec"]["containers"][0]
    assert init["image"] == vllm["image"]  # 复用 vLLM 镜像，节点上已经有了
    assert init["command"][-3:] == [BUCKET, key, "/models/vllm-cache"]
    assert {"name": "cache", "mountPath": "/models"} in init["volumeMounts"]
    assert init["securityContext"] == vllm["securityContext"]
    env = {e["name"]: e["value"] for e in vllm["env"]}
    assert env["VLLM_CACHE_ROOT"] == "/models/vllm-cache"
    volumes = {v["name"]: v for v in template["spec"]["volumes"]}
    assert volumes["compile-cache-restore"]["configMap"]["name"] == compile_cache.configmap_name()


def test_compile_cache_key_changes_with_anything_that_affects_compiled_code(config):
    def key(cfg=config, versions=VERSIONS, name="vllm-g6-tp2"):
        deployment = next(d for d in vllm_deployments(cfg, versions, BUCKET) if d["metadata"]["name"] == name)
        return deployment["spec"]["template"]["metadata"]["annotations"][compile_cache.ANNOTATION]

    base = key()
    assert key() == base
    assert key(name="vllm-g7e-1gpu") != base  # 不同 GPU
    other_image = {
        **VERSIONS,
        "images": {"vllm": {**VERSIONS["images"]["vllm"], "digest": "sha256:" + "1" * 64}},
    }
    assert key(versions=other_image) != base
    serving = {**config["advanced"]["serving"], "max_num_seqs": 8}
    assert key(cfg={**config, "advanced": {**config["advanced"], "serving": serving}}) != base  # vLLM 参数
