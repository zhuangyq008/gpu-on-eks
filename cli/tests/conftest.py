"""测试夹具：FakeGateway 默认模拟 ap-southeast-5（2026-11 实测的机型分布）。"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

import pytest

from qwenctl.az_select import Zone
from qwenctl.console import Console
from qwenctl.gateway import CapacityReservation, Certificate, EndpointServices, GatewayError, VpcDns
from qwenctl.network import RouteTable, Subnet, Vpc

MY_ZONES = (
    Zone("apse5-az1", "ap-southeast-5a"),
    Zone("apse5-az2", "ap-southeast-5b"),
    Zone("apse5-az3", "ap-southeast-5c"),
)
CPU_TYPES = frozenset({"m7g.large", "m6g.large", "m8g.large"})
MY_OFFERINGS: Dict[str, FrozenSet[str]] = {
    "apse5-az1": frozenset({"g6.12xlarge"}) | CPU_TYPES,
    "apse5-az2": frozenset({"g7e.2xlarge", "g7e.4xlarge", "g6.12xlarge"}) | CPU_TYPES,
    "apse5-az3": CPU_TYPES,
}
ALL_ZONE_IDS = frozenset(z.zone_id for z in MY_ZONES)
SERVICE_ZONES: Dict[str, FrozenSet[str]] = {
    s: ALL_ZONE_IDS for s in ("ecr.api", "ecr.dkr", "sts", "eks-auth", "sqs", "bedrock-runtime")
}

VPC = Vpc("vpc-1", "10.0.0.0/16", "factory", False, ("100.64.0.0/16",))
SUBNETS = (
    Subnet("subnet-pub-a", "apse5-az1", "ap-southeast-5a", "10.0.0.0/24", 250),
    Subnet("subnet-pub-b", "apse5-az2", "ap-southeast-5b", "10.0.1.0/24", 250),
    Subnet("subnet-priv-a", "apse5-az1", "ap-southeast-5a", "10.0.32.0/19", 8000),
    Subnet("subnet-priv-b", "apse5-az2", "ap-southeast-5b", "10.0.64.0/19", 8000),
    Subnet("subnet-priv-c", "apse5-az3", "ap-southeast-5c", "10.0.96.0/19", 8000),
)
ROUTE_TABLES = (
    RouteTable("rtb-pub", ("subnet-pub-a", "subnet-pub-b"), False, "igw-1"),
    RouteTable("rtb-priv-a", ("subnet-priv-a",), False, "nat-1"),
    RouteTable("rtb-priv-b", ("subnet-priv-b", "subnet-priv-c"), False, "nat-2"),
    RouteTable("rtb-main", (), True, None),
)
DNS_ON = VpcDns(support=True, hostnames=True)
DEFAULT_ENDPOINTS = EndpointServices(
    interface=frozenset({"sts"}), s3_gateway_route_table_ids=frozenset({"rtb-priv-a"})
)


@dataclass
class FakeGateway:
    region_name: str = "ap-southeast-5"
    account: str = "123456789012"
    caller_arn: str = "arn:aws:sts::123456789012:assumed-role/Admin/alice"
    resolved_role: Optional[str] = "arn:aws:iam::123456789012:role/Admin"
    zone_list: Tuple[Zone, ...] = MY_ZONES
    offering_map: Dict[str, FrozenSet[str]] = field(default_factory=lambda: dict(MY_OFFERINGS))
    quotas: Dict[str, Optional[float]] = field(
        default_factory=lambda: {"L-DB2E81BA": 768.0, "L-1216C47A": 1152.0}
    )
    running: Dict[str, float] = field(default_factory=lambda: {"gpu": 0.0, "standard": 0.0})
    running_error: bool = False
    price_map: Dict[str, Optional[float]] = field(
        default_factory=lambda: {"g7e.2xlarge": 5.76163, "g6.12xlarge": 5.7966}
    )
    grafana: bool = False
    idc: Optional[int] = 0
    bedrock: Optional[bool] = False
    stacks: Dict[str, str] = field(default_factory=dict)
    stack_error: bool = False
    reservations: Tuple[CapacityReservation, ...] = ()
    denied: Tuple[str, ...] = ()
    simulate_error: bool = False
    vpc_list: Tuple[Vpc, ...] = (VPC,)
    dns: VpcDns = DNS_ON
    subnet_list: Tuple[Subnet, ...] = SUBNETS
    route_list: Tuple[RouteTable, ...] = ROUTE_TABLES
    endpoints: EndpointServices = DEFAULT_ENDPOINTS
    service_zones: Optional[Dict[str, FrozenSet[str]]] = field(default_factory=lambda: dict(SERVICE_ZONES))
    certs: Tuple[Certificate, ...] = ()
    vpc_error: bool = False

    def region(self) -> str:
        return self.region_name

    def caller_identity(self) -> Tuple[str, str]:
        return self.account, self.caller_arn

    def role_arn(self, role_name: str) -> Optional[str]:
        return self.resolved_role

    def zones(self) -> Sequence[Zone]:
        return self.zone_list

    def offerings(self, instance_types: Sequence[str]) -> Dict[str, FrozenSet[str]]:
        wanted = frozenset(instance_types)
        return {z: types & wanted for z, types in self.offering_map.items() if types & wanted}

    def quota(self, service_code: str, quota_code: str) -> Optional[float]:
        return self.quotas.get(quota_code)

    def running_vcpus(self, family: str) -> float:
        if self.running_error:
            raise GatewayError("无法统计正在运行的实例")
        return self.running[family]

    def prices(self, instance_types: Sequence[str]) -> Dict[str, Optional[float]]:
        return {t: self.price_map.get(t) for t in instance_types}

    def service_available(self, service: str) -> bool:
        return self.grafana

    def idc_instance_count(self) -> Optional[int]:
        return self.idc

    def bedrock_model_available(self, model_id: str) -> Optional[bool]:
        return self.bedrock

    def stack_status(self, stack_name: str) -> Optional[str]:
        if self.stack_error:
            raise GatewayError("无法查询 CloudFormation 栈")
        return self.stacks.get(stack_name)

    def capacity_reservations(self, instance_types: Sequence[str]) -> Sequence[CapacityReservation]:
        return self.reservations

    def denied_actions(self, principal_arn: str, actions: Sequence[str]) -> Sequence[str]:
        if self.simulate_error:
            raise GatewayError("无法预检权限")
        return self.denied

    def vpcs(self) -> Sequence[Vpc]:
        if self.vpc_error:
            raise GatewayError("无法列出 VPC")
        return self.vpc_list

    def vpc_dns(self, vpc_id: str) -> VpcDns:
        return self.dns

    def subnets(self, vpc_id: str) -> Sequence[Subnet]:
        return self.subnet_list

    def route_tables(self, vpc_id: str) -> Sequence[RouteTable]:
        return self.route_list

    def endpoint_services(self, vpc_id: str) -> EndpointServices:
        return self.endpoints

    def endpoint_service_zones(self, services: Sequence[str]) -> Optional[Dict[str, FrozenSet[str]]]:
        return self.service_zones

    def issued_certificates(self) -> Sequence[Certificate]:
        return self.certs


class ScriptedInput:
    """按顺序返回预设的输入；用完后抛 EOFError，避免测试死循环。"""

    def __init__(self, answers: Sequence[str]) -> None:
        self._answers: List[str] = list(answers)
        self.prompts: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self._answers:
            raise EOFError
        return self._answers.pop(0)


def make_console(answers: Sequence[str] = (), assume_defaults: bool = False) -> Tuple[Console, io.StringIO]:
    out = io.StringIO()
    return Console(input_fn=ScriptedInput(answers), out=out, assume_defaults=assume_defaults), out


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def console_factory() -> Callable[..., Tuple[Console, io.StringIO]]:
    return make_console


@pytest.fixture(autouse=True)
def _forbid_real_install(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: object) -> None:
        raise AssertionError("测试不允许执行真实的部署（请传入 install=...）")

    monkeypatch.setattr("qwenctl.cli._default_install", boom)


class RecordingInstall:
    """记录 qwenctl 交给安装流程的请求。"""

    def __init__(self, error: Optional[Exception] = None) -> None:
        self.requests: List[object] = []
        self._error = error

    def __call__(self, request: object) -> None:
        self.requests.append(request)
        if self._error:
            raise self._error
