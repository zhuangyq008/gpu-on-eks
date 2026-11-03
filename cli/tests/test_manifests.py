import json

import pytest

from qwenctl.manifests import ManifestError, gpu_limit, render

from .sample_config import generate

OUTPUTS = {
    "KarpenterNodeRoleName": "qwen-vl-eks-KarpenterNodeRole-ABC",
    "ClusterSecurityGroupId": "sg-0123456789abcdef0",
    "PrivateSubnetIds": "subnet-0aaa,subnet-0bbb",
}


@pytest.fixture(scope="module")
def manifests():
    return render(json.loads(generate("new")), OUTPUTS, "v1.68.1")


def _by(manifests, kind, name):
    return next(m for m in manifests if m["kind"] == kind and m["metadata"]["name"] == name)


def _req(nodepool, key):
    reqs = nodepool["spec"]["template"]["spec"]["requirements"]
    return next(r for r in reqs if r["key"] == key)


def test_renders_two_nodeclasses_and_three_nodepools(manifests):
    kinds = [(m["kind"], m["metadata"]["name"]) for m in manifests]
    assert kinds == [
        ("EC2NodeClass", "general"),
        ("EC2NodeClass", "gpu"),
        ("NodePool", "general"),
        ("NodePool", "gpu-g7e-1gpu"),
        ("NodePool", "gpu-g6-tp2"),
    ]


def test_nodeclass_uses_stack_outputs_pinned_ami_and_imdsv2(manifests):
    spec = _by(manifests, "EC2NodeClass", "general")["spec"]
    assert spec["role"] == "qwen-vl-eks-KarpenterNodeRole-ABC"
    assert spec["amiSelectorTerms"] == [{"alias": "bottlerocket@v1.68.1"}]
    assert spec["subnetSelectorTerms"] == [{"id": "subnet-0aaa"}, {"id": "subnet-0bbb"}]
    assert spec["securityGroupSelectorTerms"] == [{"id": "sg-0123456789abcdef0"}]
    assert spec["metadataOptions"]["httpTokens"] == "required"
    assert spec["metadataOptions"]["httpPutResponseHopLimit"] == 1
    assert all(d["ebs"]["encrypted"] for d in spec["blockDeviceMappings"])
    assert spec["tags"]["qwenctl:deployment"] == "qwen-vl"
    gpu = _by(manifests, "EC2NodeClass", "gpu")["spec"]["blockDeviceMappings"][1]["ebs"]
    assert gpu["volumeSize"] == "150Gi" and gpu["iops"] == 16000


def test_general_nodepool_for_customer_workloads(manifests):
    pool = _by(manifests, "NodePool", "general")
    template = pool["spec"]["template"]["spec"]
    assert "taints" not in template
    assert _req(pool, "kubernetes.io/arch")["values"] == ["amd64"]
    assert _req(pool, "karpenter.sh/capacity-type")["values"] == ["on-demand"]
    assert _req(pool, "topology.k8s.aws/zone-id")["values"] == ["apse5-az1", "apse5-az2"]
    assert _req(pool, "karpenter.k8s.aws/instance-generation") == {
        "key": "karpenter.k8s.aws/instance-generation",
        "operator": "Gt",
        "values": ["5"],
    }
    assert pool["spec"]["limits"] == {"cpu": "200"}
    assert pool["spec"]["disruption"]["consolidationPolicy"] == "WhenEmptyOrUnderutilized"


def test_gpu_nodepools_fall_back_by_weight_and_never_evict_busy_nodes(manifests):
    g7e = _by(manifests, "NodePool", "gpu-g7e-1gpu")
    g6 = _by(manifests, "NodePool", "gpu-g6-tp2")
    assert g7e["spec"]["weight"] == 100 and g6["spec"]["weight"] == 10
    assert _req(g7e, "topology.k8s.aws/zone-id")["values"] == ["apse5-az2"]
    assert _req(g6, "topology.k8s.aws/zone-id")["values"] == ["apse5-az1", "apse5-az2"]
    assert _req(g6, "node.kubernetes.io/instance-type")["values"] == ["g6.12xlarge"]
    for pool in (g7e, g6):
        assert pool["spec"]["template"]["spec"]["taints"] == [
            {"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}
        ]
        assert pool["spec"]["disruption"] == {"consolidationPolicy": "WhenEmpty", "consolidateAfter": "10m"}
    assert g7e["spec"]["limits"] == {"nvidia.com/gpu": "8"}
    assert g6["spec"]["limits"] == {"nvidia.com/gpu": "16"}


@pytest.mark.parametrize(
    "per_node,tp,max_replicas,expected", [(1, 1, 8, 8), (2, 2, 8, 16), (2, 2, 3, 8), (1, 2, 3, 6)]
)
def test_gpu_limit(per_node, tp, max_replicas, expected):
    assert gpu_limit({"replicasPerNode": per_node, "tensorParallel": tp}, max_replicas) == expected


def test_missing_outputs():
    config = json.loads(generate("new"))
    with pytest.raises(ManifestError, match="ClusterSecurityGroupId"):
        render(config, {"KarpenterNodeRoleName": "r", "PrivateSubnetIds": "a"}, "v1")


def test_customer_tags_cannot_override_deployment_tag():
    config = json.loads(generate("new"))
    config["advanced"]["compliance"]["tags"] = {"qwenctl:deployment": "spoofed", "CostCenter": "mfg"}
    tags = render(config, OUTPUTS, "v1")[0]["spec"]["tags"]
    assert tags == {"CostCenter": "mfg", "qwenctl:deployment": "qwen-vl"}
