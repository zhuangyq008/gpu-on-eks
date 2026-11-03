import pytest

from qwenctl.capacity import (
    HOURS_PER_MONTH,
    Quota,
    estimate_cost,
    max_replicas_by_quota,
    replica_defaults,
    replica_upper_bound,
    validate_replicas,
)
from qwenctl.catalog import profile_by_name

G7E = profile_by_name("g7e-1gpu")
G6 = profile_by_name("g6-tp2")


def test_quota_available_never_negative():
    assert Quota(32, 40).available_vcpus == 0.0
    assert Quota(32, 8).available_vcpus == 24.0


@pytest.mark.parametrize(
    "profile,vcpus,expected",
    [(G7E, 768, 96), (G7E, 15, 1), (G6, 768, 32), (G6, 47, 0), (G6, 48, 2)],
)
def test_max_replicas_by_quota(profile, vcpus, expected):
    assert max_replicas_by_quota(profile, vcpus) == expected


def test_upper_bound_defaults_to_eight_when_quota_unknown():
    assert replica_upper_bound(G7E, None) == 8
    assert replica_upper_bound(G7E, 32) == 4


def test_replica_defaults():
    assert replica_defaults(96).min_replicas == 2
    assert replica_defaults(96).max_replicas == 8
    assert (replica_defaults(1).min_replicas, replica_defaults(1).max_replicas) == (1, 1)
    with pytest.raises(ValueError):
        replica_defaults(0)


@pytest.mark.parametrize(
    "lo,hi,upper,error",
    [(2, 8, 10, None), (0, 2, 10, "至少为 1"), (3, 2, 10, "不能小于"), (2, 11, 10, "不能超过 10")],
)
def test_validate_replicas(lo, hi, upper, error):
    result = validate_replicas(lo, hi, upper)
    if error is None:
        assert result is None
    else:
        assert error in result


def test_estimate_cost_single_gpu():
    cost = estimate_cost(G7E, 5.0, 2, 8)
    assert cost.baseline_monthly == pytest.approx(2 * 5.0 * HOURS_PER_MONTH)
    assert cost.peak_hourly == pytest.approx(40.0)


def test_estimate_cost_rounds_up_to_whole_nodes_for_tp2():
    cost = estimate_cost(G6, 6.0, 3, 3)
    assert cost.baseline_monthly == pytest.approx(2 * 6.0 * HOURS_PER_MONTH)


def test_estimate_cost_unknown_price():
    cost = estimate_cost(G7E, None, 2, 8)
    assert cost.baseline_monthly is None and cost.peak_hourly is None
