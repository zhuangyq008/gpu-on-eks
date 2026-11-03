"""4 个必问问题及其追问（见 docs/03 §3.1）。"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

from .az_select import AzSelection, AzSelectionError, select_azs
from .capacity import estimate_cost, replica_defaults
from .catalog import GPU_QUOTA_CODE, PROFILES
from .console import Aborted, Console
from .gateway import AwsGateway, EndpointServices, GatewayError
from .models import AccessChoice, Answers, CapacityChoice, FeatureChoice, NetworkChoice, SubnetRef
from .network import (
    PRIVATE,
    PUBLIC,
    ClassifiedSubnet,
    Vpc,
    classify_subnets,
    eligible_zone_ids,
    pick_subnets,
    rejection_reasons,
)
from .preflight import PreflightReport, pick_system_instance_type, quota_increase_command

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EXISTING_VPC_REQUIREMENT = (
    "要求：至少 2 个 AZ 各有一个带 NAT/TGW 出口、空闲 IP 足够的私有子网，并且其中有 GPU 机型"
)


@dataclass(frozen=True)
class NetworkResult:
    choice: NetworkChoice
    selection: AzSelection


class VpcRejected(Exception):
    """现有 VPC 不满足要求；message 是给用户看的原因。"""


def ask_all(
    console: Console, report: PreflightReport, gw: AwsGateway, advanced: Mapping[str, Any]
) -> Tuple[Answers, AzSelection]:
    if report.selection is None:
        raise ValueError("环境检查没有选出 AZ，不能继续提问")
    console.heading("[2/3] 4 个问题（直接回车 = 使用默认值）")
    network = ask_network(console, report, report.selection, gw, advanced["network"]["cidr"])
    access = ask_access(console, network, gw)
    capacity = ask_capacity(console, report, network.selection)
    features = ask_features(console, report)
    answers = Answers(network=network.choice, access=access, capacity=capacity, features=features)
    return answers, network.selection


# ---- 1. 网络 ----
def ask_network(
    console: Console,
    report: PreflightReport,
    default_selection: AzSelection,
    gw: AwsGateway,
    default_cidr: str,
) -> NetworkResult:
    options = [("new", f"新建 VPC（{default_cidr}）"), ("existing", "使用现有 VPC")]
    while True:
        mode = console.choose("1. 网络", options, "new")
        if mode == "new":
            return NetworkResult(NetworkChoice(mode="new", cidr=default_cidr), default_selection)
        result = _ask_existing_vpc(console, report, gw)
        if result is not None:
            return result


def _ask_existing_vpc(console: Console, report: PreflightReport, gw: AwsGateway) -> Optional[NetworkResult]:
    try:
        vpcs = gw.vpcs()
    except GatewayError as exc:
        console.status("fail", str(exc))
        return None
    if not vpcs:
        console.status("fail", "当前 region 没有可用的 VPC，请选择新建")
        return None
    by_id = {v.vpc_id: v for v in vpcs}
    vpc_id = console.choose(
        "选择 VPC",
        [(v.vpc_id, f"{v.vpc_id}  {v.cidr}  {v.name}{'（默认 VPC）' if v.is_default else ''}") for v in vpcs],
        vpcs[0].vpc_id,
    )
    try:
        result = inspect_existing_vpc(by_id[vpc_id], report, gw)
    except VpcRejected as exc:
        console.status("fail", f"这个 VPC 不能用：{exc}")
        return None
    for zone, subnet in zip(result.selection.zones, result.choice.private_subnets):
        console.status("ok", f"{zone.label}：私有子网 {subnet.subnet_id}（{subnet.cidr}）")
    return result


def inspect_existing_vpc(vpc: Vpc, report: PreflightReport, gw: AwsGateway) -> NetworkResult:
    """检查现有 VPC 能不能用；不能用时抛 VpcRejected。"""
    try:
        dns = gw.vpc_dns(vpc.vpc_id)
        route_tables = gw.route_tables(vpc.vpc_id)
        classified = classify_subnets(gw.subnets(vpc.vpc_id), route_tables)
        endpoints = gw.endpoint_services(vpc.vpc_id)
    except GatewayError as exc:
        raise VpcRejected(str(exc)) from exc
    if not (dns.support and dns.hostnames):
        raise VpcRejected(
            "没有开启 DNS 解析（enableDnsSupport）和 DNS 主机名（enableDnsHostnames），EKS 节点无法注册"
        )
    try:
        selection = select_azs(report.zones, report.offerings, PROFILES, eligible_zone_ids(classified))
    except AzSelectionError as exc:
        reasons = rejection_reasons(classified)
        detail = "\n      ".join([_EXISTING_VPC_REQUIREMENT, *reasons])
        raise VpcRejected(f"{exc}\n      {detail}") from exc
    if report.upper_bound_for(selection) < 1:
        profile = selection.primary.profile
        command = quota_increase_command(GPU_QUOTA_CODE, profile.vcpus_for(2))
        raise VpcRejected(
            f"这个 VPC 的 AZ 里只能用 {profile.instance_type}，G/VT 配额不够启动 1 个副本。{command}"
        )
    if pick_system_instance_type(selection, report.offerings) is None:
        raise VpcRejected("这个 VPC 的 AZ 里没有可用的系统节点机型")
    return NetworkResult(_existing_choice(vpc, classified, endpoints, selection), selection)


def _existing_choice(
    vpc: Vpc,
    classified: Sequence[ClassifiedSubnet],
    endpoints: EndpointServices,
    selection: AzSelection,
) -> NetworkChoice:
    zone_ids = frozenset(selection.zone_ids)
    private = pick_subnets(classified, PRIVATE, zone_ids)
    public = pick_subnets(classified, PUBLIC, zone_ids)
    return NetworkChoice(
        mode="existing",
        cidr=vpc.cidr,
        secondary_cidrs=vpc.secondary_cidrs,
        vpc_id=vpc.vpc_id,
        private_subnets=tuple(_subnet_ref(private[z]) for z in selection.zone_ids),
        public_subnets=tuple(_subnet_ref(public[z]) for z in selection.zone_ids if z in public),
        existing_interface_endpoints=tuple(sorted(endpoints.interface)),
        s3_covered_route_table_ids=tuple(sorted(endpoints.s3_gateway_route_table_ids)),
    )


def _subnet_ref(c: ClassifiedSubnet) -> SubnetRef:
    return SubnetRef(
        subnet_id=c.subnet.subnet_id,
        zone_id=c.subnet.zone_id,
        cidr=c.subnet.cidr,
        route_table_id=c.route_table_id or "",
    )


# ---- 2. 访问方式 ----
def ask_access(console: Console, network: NetworkResult, gw: AwsGateway) -> AccessChoice:
    options = [("internal", "仅 VPC 内部（internal ALB）"), ("public", "公网 HTTPS（需要 ACM 证书）")]
    while True:
        mode = console.choose("2. API 访问方式", options, "internal")
        if mode == "internal":
            console.status("info", "部署完成后可以用 ./qwenctl test 在集群内发测试请求")
            return AccessChoice(mode="internal")
        result = _ask_public(console, network, gw)
        if result is not None:
            return result


def _ask_public(console: Console, network: NetworkResult, gw: AwsGateway) -> Optional[AccessChoice]:
    choice = network.choice
    if choice.mode == "existing" and len(choice.public_subnets) < len(network.selection.zones):
        console.status("fail", "所选 VPC 在这两个 AZ 里没有齐全的公有子网，无法创建公网 ALB")
        return None
    try:
        certs = gw.issued_certificates()
    except GatewayError as exc:
        console.status("fail", str(exc))
        return None
    if not certs:
        console.status(
            "fail", "当前 region 没有 ISSUED 状态的 ACM 证书。请先申请证书，或者选择仅 VPC 内部访问"
        )
        return None
    cert = console.choose("选择证书", [(c.arn, c.domain) for c in certs], certs[0].arn)
    while True:
        raw = console.ask_text("允许访问的来源 CIDR（多个用逗号分隔）", "", validate_cidrs)
        cidrs = tuple(c.strip() for c in raw.split(",") if c.strip())
        if cidrs and not _is_open(cidrs):
            return AccessChoice(mode="public", certificate_arn=cert, allowed_cidrs=cidrs)
        console.status("warn", "不限制来源 IP：公网上任何地址都能访问（仍然需要 API Key 和 WAF 限速）")
        if not console.interactive:
            raise Aborted("非交互模式下不能对整个公网开放，请填写来源 CIDR")
        if console.ask_bool("确认对整个公网开放？", False):
            return AccessChoice(mode="public", certificate_arn=cert, allowed_cidrs=(), open_to_internet=True)


def _is_open(cidrs: Sequence[str]) -> bool:
    return any(ipaddress.ip_network(c).prefixlen == 0 for c in cidrs)


def validate_cidrs(raw: str) -> Optional[str]:
    for item in (c.strip() for c in raw.split(",") if c.strip()):
        try:
            ipaddress.ip_network(item, strict=True)
        except ValueError:
            try:
                fixed = ipaddress.ip_network(item, strict=False)
            except ValueError:
                return f"{item} 不是合法的 CIDR"
            return f"{item} 的主机位不为 0，应该写成 {fixed}"
    return None


# ---- 3. 副本数 ----
def ask_capacity(console: Console, report: PreflightReport, selection: AzSelection) -> CapacityChoice:
    profile = selection.primary.profile
    upper = report.upper_bound_for(selection)
    if upper < 1:
        raise ValueError(f"G/VT 配额不足以启动 1 个 {profile.instance_type} 副本")
    defaults = replica_defaults(upper)
    console.line(f"  3. 副本数（上限 {upper}，按 {profile.instance_type} 和当前配额计算）")
    min_r = console.ask_int("最少副本数", defaults.min_replicas, 1, upper)
    max_r = console.ask_int("最多副本数", max(defaults.max_replicas, min_r), min_r, upper)
    if min_r < 2:
        console.status("warn", "只有 1 个常驻副本：没有跨 AZ 高可用，扩容期间也没有余量")
    cost = estimate_cost(profile, report.prices.get(profile.instance_type), min_r, max_r)
    if cost.baseline_monthly is not None and cost.peak_hourly is not None:
        console.status(
            "info",
            f"常驻 ≈ ${cost.baseline_monthly:,.0f}/月，满载 ≈ ${cost.peak_hourly:,.2f}/h（GPU 部分，On-Demand）",
        )
    return CapacityChoice(min_replicas=min_r, max_replicas=max_r)


# ---- 4. 可选能力 ----
def ask_features(console: Console, report: PreflightReport) -> FeatureChoice:
    console.line("  4. 可选能力")
    async_on = console.ask_bool("启用异步通道（SQS）？", True)
    overflow = False
    if report.bedrock_overflow_available:
        overflow = console.ask_bool("启用 Bedrock 溢出？（数据会发送给 Bedrock 上的另一个模型）", False)
        if overflow:
            overflow = console.ask_bool(
                "确认：溢出的请求由 Qwen3-VL-235B 处理，输出可能和本地模型不一致。仍然启用？", False
            )
    else:
        console.status("info", "Bedrock 溢出：当前 region 不可用，已跳过")
    email = console.ask_text("告警邮箱（留空 = 不发邮件）", "", validate_email)
    if email:
        console.status("info", "部署后请到邮箱点击 SNS 订阅确认链接")
    return FeatureChoice(async_channel=async_on, bedrock_overflow=overflow, alert_email=email or None)


def validate_email(raw: str) -> Optional[str]:
    if raw and not _EMAIL.match(raw):
        return f"{raw} 不是合法的邮箱地址"
    return None
