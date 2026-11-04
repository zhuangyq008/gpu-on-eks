import json

import pytest

from qwenctl.addons import PROMETHEUS_URL, keda_values, prometheus_values
from qwenctl.autoscaling import STUCK_SECONDS, c_sla, load_threshold, pod_monitor, scaled_objects
from qwenctl.catalog import DEFAULT_C_SLA

from .sample_config import generate


@pytest.fixture(scope="module")
def config():
    return json.loads(generate("new"))


def _profile(config, name):
    return next(p for p in config["gpu"]["profiles"] if p["name"] == name)


def test_thresholds_follow_target_utilization_and_c_sla(config):
    assert c_sla(config, _profile(config, "g7e-1gpu")) == 32
    assert load_threshold(config, _profile(config, "g7e-1gpu")) == 21  # round(0.65 × 32)
    assert load_threshold(config, _profile(config, "g6-tp2")) == 5  # round(0.65 × 8)


def test_measured_c_sla_overrides_catalog_placeholder(config):
    scaling = {**config["advanced"]["scaling"], "c_sla": {"g7e-1gpu": 40}}
    tuned = {**config, "advanced": {**config["advanced"], "scaling": scaling}}
    assert load_threshold(tuned, _profile(config, "g7e-1gpu")) == 26


def test_primary_scales_on_demand_and_queueing(config):
    primary, _ = scaled_objects(config)
    spec = primary["spec"]
    assert primary["metadata"] == {"name": "vllm-g7e-1gpu", "namespace": "qwen-vl"}
    assert spec["scaleTargetRef"] == {"name": "vllm-g7e-1gpu"}
    assert (spec["minReplicaCount"], spec["maxReplicaCount"]) == (2, 8)
    assert spec["fallback"] == {"failureThreshold": 3, "replicas": 2, "behavior": "currentReplicasIfHigher"}
    demand, queue = spec["triggers"]
    assert demand["metadata"]["serverAddress"] == PROMETHEUS_URL
    assert 'vllm:num_requests_running{namespace="qwen-vl"}' in demand["metadata"]["query"]
    assert "or vector(0)" in demand["metadata"]["query"]
    assert demand["metadata"]["threshold"] == "21"
    assert "max_over_time" in queue["metadata"]["query"] and queue["metadata"]["threshold"] == "4"


def test_fallback_follows_unschedulable_primary_pods(config):
    _, fallback = scaled_objects(config)
    spec = fallback["spec"]
    assert fallback["metadata"]["name"] == "vllm-g6-tp2"
    assert spec["minReplicaCount"] == 0 and spec["maxReplicaCount"] == 8
    (trigger,) = spec["triggers"]
    query = trigger["metadata"]["query"]
    # 看「调度不上」而不是 Pending：镜像拉取期间 Pod 也是 Pending，正常冷启动会误触发回退
    assert "kube_pod_status_unschedulable" in query and "Pending" not in query
    assert 'pod=~"vllm-g7e-1gpu-[a-z0-9]+-[a-z0-9]+"' in query
    assert f"> {STUCK_SECONDS}" in query
    assert trigger["metadata"]["threshold"] == "1" and trigger["metadata"]["activationThreshold"] == "0.5"


def test_fallbacks_relay_instead_of_all_scaling_at_once(config):
    """3 个 profile 时：第二个看主力，第三个看第二个。主力卡住时不会同时扩出两个回退。"""
    base = config["gpu"]["profiles"]
    third = {**base[1], "name": "g6e-1gpu"}
    three = {**config, "gpu": {**config["gpu"], "profiles": [base[0], third, base[1]]}}
    objects = scaled_objects(three)
    assert [o["metadata"]["name"] for o in objects] == ["vllm-g7e-1gpu", "vllm-g6e-1gpu", "vllm-g6-tp2"]
    watched = [o["spec"]["triggers"][0]["metadata"]["query"] for o in objects[1:]]
    assert '"vllm-g7e-1gpu-[a-z0-9]+-[a-z0-9]+"' in watched[0]
    assert '"vllm-g6e-1gpu-[a-z0-9]+-[a-z0-9]+"' in watched[1]


def _balanced(query):
    pairs, stack = {")": "(", "]": "[", "}": "{"}, []
    in_string = False
    for ch in query:
        if ch == '"':
            in_string = not in_string
        elif in_string:
            continue
        elif ch in "([{":
            stack.append(ch)
        elif ch in pairs and (not stack or stack.pop() != pairs[ch]):
            return False
    return not stack and not in_string


def test_every_query_is_well_formed(config):
    """语法错误的查询会让 KEDA 一直走 fallback（失明）。这里至少保证括号和引号配对；真实语法在集群上验证过。"""
    queries = [t["metadata"]["query"] for o in scaled_objects(config) for t in o["spec"]["triggers"]]
    assert len(queries) == 3 and all(_balanced(q) for q in queries)
    assert all("or vector(0)" in q for q in queries)  # 没有序列时返回 0，而不是空结果（KEDA 会报错）


