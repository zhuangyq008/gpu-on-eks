"""VPC 终端节点计划：建哪些、跳过哪些、为什么（CDK 只按这个清单创建）。

现有 VPC 默认不建接口终端节点：私有 DNS 对整个 VPC 生效，会改变客户其他业务访问 STS/ECR 的路径，
还可能让基于 aws:SourceIp 的 IAM 策略失效。S3 网关终端节点只影响所选子网的路由表，默认创建。
"""

from __future__ import annotations

from typing import AbstractSet, Mapping, Optional, Sequence, Tuple

from .models import EndpointPlan, FeatureChoice, NetworkChoice

BASE_INTERFACE_ENDPOINTS: Tuple[str, ...] = ("ecr.api", "ecr.dkr", "sts", "eks-auth")


def wanted_interface_endpoints(features: FeatureChoice) -> Tuple[str, ...]:
    extra = (("sqs",) if features.async_channel else ()) + (
        ("bedrock-runtime",) if features.bedrock_overflow else ()
    )
    return BASE_INTERFACE_ENDPOINTS + extra


def plan_endpoints(
    setting: str,
    network: NetworkChoice,
    features: FeatureChoice,
    zone_ids: Sequence[str],
    service_zones: Optional[Mapping[str, AbstractSet[str]]],
) -> EndpointPlan:
    """setting 是高级配置 network.interface_endpoints（auto/on/off）。

    service_zones 为 None 时表示无法查询服务可用性，按「都可用」处理。
    """
    s3_gateway, s3_tables = _s3_plan(network)
    wanted = wanted_interface_endpoints(features)
    enabled = setting == "on" or (setting == "auto" and network.mode == "new")
    if not enabled:
        reason = (
            "现有 VPC 默认不创建（会改变整个 VPC 的 DNS 解析）" if setting == "auto" else "高级配置已关闭"
        )
        return EndpointPlan((), tuple((s, reason) for s in wanted), s3_gateway, s3_tables)
    create, skipped = [], []
    for service in wanted:
        reason = _skip_reason(service, network, zone_ids, service_zones)
        if reason:
            skipped.append((service, reason))
        else:
            create.append(service)
    return EndpointPlan(tuple(create), tuple(skipped), s3_gateway, s3_tables)


def _skip_reason(
    service: str,
    network: NetworkChoice,
    zone_ids: Sequence[str],
    service_zones: Optional[Mapping[str, AbstractSet[str]]],
) -> Optional[str]:
    if service in network.existing_interface_endpoints:
        return "VPC 里已经有了"
    if service_zones is None:
        return None
    available = service_zones.get(service, frozenset())
    missing = [z for z in zone_ids if z not in available]
    if missing:
        return f"在 {', '.join(missing)} 不提供，流量走 NAT"
    return None


def _s3_plan(network: NetworkChoice) -> Tuple[bool, Tuple[str, ...]]:
    if network.mode == "new":
        return True, ()
    covered = frozenset(network.s3_covered_route_table_ids)
    ours = tuple(dict.fromkeys(s.route_table_id for s in network.private_subnets))
    uncovered = tuple(rt for rt in ours if rt not in covered)
    return bool(uncovered), uncovered
