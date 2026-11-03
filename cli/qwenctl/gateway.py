"""AWS 访问接口。preflight 和交互逻辑只依赖这个接口，测试时注入假实现。

约定：返回 Optional 的方法，None 表示「无法判断」（通常是缺少权限），调用方要区分「没有」和「不知道」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Protocol, Sequence, Tuple

from .az_select import Zone
from .network import RouteTable, Subnet, Vpc


class GatewayError(Exception):
    """AWS 调用失败（权限不足、服务不可用等）。message 已经是给用户看的中文。"""


@dataclass(frozen=True)
class Certificate:
    arn: str
    domain: str


@dataclass(frozen=True)
class EndpointServices:
    """VPC 里已有的终端节点（短服务名，例如 "sts"、"s3"）。"""

    interface: FrozenSet[str] = frozenset()
    s3_gateway_route_table_ids: FrozenSet[str] = frozenset()  # 已经挂了 S3 网关终端节点的路由表


@dataclass(frozen=True)
class VpcDns:
    support: bool
    hostnames: bool


@dataclass(frozen=True)
class CapacityReservation:
    reservation_id: str
    instance_type: str
    zone_id: str
    available_count: int
    open_matching: bool  # open 类型可以被匹配的实例自动使用；targeted 需要按 ID 指定


class AwsGateway(Protocol):
    def region(self) -> str: ...

    def caller_identity(self) -> Tuple[str, str]:
        """返回 (account, caller_arn)。"""

    def role_arn(self, role_name: str) -> Optional[str]: ...

    def zones(self) -> Sequence[Zone]: ...

    def offerings(self, instance_types: Sequence[str]) -> Dict[str, FrozenSet[str]]:
        """zone_id -> 有货的机型。"""

    def quota(self, service_code: str, quota_code: str) -> Optional[float]: ...

    def running_vcpus(self, family: str) -> float:
        """family = "gpu" | "standard"：账号里正在运行的这类按需实例占用的 vCPU。"""

    def prices(self, instance_types: Sequence[str]) -> Dict[str, Optional[float]]: ...

    def service_available(self, service: str) -> bool: ...

    def idc_instance_count(self) -> Optional[int]: ...

    def bedrock_model_available(self, model_id: str) -> Optional[bool]: ...

    def stack_status(self, stack_name: str) -> Optional[str]:
        """栈不存在时返回 None。"""

    def capacity_reservations(self, instance_types: Sequence[str]) -> Sequence[CapacityReservation]: ...

    def denied_actions(self, principal_arn: str, actions: Sequence[str]) -> Sequence[str]: ...

    def vpcs(self) -> Sequence[Vpc]: ...

    def vpc_dns(self, vpc_id: str) -> VpcDns: ...

    def subnets(self, vpc_id: str) -> Sequence[Subnet]: ...

    def route_tables(self, vpc_id: str) -> Sequence[RouteTable]: ...

    def endpoint_services(self, vpc_id: str) -> EndpointServices: ...

    def endpoint_service_zones(self, services: Sequence[str]) -> Optional[Dict[str, FrozenSet[str]]]:
        """服务短名 -> 提供该接口终端节点的 zone_id。None = 无法判断。"""

    def issued_certificates(self) -> Sequence[Certificate]: ...