def test_c_sla_for_unknown_profile_is_rejected(config):
    scaling = {**config["advanced"]["scaling"], "c_sla": {"g7e-1gpus": 40}}
    typo = {**config, "advanced": {**config["advanced"], "scaling": scaling}}
    with pytest.raises(ValueError, match="g7e-1gpus"):
        scaled_objects(typo)


def test_threshold_rounds_half_up(config):
    scaling = {**config["advanced"]["scaling"], "target_utilization": 0.5, "c_sla": {"g7e-1gpu": 5}}
    tuned = {**config, "advanced": {**config["advanced"], "scaling": scaling}}
    assert load_threshold(tuned, _profile(config, "g7e-1gpu")) == 3  # 2.5 → 3（不是银行家舍入的 2）


def test_returned_manifests_do_not_share_nested_objects(config):
    first, second = scaled_objects(config)
    first["spec"]["advanced"]["horizontalPodAutoscalerConfig"]["behavior"]["scaleUp"]["policies"].clear()
    assert scaled_objects(config)[0]["spec"]["advanced"]["horizontalPodAutoscalerConfig"]["behavior"][
        "scaleUp"
    ]["policies"]
    assert second["spec"]["advanced"]["horizontalPodAutoscalerConfig"]["behavior"]["scaleUp"]["policies"]
    values = prometheus_values("qwen-vl")
    values["kube-state-metrics"]["tolerations"].clear()
    assert values["prometheusOperator"]["tolerations"]


def test_scale_up_fast_scale_down_slow(config):
    behavior = scaled_objects(config)[0]["spec"]["advanced"]["horizontalPodAutoscalerConfig"]["behavior"]
    assert behavior["scaleUp"]["stabilizationWindowSeconds"] == 0
    assert behavior["scaleDown"]["stabilizationWindowSeconds"] == 900
    assert behavior["scaleDown"]["policies"] == [{"type": "Pods", "value": 1, "periodSeconds": 300}]


def test_pod_monitor_scrapes_vllm_every_5s_with_profile_label(config):
    monitor = pod_monitor(config)
    endpoint = monitor["spec"]["podMetricsEndpoints"][0]
    assert monitor["spec"]["selector"] == {"matchLabels": {"app": "qwen-vl"}}
    assert endpoint["interval"] == "5s" and endpoint["path"] == "/metrics"
    assert endpoint["relabelings"][0]["targetLabel"] == "gpu_profile"


def test_single_profile_region_has_no_fallback_scaler(config):
    single = {**config, "gpu": {**config["gpu"], "profiles": config["gpu"]["profiles"][:1]}}
    assert [o["metadata"]["name"] for o in scaled_objects(single)] == ["vllm-g7e-1gpu"]


def test_names_are_validated_before_going_into_promql(config):
    tampered = {**config, "serving": {**config["serving"], "namespace": 'qwen-vl"} or vector(99) #'}}
    with pytest.raises(ValueError, match="PromQL"):
        scaled_objects(tampered)
    bad_profile = [{**config["gpu"]["profiles"][0], "name": "g7e.*"}]
    with pytest.raises(ValueError):
        scaled_objects({**config, "gpu": {**config["gpu"], "profiles": bad_profile}})


def test_polling_and_cooldown_only_where_scale_to_zero_applies(config):
    primary, fallback = scaled_objects(config)
    assert "pollingInterval" not in primary["spec"] and "cooldownPeriod" not in primary["spec"]
    assert fallback["spec"]["pollingInterval"] == 10 and fallback["spec"]["cooldownPeriod"] == 600


def test_profile_without_c_sla_uses_placeholder(config):
    bare = {k: v for k, v in _profile(config, "g7e-1gpu").items() if k != "cSla"}
    assert c_sla(config, bare) == DEFAULT_C_SLA


def test_prometheus_only_loads_monitors_from_trusted_namespaces():
    """扩缩容指标决定 GPU 成本：客户业务命名空间里的 PodMonitor / 规则不能注入 vllm:* 指标。"""
    spec = prometheus_values("qwen-vl")["prometheus"]["prometheusSpec"]

    def allowed(selector):
        (expr,) = selector["matchExpressions"]
        assert expr["key"] == "kubernetes.io/metadata.name" and expr["operator"] == "In"
        return expr["values"]

    assert allowed(spec["podMonitorNamespaceSelector"]) == ["qwen-vl"]
    for key in ("serviceMonitorNamespaceSelector", "ruleNamespaceSelector", "probeNamespaceSelector"):
        assert allowed(spec[key]) == ["monitoring"]
    assert "ruleSelectorNilUsesHelmValues" not in spec


def test_prometheus_drops_scrapers_keda_does_not_need():
    values = prometheus_values("qwen-vl")
    for component in ("nodeExporter", "kubelet", "kubeApiServer", "coreDns", "grafana", "alertmanager"):
        assert values[component] == {"enabled": False}, component
    assert "prometheus-node-exporter" not in values


def test_keda_cannot_read_secrets_cluster_wide():
    permissions = keda_values()["permissions"]
    assert permissions["operator"]["restrict"]["secret"] is True
    assert permissions["metricServer"]["restrict"]["secret"] is True
