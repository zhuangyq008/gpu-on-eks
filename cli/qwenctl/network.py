"""现有 VPC：判断子网是公有、私有还是隔离，并在每个 AZ 里挑一个子网。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AbstractSet, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

MIN_FREE_IPS = 128  # 空闲 IP 少于这个数的子网不考虑（每个节点都会占用不少 Pod IP）

PUBLIC = "public"
PRIVATE = "private"
ISOLATED = "isolated"


@dataclass(frozen=True)
class Vpc:
    vpc_id: str
    cidr: str
    name: str
    is_default: bool
    secondary_cidrs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Subnet:
    subnet_id: str
    zone_id: str
    zone_name: str
    cidr: str
    available_ips: int


@dataclass(frozen=True)
class RouteTable:
    route_table_id: str
    subnet_ids: Tuple[str, ...]
    is_main: bool
    default_route_target: Optional[str]  # 0.0.0.0/0 指向哪里：igw-/nat-/tgw-/...；没有则为 None


@dataclass(frozen=True)
class ClassifiedSubnet:
    subnet: Subnet
    kind: str
    route_table_id: Optional[str]


def route_kind(target: Optional[str]) -> str:
    if target is None:
        return ISOLATED
    if target.startswith("igw-"):
        return PUBLIC
    # NAT 网关、TGW、防火墙终端节点（vpce-）、NAT 实例（eni-/i-）、Cloud WAN 都算有出口的私有子网
    return PRIVATE


def classify_subnets(
    subnets: Iterable[Subnet], route_tables: Sequence[RouteTable]
) -> Tuple[ClassifiedSubnet, ...]:
    explicit: Dict[str, RouteTable] = {sid: rt for rt in route_tables for sid in rt.subnet_ids}
    main = next((rt for rt in route_tables if rt.is_main), None)
    result = []
    for subnet in subnets:
        table = explicit.get(subnet.subnet_id, main)
        target = table.default_route_target if table else None
        result.append(
            ClassifiedSubnet(
                subnet=subnet,
                kind=route_kind(target),
                route_table_id=table.route_table_id if table else None,
            )
        )
    return tuple(result)


def eligible_zone_ids(classified: Iterable[ClassifiedSubnet]) -> FrozenSet[str]:
    """有合格私有子网（有出口、空闲 IP 足够）的 AZ。"""
    return frozenset(c.subnet.zone_id for c in classified if _usable(c, PRIVATE))


def pick_subnets(
    classified: Iterable[ClassifiedSubnet], kind: str, zone_ids: AbstractSet[str]
) -> Dict[str, ClassifiedSubnet]:
    """每个 AZ 选空闲 IP 最多的那个子网。返回 zone_id -> ClassifiedSubnet。"""
    best: Dict[str, ClassifiedSubnet] = {}
    for c in classified:
        if not _usable(c, kind) or c.subnet.zone_id not in zone_ids:
            continue
        current = best.get(c.subnet.zone_id)
        if current is None or c.subnet.available_ips > current.subnet.available_ips:
            best = {**best, c.subnet.zone_id: c}
    return best


def rejection_reasons(classified: Iterable[ClassifiedSubnet]) -> List[str]:
    """解释每个子网为什么不能用，帮客户看懂「这个 VPC 不能用」。"""
    reasons = []
    for c in classified:
        s = c.subnet
        if c.kind == PUBLIC:
            reasons.append(f"{s.subnet_id}（{s.zone_name}）：公有子网，不放节点")
        elif c.kind == ISOLATED:
            reasons.append(f"{s.subnet_id}（{s.zone_name}）：没有 0.0.0.0/0 出口（需要 NAT 或 TGW）")
        elif s.available_ips < MIN_FREE_IPS:
            reasons.append(
                f"{s.subnet_id}（{s.zone_name}）：空闲 IP 只有 {s.available_ips} 个，少于 {MIN_FREE_IPS}"
            )
    return reasons


def _usable(c: ClassifiedSubnet, kind: str) -> bool:
    return c.kind == kind and c.subnet.available_ips >= MIN_FREE_IPS
