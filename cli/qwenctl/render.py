"""把环境检查结果和最终配置渲染成终端文本。"""

from __future__ import annotations

from typing import List

from .capacity import estimate_cost
from .console import Console
from .models import DeploymentConfig
from .preflight import PreflightReport


def show_preflight(console: Console, report: PreflightReport) -> None:
    console.heading("[1/3] 环境检查（只读）")
    for check in report.checks:
        console.status(check.status, check.title, check.detail)


def summary_lines(config: DeploymentConfig, report: PreflightReport) -> List[str]:
    a = config.answers
    general = config.advanced["general_nodes"]
    lines = [
        f"  部署名称      {config.name}",
        f"  账号 / Region {config.account} / {config.region}",
        f"  集群管理员    {config.admin_principal_arn}",
        f"  AZ           {', '.join(z.label for z in config.selection.zones)}",
        *_network_lines(config),
        *_endpoint_lines(config),
        f"  API 访问      {_access_text(config)}",
        f"  GPU 副本      {a.capacity.min_replicas} ~ {a.capacity.max_replicas}（按优先级回退）",
        *_gpu_lines(config),
        f"  系统节点      2 × {config.system_instance_type}",
        (
            f"  通用 CPU 节点 {'/'.join(general['arch'])}，{'/'.join(general['capacity_types'])}，"
            f"上限 {general['cpu_limit']} vCPU（客户的非 GPU 业务）"
        ),
        f"  异步通道      {'启用' if a.features.async_channel else '不启用'}",
        f"  Bedrock 溢出  {'启用' if a.features.bedrock_overflow else '不启用'}",
        f"  告警邮箱      {a.features.alert_email or '不发送'}",
        f"  可观测性      {'Grafana（AMG）' if config.observability_mode == 'grafana' else 'CloudWatch'}",
    ]
    return lines + _cost_lines(config, report)


def _network_lines(config: DeploymentConfig) -> List[str]:
    n = config.answers.network
    if n.mode == "new":
        nat = config.advanced["network"]["nat_gateways"]
        lines = [f"  网络          新建 VPC {n.cidr}，NAT × {nat}"]
        if nat == 1:
            lines.append(
                "                ⚠ 单 NAT：它所在的 AZ 故障时，另一个 AZ 无法访问公网（生产建议 2 个）"
            )
        return lines
    subnets = ", ".join(f"{s.subnet_id}（{s.zone_id}）" for s in n.private_subnets)
    return [f"  网络          现有 VPC {n.vpc_id}，私有子网 {subnets}"]


def _endpoint_lines(config: DeploymentConfig) -> List[str]:
    e = config.endpoints
    if config.answers.network.mode == "new":
        s3 = "S3 网关终端节点"
    elif e.s3_gateway:
        s3 = f"S3 网关终端节点（加到路由表 {', '.join(e.s3_route_table_ids)}）"
    else:
        s3 = "S3 网关终端节点：所选路由表已经有了，不再创建"
    lines = [f"  终端节点      {s3}"]
    if e.interface:
        lines.append(f"                接口终端节点：{', '.join(e.interface)}")
    lines.extend(f"                不创建 {service}：{reason}" for service, reason in e.skipped)
    return lines


def _gpu_lines(config: DeploymentConfig) -> List[str]:
    return [
        f"    {p.profile.instance_type:<13} {p.profile.gpu_model:<18} TP={p.profile.tensor_parallel}"
        f"  AZ: {', '.join(p.zone_ids)}{'  （需 PoC 验证）' if p.profile.experimental else ''}"
        for p in config.selection.placements
    ]


def _cost_lines(config: DeploymentConfig, report: PreflightReport) -> List[str]:
    a = config.answers
    primary = config.selection.primary.profile
    cost = estimate_cost(
        primary, report.prices.get(primary.instance_type), a.capacity.min_replicas, a.capacity.max_replicas
    )
    if cost.baseline_monthly is None or cost.peak_hourly is None:
        return []
    text = (
        f"常驻 ≈ ${cost.baseline_monthly:,.0f}/月；满载 ≈ ${cost.peak_hourly:,.2f}/h（GPU 部分，On-Demand）"
    )
    return [f"  预计成本      {text}"]


def _access_text(config: DeploymentConfig) -> str:
    acc = config.answers.access
    if acc.mode == "internal":
        return "仅 VPC 内部（internal ALB）"
    cidrs = ", ".join(acc.allowed_cidrs) if acc.allowed_cidrs else "⚠ 对整个公网开放（已确认）"
    return f"公网 HTTPS，证书 {acc.certificate_arn}，来源 {cidrs}"
