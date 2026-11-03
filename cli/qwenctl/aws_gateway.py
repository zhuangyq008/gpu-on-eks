"""AwsGateway 的 boto3 实现。只做只读调用（Describe/List/Get/Simulate），不创建也不修改任何资源。"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
)

from .az_select import Zone
from .catalog import is_gpu_family, is_standard_family
from .gateway import CapacityReservation, Certificate, EndpointServices, GatewayError, VpcDns
from .network import RouteTable, Subnet, Vpc

_CONFIG = Config(retries={"max_attempts": 5, "mode": "standard"}, connect_timeout=5, read_timeout=20)
_PRICING_REGION = "us-east-1"
_FAMILY_FILTERS: Dict[str, Callable[[str], bool]] = {
    "gpu": is_gpu_family,
    "standard": is_standard_family,
}
_DEFAULT_ROUTE_TARGET_KEYS = (
    "GatewayId",
    "NatGatewayId",
    "TransitGatewayId",
    "NetworkInterfaceId",
    "InstanceId",
    "CoreNetworkArn",
    "VpcPeeringConnectionId",
)
_INACTIVE_ENDPOINT_STATES = frozenset({"deleted", "deleting", "failed", "rejected", "expired"})


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "ClientError")
    return type(exc).__name__


class Boto3Gateway:
    def __init__(self, region: Optional[str] = None, session: Optional[boto3.session.Session] = None):
        # boto3 只读 AWS_DEFAULT_REGION；CloudShell 两个都会设置，这里再兜底 AWS_REGION
        self._session = session or boto3.session.Session(region_name=region or os.environ.get("AWS_REGION"))
        if not self._session.region_name:
            raise GatewayError("无法确定 region。CloudShell 会自动设置 AWS_REGION；本地运行请加 --region")
        self._clients: Dict[Tuple[str, str], Any] = {}

    def _client(self, name: str, region: Optional[str] = None) -> Any:
        key = (name, region or self._session.region_name)
        if key not in self._clients:
            self._clients[key] = self._session.client(name, region_name=key[1], config=_CONFIG)
        return self._clients[key]

    def _call(self, message: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except NoCredentialsError as exc:
            raise GatewayError("没有可用的 AWS 凭证") from exc
        except (ClientError, BotoCoreError) as exc:
            raise GatewayError(f"{message}（{_error_code(exc)}）") from exc

    def _paginate(self, service: str, operation: str, key: str, **kwargs: Any) -> List[Any]:
        paginator = self._client(service).get_paginator(operation)
        return [item for page in paginator.paginate(**kwargs) for item in page.get(key, [])]

    # ---- 身份与区域 ----
    def region(self) -> str:
        return self._session.region_name

    def caller_identity(self) -> Tuple[str, str]:
        resp = self._call("无法获取当前身份", lambda: self._client("sts").get_caller_identity())
        return resp["Account"], resp["Arn"]

    def role_arn(self, role_name: str) -> Optional[str]:
        try:
            return self._client("iam").get_role(RoleName=role_name)["Role"]["Arn"]
        except (ClientError, BotoCoreError):
            return None

    # ---- AZ、机型、配额、价格 ----
    def zones(self) -> Sequence[Zone]:
        resp = self._call(
            "无法列出可用区",
            lambda: self._client("ec2").describe_availability_zones(
                Filters=[{"Name": "zone-type", "Values": ["availability-zone"]}]
            ),
        )
        return tuple(
            Zone(zone_id=z["ZoneId"], zone_name=z["ZoneName"])
            for z in resp["AvailabilityZones"]
            if z.get("State") == "available"
        )

    def offerings(self, instance_types: Sequence[str]) -> Dict[str, FrozenSet[str]]:
        items = self._call(
            "无法查询机型供应情况",
            lambda: self._paginate(
                "ec2",
                "describe_instance_type_offerings",
                "InstanceTypeOfferings",
                LocationType="availability-zone-id",
                Filters=[{"Name": "instance-type", "Values": list(instance_types)}],
            ),
        )
        by_zone: Dict[str, Set[str]] = {}
        for item in items:
            by_zone.setdefault(item["Location"], set()).add(item["InstanceType"])
        return {zone: frozenset(types) for zone, types in by_zone.items()}

    def quota(self, service_code: str, quota_code: str) -> Optional[float]:
        client = self._client("service-quotas")
        try:
            return float(
                client.get_service_quota(ServiceCode=service_code, QuotaCode=quota_code)["Quota"]["Value"]
            )
        except ClientError as exc:
            if _error_code(exc) != "NoSuchResourceException":
                return None
        except BotoCoreError:
            return None
        try:
            resp = client.get_aws_default_service_quota(ServiceCode=service_code, QuotaCode=quota_code)
            return float(resp["Quota"]["Value"])
        except (ClientError, BotoCoreError):
            return None

    def running_vcpus(self, family: str) -> float:
        """只统计按需实例：Spot 有单独的配额。"""
        matches = _FAMILY_FILTERS[family]
        reservations = self._call(
            "无法统计正在运行的实例",
            lambda: self._paginate(
                "ec2",
                "describe_instances",
                "Reservations",
                Filters=[{"Name": "instance-state-name", "Values": ["pending", "running"]}],
            ),
        )
        counts: Dict[str, int] = {}
        for reservation in reservations:
            for instance in reservation.get("Instances", []):
                itype = instance["InstanceType"]
                if matches(itype) and instance.get("InstanceLifecycle") != "spot":
                    counts = {**counts, itype: counts.get(itype, 0) + 1}
        vcpus = self._vcpus_of(sorted(counts))
        return float(sum(vcpus[t] * n for t, n in counts.items()))

    def _vcpus_of(self, instance_types: Sequence[str]) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for start in range(0, len(instance_types), 100):
            batch = list(instance_types[start : start + 100])
            resp = self._call(
                "无法查询机型规格",
                lambda batch=batch: self._client("ec2").describe_instance_types(InstanceTypes=batch),
            )
            for item in resp["InstanceTypes"]:
                result = {**result, item["InstanceType"]: item["VCpuInfo"]["DefaultVCpus"]}
        return result

    def prices(self, instance_types: Sequence[str]) -> Dict[str, Optional[float]]:
        return {t: self._price(t) for t in instance_types}

    def _price(self, instance_type: str) -> Optional[float]:
        filters = [
            ("regionCode", self.region()),
            ("instanceType", instance_type),
            ("operatingSystem", "Linux"),
            ("tenancy", "Shared"),
            ("preInstalledSw", "NA"),
            ("capacitystatus", "Used"),
        ]
        try:
            resp = self._client("pricing", _PRICING_REGION).get_products(
                ServiceCode="AmazonEC2",
                Filters=[{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters],
                MaxResults=1,
            )
        except (ClientError, BotoCoreError):
            return None
        return _parse_on_demand_price(resp.get("PriceList", []))

    # ---- 服务可用性（None = 无法判断）----
    def service_available(self, service: str) -> bool:
        probes: Dict[str, Callable[[], Any]] = {
            "grafana": lambda: self._client("grafana").list_workspaces(maxResults=1),
        }
        try:
            probes[service]()
            return True
        except EndpointConnectionError:
            return False
        except ClientError:
            return True  # 服务存在，只是没有权限
        except BotoCoreError:
            return False

    def idc_instance_count(self) -> Optional[int]:
        try:
            return len(self._client("sso-admin").list_instances().get("Instances", []))
        except (ClientError, BotoCoreError):
            return None

    def bedrock_model_available(self, model_id: str) -> Optional[bool]:
        try:
            models = self._client("bedrock").list_foundation_models().get("modelSummaries", [])
        except EndpointConnectionError:
            return False  # 本 region 没有 Bedrock
        except (ClientError, BotoCoreError):
            return None
        return any(m["modelId"] == model_id or m["modelId"].startswith(model_id + ":") for m in models)

    def stack_status(self, stack_name: str) -> Optional[str]:
        try:
            stacks = self._client("cloudformation").describe_stacks(StackName=stack_name)["Stacks"]
        except ClientError as exc:
            if _error_code(exc) == "ValidationError" and "does not exist" in str(exc):
                return None
            raise GatewayError(f"无法查询 CloudFormation 栈 {stack_name}（{_error_code(exc)}）") from exc
        except BotoCoreError as exc:
            raise GatewayError(f"无法查询 CloudFormation 栈 {stack_name}（{_error_code(exc)}）") from exc
        live = [s["StackStatus"] for s in stacks if s["StackStatus"] != "DELETE_COMPLETE"]
        return live[0] if live else None

    def capacity_reservations(self, instance_types: Sequence[str]) -> Sequence[CapacityReservation]:
        try:
            items = self._paginate(
                "ec2",
                "describe_capacity_reservations",
                "CapacityReservations",
                Filters=[
                    {"Name": "instance-type", "Values": list(instance_types)},
                    {"Name": "state", "Values": ["active"]},
                ],
            )
        except (ClientError, BotoCoreError):
            return ()
        return tuple(
            CapacityReservation(
                reservation_id=r["CapacityReservationId"],
                instance_type=r["InstanceType"],
                zone_id=r.get("AvailabilityZoneId", ""),
                available_count=r.get("AvailableInstanceCount", 0),
                open_matching=r.get("InstanceMatchCriteria", "open") == "open",
            )
            for r in items
        )

    def denied_actions(self, principal_arn: str, actions: Sequence[str]) -> Sequence[str]:
        results = self._call(
            "无法预检权限",
            lambda: self._paginate(
                "iam",
                "simulate_principal_policy",
                "EvaluationResults",
                PolicySourceArn=principal_arn,
                ActionNames=list(actions),
            ),
        )
        return tuple(r["EvalActionName"] for r in results if r.get("EvalDecision") != "allowed")

    # ---- 现有 VPC ----
    def vpcs(self) -> Sequence[Vpc]:
        items = self._call(
            "无法列出 VPC",
            lambda: self._paginate(
                "ec2", "describe_vpcs", "Vpcs", Filters=[{"Name": "state", "Values": ["available"]}]
            ),
        )
        return tuple(_vpc(v) for v in items)

    def vpc_dns(self, vpc_id: str) -> VpcDns:
        ec2 = self._client("ec2")

        def attribute(name: str, key: str) -> bool:
            resp = self._call(
                "无法读取 VPC 的 DNS 设置",
                lambda: ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute=name),
            )
            return bool(resp.get(key, {}).get("Value"))

        return VpcDns(
            support=attribute("enableDnsSupport", "EnableDnsSupport"),
            hostnames=attribute("enableDnsHostnames", "EnableDnsHostnames"),
        )

    def subnets(self, vpc_id: str) -> Sequence[Subnet]:
        items = self._call(
            "无法列出子网",
            lambda: self._paginate(
                "ec2", "describe_subnets", "Subnets", Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            ),
        )
        return tuple(
            Subnet(
                subnet_id=s["SubnetId"],
                zone_id=s["AvailabilityZoneId"],
                zone_name=s["AvailabilityZone"],
                cidr=s["CidrBlock"],
                available_ips=s["AvailableIpAddressCount"],
            )
            for s in items
            if s.get("CidrBlock")  # 跳过纯 IPv6 子网
        )

    def route_tables(self, vpc_id: str) -> Sequence[RouteTable]:
        items = self._call(
            "无法列出路由表",
            lambda: self._paginate(
                "ec2",
                "describe_route_tables",
                "RouteTables",
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}],
            ),
        )
        return tuple(_route_table(rt) for rt in items)

    def endpoint_services(self, vpc_id: str) -> EndpointServices:
        items = self._call(
            "无法列出 VPC 终端节点",
            lambda: self._paginate(
                "ec2",
                "describe_vpc_endpoints",
                "VpcEndpoints",
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}],
            ),
        )
        prefix = f"com.amazonaws.{self.region()}."
        active = [
            e
            for e in items
            if e["ServiceName"].startswith(prefix)
            and str(e.get("State", "available")).lower() not in _INACTIVE_ENDPOINT_STATES
        ]
        return EndpointServices(
            interface=frozenset(
                e["ServiceName"][len(prefix) :] for e in active if e.get("VpcEndpointType") == "Interface"
            ),
            s3_gateway_route_table_ids=frozenset(
                rt
                for e in active
                if e.get("VpcEndpointType") == "Gateway" and e["ServiceName"] == prefix + "s3"
                for rt in e.get("RouteTableIds", [])
            ),
        )

    def endpoint_service_zones(self, services: Sequence[str]) -> Optional[Dict[str, FrozenSet[str]]]:
        prefix = f"com.amazonaws.{self.region()}."
        try:
            details = self._paginate(
                "ec2",
                "describe_vpc_endpoint_services",
                "ServiceDetails",
                Filters=[{"Name": "service-name", "Values": [prefix + s for s in services]}],
            )
            name_to_id = {z.zone_name: z.zone_id for z in self.zones()}
        except (ClientError, BotoCoreError, GatewayError):
            return None
        result: Dict[str, FrozenSet[str]] = {}
        for d in details:
            if not any(t.get("ServiceType") == "Interface" for t in d.get("ServiceType", [])):
                continue
            zone_ids = frozenset(name_to_id[n] for n in d.get("AvailabilityZones", []) if n in name_to_id)
            result = {**result, d["ServiceName"][len(prefix) :]: zone_ids}
        return result

    def issued_certificates(self) -> Sequence[Certificate]:
        items = self._call(
            "无法列出 ACM 证书",
            lambda: self._paginate(
                "acm", "list_certificates", "CertificateSummaryList", CertificateStatuses=["ISSUED"]
            ),
        )
        return tuple(Certificate(arn=c["CertificateArn"], domain=c["DomainName"]) for c in items)


def _name_tag(tags: Iterable[Dict[str, str]]) -> str:
    return next((t["Value"] for t in tags if t.get("Key") == "Name"), "")


def _vpc(v: Dict[str, Any]) -> Vpc:
    secondary = tuple(
        a["CidrBlock"]
        for a in v.get("CidrBlockAssociationSet", [])
        if a.get("CidrBlock") != v["CidrBlock"] and a.get("CidrBlockState", {}).get("State") == "associated"
    )
    return Vpc(
        vpc_id=v["VpcId"],
        cidr=v["CidrBlock"],
        name=_name_tag(v.get("Tags", [])),
        is_default=bool(v.get("IsDefault")),
        secondary_cidrs=secondary,
    )


def _route_table(rt: Dict[str, Any]) -> RouteTable:
    associations = rt.get("Associations", [])
    default = next(
        (
            r
            for r in rt.get("Routes", [])
            if r.get("DestinationCidrBlock") == "0.0.0.0/0" and r.get("State") == "active"
        ),
        None,
    )
    target = None
    if default:
        target = next((default[k] for k in _DEFAULT_ROUTE_TARGET_KEYS if default.get(k)), None)
    return RouteTable(
        route_table_id=rt["RouteTableId"],
        subnet_ids=tuple(a["SubnetId"] for a in associations if a.get("SubnetId")),
        is_main=any(a.get("Main") for a in associations),
        default_route_target=target,
    )


def _parse_on_demand_price(price_list: Sequence[str]) -> Optional[float]:
    if not price_list:
        return None
    try:
        product = json.loads(price_list[0])
        terms = product["terms"]["OnDemand"].values()
        dims = [d for term in terms for d in term["priceDimensions"].values()]
        return float(dims[0]["pricePerUnit"]["USD"])
    except (ValueError, KeyError, IndexError):
        return None
