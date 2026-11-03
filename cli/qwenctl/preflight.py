"""环境检查（只读）：探测账号、AZ、配额、可选服务，产出 PreflightReport（见 docs/03 §6）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Mapping, Optional, Sequence, Tuple

from .az_select import AzSelection, AzSelectionError, Zone, select_azs
from .capacity import (
    DEFAULT_MAX_REPLICAS,
    DEFAULT_MIN_REPLICAS,
    Quota,
    max_replicas_by_quota,
    replica_upper_bound,
)
from .catalog import (
    BEDROCK_OVERFLOW_MODEL_ID,
    GPU_QUOTA_CODE,
    PROFILES,
    STANDARD_QUOTA_CODE,
    SYSTEM_INSTANCE_CANDIDATES,
    SYSTEM_NODE_COUNT,
    SYSTEM_NODE_VCPUS,
    gpu_instance_types,
)
from .gateway import AwsGateway, GatewayError
from .identity import IdentityError, assumed_role_name, principal_arn

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"

REQUIRED_ACTIONS: Tuple[str, ...] = (
    "cloudformation:CreateStack",
    "iam:CreateRole",
    "iam:PassRole",
    "codebuild:CreateProject",
    "codebuild:StartBuild",
    "s3:CreateBucket",
    "ssm:PutParameter",
    "eks:CreateCluster",
    "ec2:CreateVpc",
)

# 处于这些状态的栈不能直接更新
BROKEN_STACK_STATES = frozenset(
    {"ROLLBACK_COMPLETE", "ROLLBACK_FAILED", "DELETE_FAILED", "UPDATE_ROLLBACK_FAILED", "CREATE_FAILED"}
)

Checks = Tuple["Check", ...]


@dataclass(frozen=True)
class Check:
    status: str
    title: str
    detail: str = ""


@dataclass(frozen=True)
class PreflightReport:
    name: str
    account: str
    region: str
    principal_arn: str
    zones: Tuple[Zone, ...]
    offerings: Mapping[str, FrozenSet[str]]
    selection: Optional[AzSelection]
    gpu_available_vcpus: Optional[float]  # None = 配额读不到
    prices: Mapping[str, Optional[float]]
    system_instance_type: Optional[str]
    observability_mode: str
    bedrock_overflow_available: bool
    cdk_bootstrapped: bool
    existing_deployment: bool
    checks: Checks

    @property
    def passed(self) -> bool:
        return not any(c.status == FAIL for c in self.checks)

    def upper_bound_for(self, selection: AzSelection) -> int:
        return replica_upper_bound(selection.primary.profile, self.gpu_available_vcpus)


def run_preflight(gw: AwsGateway, name: str, advanced: Mapping[str, Any]) -> PreflightReport:
    """GatewayError 只会从身份、AZ、机型查询这几个关键步骤抛出，其余失败都降级为 WARN。"""
    (account, principal), c_identity = _identity(gw)
    zones = tuple(gw.zones())
    offerings = gw.offerings(gpu_instance_types() + SYSTEM_INSTANCE_CANDIDATES)
    selection, c_azs = _select(zones, offerings)
    available, c_gpu = _gpu_quota(gw, selection)
    system_type, c_system = _system_nodes(selection, offerings)
    prices, c_prices = _prices(gw, offerings)
    obs_mode, c_obs = _observability(gw)
    bedrock, c_bedrock = _bedrock(gw)
    cdk, c_cdk = _cdk_bootstrap(gw)
    existing, c_existing = _existing_deployment(gw, name)
    checks = (
        c_identity
        + c_azs
        + c_gpu
        + c_system
        + _standard_quota(gw, advanced)
        + c_prices
        + _odcr(gw, selection)
        + c_obs
        + c_bedrock
        + c_cdk
        + c_existing
        + _permissions(gw, principal)
    )
    return PreflightReport(
        name=name,
        account=account,
        region=gw.region(),
        principal_arn=principal,
        zones=zones,
        offerings=offerings,
        selection=selection,
        gpu_available_vcpus=available,
        prices=prices,
        system_instance_type=system_type,
        observability_mode=obs_mode,
        bedrock_overflow_available=bedrock,
        cdk_bootstrapped=cdk,
        existing_deployment=existing,
        checks=checks,
    )


def pick_system_instance_type(
    selection: AzSelection, offerings: Mapping[str, FrozenSet[str]]
) -> Optional[str]:
    for itype in SYSTEM_INSTANCE_CANDIDATES:
        if all(itype in offerings.get(zid, ()) for zid in selection.zone_ids):
            return itype
    return None


def quota_increase_command(quota_code: str, desired_vcpus: float) -> str:
    return (
        "aws service-quotas request-service-quota-increase --service-code ec2 "
        f"--quota-code {quota_code} --desired-value {desired_vcpus:.0f}"
    )


def _short_principal(arn: str) -> str:
    return arn.split(":", 5)[-1] if arn else "-"


def _identity(gw: AwsGateway) -> Tuple[Tuple[str, str], Checks]:
    account, caller = gw.caller_identity()
    role = assumed_role_name(caller)
    resolved = gw.role_arn(role) if role else None
    try:
        principal = principal_arn(caller, resolved)
    except IdentityError as exc:
        return (account, ""), (Check(FAIL, "当前身份不能用于部署", str(exc)),)
    checks: Checks = (Check(OK, f"账号 {account} / {gw.region()} / {_short_principal(principal)}"),)
    if role and not resolved:
        checks += (
            Check(
                WARN,
                "无法读取角色的完整 ARN（缺少 iam:GetRole）",
                f"EKS 管理员授权将使用 {principal}；如果角色带 path，需要手动确认",
            ),
        )
    return (account, principal), checks


def _select(
    zones: Sequence[Zone], offerings: Mapping[str, FrozenSet[str]]
) -> Tuple[Optional[AzSelection], Checks]:
    try:
        selection = select_azs(zones, offerings, PROFILES)
    except AzSelectionError as exc:
        types = "、".join(p.instance_type for p in PROFILES)
        return None, (Check(FAIL, "GPU 可用区", f"{exc}。需要换一个提供 {types} 的 region"),)
    zone_text = ", ".join(z.label for z in selection.zones)
    detail = "；".join(f"{p.profile.instance_type}: {', '.join(p.zone_ids)}" for p in selection.placements)
    return selection, (Check(OK, f"AZ: {zone_text}", detail),) + tuple(
        Check(WARN, w) for w in selection.warnings
    )


def _gpu_quota(gw: AwsGateway, selection: Optional[AzSelection]) -> Tuple[Optional[float], Checks]:
    """返回可用的 G/VT vCPU（读不到配额时为 None）。"""
    if selection is None:
        return None, ()
    primary = selection.primary.profile
    limit = gw.quota("ec2", GPU_QUOTA_CODE)
    if limit is None:
        return None, (Check(WARN, "无法读取 G/VT 实例配额", f"副本上限暂按 {DEFAULT_MAX_REPLICAS} 计算"),)
    try:
        used: Optional[float] = gw.running_vcpus("gpu")
    except GatewayError:
        used = None
    available = Quota(limit, used or 0.0).available_vcpus
    upper = max_replicas_by_quota(primary, available)
    used_text = "未知" if used is None else f"{used:.0f}"
    title = f"G/VT 配额 {limit:.0f} vCPU，已用 {used_text} → 最多 {upper} 个副本（按 {primary.instance_type} 计算）"
    notes = (
        [] if used is not None else ["无法统计已用的 vCPU（缺少 ec2:DescribeInstances 权限），上限可能偏高"]
    )
    fallbacks = [
        p.profile for p in selection.placements[1:] if p.profile.vcpus_per_node != primary.vcpus_per_node
    ]
    notes += [
        f"回退到 {p.instance_type} 时每个副本占 {p.vcpus_per_node // p.replicas_per_node} vCPU，配额会更快用完"
        for p in fallbacks
    ]
    if upper < 1:
        need = primary.vcpus_for(DEFAULT_MIN_REPLICAS) + (used or 0.0)
        notes.append("请先申请提额：" + quota_increase_command(GPU_QUOTA_CODE, need))
        return available, (Check(FAIL, title, "；".join(notes)),)
    if upper < DEFAULT_MAX_REPLICAS:
        need = primary.vcpus_for(DEFAULT_MAX_REPLICAS) + (used or 0.0)
        notes.append("需要更多副本时可以申请提额：" + quota_increase_command(GPU_QUOTA_CODE, need))
    status = OK if upper >= DEFAULT_MAX_REPLICAS and used is not None else WARN
    return available, (Check(status, title, "；".join(notes)),)


def _system_nodes(
    selection: Optional[AzSelection], offerings: Mapping[str, FrozenSet[str]]
) -> Tuple[Optional[str], Checks]:
    if selection is None:
        return None, ()
    itype = pick_system_instance_type(selection, offerings)
    if itype is None:
        candidates = ", ".join(SYSTEM_INSTANCE_CANDIDATES)
        return None, (Check(FAIL, "系统节点", f"所选 AZ 里没有 {candidates} 中的任何机型"),)
    return itype, (Check(OK, f"系统节点：{SYSTEM_NODE_COUNT} × {itype}（托管节点组）"),)


def _standard_quota(gw: AwsGateway, advanced: Mapping[str, Any]) -> Checks:
    general = advanced["general_nodes"]
    title = (
        f"通用 CPU 节点池（非 GPU 业务）：{'/'.join(general['arch'])}，"
        f"{'/'.join(general['capacity_types'])}，上限 {general['cpu_limit']} vCPU"
    )
    limit = gw.quota("ec2", STANDARD_QUOTA_CODE)
    if limit is None:
        return (Check(WARN, title, "无法读取标准实例配额"),)
    try:
        used = gw.running_vcpus("standard")
    except GatewayError:
        used = 0.0
    available = limit - used
    need = general["cpu_limit"] + SYSTEM_NODE_COUNT * SYSTEM_NODE_VCPUS
    if available < need:
        detail = (
            f"标准实例配额剩余 {available:.0f} vCPU，少于节点池上限加系统节点所需的 {need} vCPU，"
            "超出部分会扩不出来。" + quota_increase_command(STANDARD_QUOTA_CODE, need + used)
        )
        return (Check(WARN, title, detail),)
    return (Check(OK, title, f"标准实例配额剩余 {available:.0f} vCPU"),)


def _prices(
    gw: AwsGateway, offerings: Mapping[str, FrozenSet[str]]
) -> Tuple[Dict[str, Optional[float]], Checks]:
    """查本 region 有货的所有 GPU profile 机型的价格（选现有 VPC 后主力机型可能变化）。"""
    offered = frozenset().union(*offerings.values()) if offerings else frozenset()
    types = [p.instance_type for p in PROFILES if p.instance_type in offered]
    if not types:
        return {}, ()
    prices = dict(gw.prices(types))
    missing = [t for t, v in prices.items() if v is None]
    if missing:
        return prices, (
            Check(WARN, "无法获取部分机型的价格", f"{', '.join(missing)}（需要 pricing:GetProducts 权限）"),
        )
    return prices, ()


def _odcr(gw: AwsGateway, selection: Optional[AzSelection]) -> Checks:
    if selection is None:
        return ()
    types = [t for p in selection.placements for t in p.profile.instance_types]
    found = [r for r in gw.capacity_reservations(types) if r.zone_id in selection.zone_ids]
    if not found:
        return ()
    detail = "；".join(
        f"{r.reservation_id} {r.instance_type} @ {r.zone_id}，剩余 {r.available_count} 台"
        f"（{'open：可直接使用' if r.open_matching else 'targeted：需要按 ID 指定'}）"
        for r in found
    )
    return (Check(INFO, "发现可用的容量预留（ODCR），可以给常驻副本使用", detail),)


def _observability(gw: AwsGateway) -> Tuple[str, Checks]:
    if not gw.service_available("grafana"):
        return "cloudwatch", (Check(INFO, "本 region 没有 Amazon Managed Grafana → 可观测性使用 CloudWatch"),)
    count = gw.idc_instance_count()
    if count is None:
        return "cloudwatch", (
            Check(
                WARN,
                "无法判断是否启用了 IAM Identity Center（缺少 sso:ListInstances 权限）→ 可观测性使用 CloudWatch",
                "之后可以用 qwenctl observability grafana 切换",
            ),
        )
    if count == 0:
        return "cloudwatch", (
            Check(
                INFO,
                "未启用 IAM Identity Center → 可观测性使用 CloudWatch",
                "之后可以用 qwenctl observability grafana 切换",
            ),
        )
    return "grafana", (Check(OK, "已启用 IAM Identity Center → 可观测性使用 Grafana（AMG）"),)


def _bedrock(gw: AwsGateway) -> Tuple[bool, Checks]:
    available = gw.bedrock_model_available(BEDROCK_OVERFLOW_MODEL_ID)
    if available:
        return True, (Check(INFO, f"Bedrock 溢出可用（{BEDROCK_OVERFLOW_MODEL_ID}），默认关闭"),)
    if available is None:
        return False, (
            Check(
                WARN,
                "无法判断 Bedrock 是否提供 Qwen3-VL（缺少 bedrock:ListFoundationModels 权限）→ 溢出选项不可用",
            ),
        )
    return False, (Check(INFO, "本 region 的 Bedrock 没有 Qwen3-VL → 溢出选项不可用"),)


def _cdk_bootstrap(gw: AwsGateway) -> Tuple[bool, Checks]:
    try:
        status = gw.stack_status("CDKToolkit")
    except GatewayError as exc:
        return False, (Check(WARN, "无法检查 CDK bootstrap", str(exc)),)
    if status is None:
        return False, (Check(INFO, "CDK 尚未 bootstrap", "首次部署时会先征得你同意再执行"),)
    if status in BROKEN_STACK_STATES or status.endswith("_IN_PROGRESS"):
        return True, (Check(WARN, f"CDKToolkit 栈的状态是 {status}", "部署前需要先处理好这个栈"),)
    return True, (Check(OK, "CDK 已经 bootstrap"),)


def _existing_deployment(gw: AwsGateway, name: str) -> Tuple[bool, Checks]:
    stack = f"{name}-bootstrap"
    try:
        status = gw.stack_status(stack)
    except GatewayError as exc:
        return False, (Check(WARN, "无法检查是否已有同名部署", str(exc)),)
    if status is None:
        return False, ()
    if status in BROKEN_STACK_STATES:
        detail = f"栈 {stack} 无法更新，请先删除（./qwenctl destroy --name {name}），或者换一个部署名称"
        return True, (Check(FAIL, f"已存在名为 {name} 的部署，但状态是 {status}", detail),)
    if status.endswith("_IN_PROGRESS"):
        return True, (Check(FAIL, f"名为 {name} 的部署正在变更中（{status}）", "请等它完成后再运行"),)
    return True, (Check(INFO, f"已存在名为 {name} 的部署 → 本次将更新它"),)


def _permissions(gw: AwsGateway, principal: str) -> Checks:
    if not principal:
        return ()
    try:
        denied = gw.denied_actions(principal, REQUIRED_ACTIONS)
    except GatewayError:
        return (Check(WARN, "无法预检权限（缺少 iam:SimulatePrincipalPolicy）", "部署时如果权限不足会报错"),)
    if denied:
        return (Check(FAIL, "当前身份缺少部署所需的权限", ", ".join(denied)),)
    return (Check(OK, "部署所需的关键权限已具备（抽查；SCP 的限制不一定能提前发现）"),)
