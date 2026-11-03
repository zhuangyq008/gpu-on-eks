from qwenctl.network import (
    ISOLATED,
    MIN_FREE_IPS,
    PRIVATE,
    PUBLIC,
    RouteTable,
    Subnet,
    classify_subnets,
    eligible_zone_ids,
    pick_subnets,
    rejection_reasons,
    route_kind,
)

from .conftest import ROUTE_TABLES, SUBNETS


def test_route_kind():
    assert route_kind("igw-1") == PUBLIC
    assert route_kind("nat-1") == PRIVATE
    assert route_kind("tgw-1") == PRIVATE
    assert route_kind("vpce-1") == PRIVATE
    assert route_kind(None) == ISOLATED


def test_explicit_association_and_main_table_fallback():
    subnets = (
        Subnet("s-explicit", "az1", "a", "10.0.0.0/24", 300),
        Subnet("s-main", "az1", "a", "10.0.1.0/24", 300),
    )
    tables = (
        RouteTable("rt-1", ("s-explicit",), False, "nat-1"),
        RouteTable("rt-main", (), True, "igw-1"),
    )
    result = {c.subnet.subnet_id: (c.kind, c.route_table_id) for c in classify_subnets(subnets, tables)}
    assert result == {"s-explicit": (PRIVATE, "rt-1"), "s-main": (PUBLIC, "rt-main")}


def test_no_main_table_means_isolated():
    classified = classify_subnets((Subnet("s-1", "az1", "a", "10.0.0.0/24", 300),), ())
    assert classified[0].kind == ISOLATED
    assert classified[0].route_table_id is None


def test_eligible_zones_require_private_subnet_with_enough_ips():
    small = Subnet("s-small", "apse5-az3", "c", "10.0.200.0/28", 11)
    classified = classify_subnets(
        SUBNETS[:4] + (small,), (RouteTable("rt", ("s-small",), False, "nat-1"),) + ROUTE_TABLES
    )
    assert eligible_zone_ids(classified) == frozenset({"apse5-az1", "apse5-az2"})


def test_slash_24_with_some_ips_used_is_still_eligible():
    subnet = Subnet("s-24", "az1", "a", "10.0.5.0/24", 240)
    classified = classify_subnets((subnet,), (RouteTable("rt", ("s-24",), False, "nat-1"),))
    assert MIN_FREE_IPS <= 240
    assert eligible_zone_ids(classified) == frozenset({"az1"})


def test_pick_subnets_prefers_most_free_ips():
    subnets = (
        Subnet("s-1", "az1", "a", "10.0.0.0/20", 1000),
        Subnet("s-2", "az1", "a", "10.0.16.0/20", 4000),
        Subnet("s-3", "az2", "b", "10.0.32.0/20", 4000),
    )
    tables = (RouteTable("rt", ("s-1", "s-2", "s-3"), False, "nat-1"),)
    picked = pick_subnets(classify_subnets(subnets, tables), PRIVATE, frozenset({"az1"}))
    assert {z: c.subnet.subnet_id for z, c in picked.items()} == {"az1": "s-2"}


def test_rejection_reasons_explain_each_unusable_subnet():
    subnets = (
        Subnet("s-pub", "az1", "a", "10.0.0.0/24", 250),
        Subnet("s-iso", "az1", "a", "10.0.1.0/24", 250),
        Subnet("s-tiny", "az2", "b", "10.0.2.0/28", 11),
        Subnet("s-ok", "az2", "b", "10.0.32.0/19", 8000),
    )
    tables = (
        RouteTable("rt-pub", ("s-pub",), False, "igw-1"),
        RouteTable("rt-iso", ("s-iso",), False, None),
        RouteTable("rt-nat", ("s-tiny", "s-ok"), False, "nat-1"),
    )
    reasons = "\n".join(rejection_reasons(classify_subnets(subnets, tables)))
    assert "s-pub（a）：公有子网" in reasons
    assert "s-iso（a）：没有 0.0.0.0/0 出口" in reasons
    assert "s-tiny（b）：空闲 IP 只有 11 个" in reasons
    assert "s-ok" not in reasons
