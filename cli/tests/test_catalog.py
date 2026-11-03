import pytest

from qwenctl.catalog import (
    PROFILES,
    gpu_instance_types,
    is_gpu_family,
    is_standard_family,
    profile_by_name,
)


def test_tp2_profile_packs_two_replicas_per_node():
    p = profile_by_name("g6-tp2")
    assert p.replicas_per_node == 2
    assert p.nodes_for(3) == 2
    assert p.vcpus_for(3) == 96


def test_single_gpu_profile_uses_one_node_per_replica():
    p = profile_by_name("g7e-1gpu")
    assert p.replicas_per_node == 1
    assert p.vcpus_for(4) == 32
    assert p.instance_types == ("g7e.2xlarge", "g7e.4xlarge")


def test_nodepool_weight_follows_priority():
    weights = [p.nodepool_weight for p in sorted(PROFILES, key=lambda p: -p.priority)]
    assert weights == [100, 50, 10]


def test_unknown_profile_raises():
    with pytest.raises(KeyError):
        profile_by_name("nope")


def test_gpu_instance_types_lists_all_profile_types():
    assert "g6.12xlarge" in gpu_instance_types()
    assert "g6e.4xlarge" in gpu_instance_types()


@pytest.mark.parametrize(
    "itype,expected",
    [("g7e.2xlarge", True), ("gr6.4xlarge", True), ("vt1.3xlarge", True), ("m7g.large", False)],
)
def test_is_gpu_family(itype, expected):
    assert is_gpu_family(itype) is expected


@pytest.mark.parametrize(
    "itype,expected",
    [
        ("t3.micro", True),
        ("c7i.large", True),
        ("m7g.large", True),
        ("trn1.2xlarge", False),
        ("inf2.xlarge", False),
        ("dl1.24xlarge", False),
        ("p5.48xlarge", False),
        ("g6.xlarge", False),
    ],
)
def test_is_standard_family(itype, expected):
    assert is_standard_family(itype) is expected
