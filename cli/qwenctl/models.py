"""交互回答和最终部署配置（交给 CDK 的契约，见 infra/lib/config.ts）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from .az_select import AzSelection, ProfilePlacement

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SubnetRef:
    subnet_id: str
    zone_id: str
    cidr: str = ""
    route_table_id: str = ""


@dataclass(frozen=True)
class NetworkChoice:
    mode: str  # "new" | "existing"
    cidr: Optional[str] = None  # 新建时是要创建的 CIDR；现有 VPC 时是它的主 CIDR
    secondary_cidrs: Tuple[str, ...] = ()
    vpc_id: Optional[str] = None
    private_subnets: Tuple[SubnetRef, ...] = ()  # 与 zones 顺序一致
    public_subnets: Tuple[SubnetRef, ...] = ()
    existing_interface_endpoints: Tuple[str, ...] = ()
    s3_covered_route_table_ids: Tuple[str, ...] = ()  # 已经挂了 S3 网关终端节点的路由表


@dataclass(frozen=True)
class AccessChoice:
    mode: str  # "internal" | "public"
    certificate_arn: Optional[str] = None
    allowed_cidrs: Tuple[str, ...] = ()
    open_to_internet: bool = False  # 公网且不限来源 IP 时，客户明确确认过


@dataclass(frozen=True)
class CapacityChoice:
    min_replicas: int
    max_replicas: int


@dataclass(frozen=True)
class FeatureChoice:
    async_channel: bool
    bedrock_overflow: bool
    alert_email: Optional[str] = None


@dataclass(frozen=True)
class Answers:
    network: NetworkChoice
    access: AccessChoice
    capacity: CapacityChoice
    features: FeatureChoice


@dataclass(frozen=True)
class EndpointPlan:
    interface: Tuple[str, ...]
    skipped: Tuple[Tuple[str, str], ...]  # (服务, 原因)
    s3_gateway: bool
    s3_route_table_ids: Tuple[str, ...]  # 空 = 新建 VPC 的全部私有路由表


@dataclass(frozen=True)
class DeploymentConfig:
    name: str
    account: str
    region: str
    admin_principal_arn: str
    selection: AzSelection
    system_instance_type: str
    observability_mode: str  # "cloudwatch" | "grafana"
    answers: Answers
    endpoints: EndpointPlan
    advanced: Mapping[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        a = self.answers
        return {
            "schemaVersion": SCHEMA_VERSION,
            "name": self.name,
            "account": self.account,
            "region": self.region,
            "adminPrincipalArn": self.admin_principal_arn,
            "zones": [{"zoneId": z.zone_id, "zoneName": z.zone_name} for z in self.selection.zones],
            "network": _network_dict(a.network, self.endpoints),
            "access": {
                "mode": a.access.mode,
                "certificateArn": a.access.certificate_arn,
                "allowedCidrs": list(a.access.allowed_cidrs),
                "openToInternet": a.access.open_to_internet,
            },
            "gpu": {
                "minReplicas": a.capacity.min_replicas,
                "maxReplicas": a.capacity.max_replicas,
                "profiles": [_placement_dict(p) for p in self.selection.placements],
            },
            "systemNodes": {"instanceType": self.system_instance_type},
            "features": {
                "asyncChannel": a.features.async_channel,
                "bedrockOverflow": a.features.bedrock_overflow,
                "alertEmail": a.features.alert_email,
            },
            "observability": {"mode": self.observability_mode},
            "advanced": dict(self.advanced),
        }


def _network_dict(network: NetworkChoice, endpoints: EndpointPlan) -> Dict[str, Any]:
    return {
        "mode": network.mode,
        "cidr": network.cidr,
        "secondaryCidrs": list(network.secondary_cidrs),
        "vpcId": network.vpc_id,
        "privateSubnets": [
            {"subnetId": s.subnet_id, "zoneId": s.zone_id, "cidr": s.cidr, "routeTableId": s.route_table_id}
            for s in network.private_subnets
        ],
        "publicSubnets": [{"subnetId": s.subnet_id, "zoneId": s.zone_id} for s in network.public_subnets],
        "endpoints": {
            "interface": list(endpoints.interface),
            "s3Gateway": endpoints.s3_gateway,
            "s3RouteTableIds": list(endpoints.s3_route_table_ids),
        },
    }


def _placement_dict(placement: ProfilePlacement) -> Dict[str, Any]:
    p = placement.profile
    return {
        "name": p.name,
        "instanceTypes": list(p.instance_types),
        "tensorParallel": p.tensor_parallel,
        "replicasPerNode": p.replicas_per_node,
        "nodepoolWeight": p.nodepool_weight,
        "experimental": p.experimental,
        "zoneIds": list(placement.zone_ids),
    }
