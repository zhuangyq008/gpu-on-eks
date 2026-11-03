"""命令行入口：qwenctl preflight / deploy。"""

from __future__ import annotations

import argparse
import json
import re
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from . import __version__
from .advanced import ConfigError, load_advanced
from .console import Aborted, Console
from .endpoints import plan_endpoints, wanted_interface_endpoints
from .gateway import AwsGateway, GatewayError
from .installer import InstallError
from .models import DeploymentConfig
from .paths import DeploymentPaths
from .preflight import PreflightReport, pick_system_instance_type, run_preflight
from .questions import ask_all
from .render import show_preflight, summary_lines
from .store import save_config
from .toolchain import ToolError
from .versions import VersionsError

EXIT_OK = 0
EXIT_PREFLIGHT_FAILED = 1
EXIT_ABORTED = 2
EXIT_CONFIG_ERROR = 3
EXIT_AWS_ERROR = 4
EXIT_UNEXPECTED = 5
EXIT_INSTALL_FAILED = 6

DEFAULT_NAME = "qwen-vl"
_NAME = re.compile(r"^[a-z][a-z0-9-]{1,18}[a-z0-9]$")

GatewayFactory = Callable[[Optional[str]], AwsGateway]


@dataclass(frozen=True)
class InstallRequest:
    name: str
    region: str
    account: str
    config: Mapping[str, Any]
    config_path: str
    cdk_bootstrapped: bool
    resume: bool
    console: Console


InstallFn = Callable[[InstallRequest], None]


def _default_gateway(region: Optional[str]) -> AwsGateway:
    from .aws_gateway import Boto3Gateway  # 延迟导入：单元测试不需要 boto3

    return Boto3Gateway(region=region)


def _default_install(request: InstallRequest) -> None:  # pragma: no cover — 真实执行，由 E2E 验证
    from .installer import InstallContext, run_install
    from .paths import INFRA_DIR, bin_dir
    from .runner import SubprocessRunner
    from .toolchain import detect_arch, ensure_tools, missing_commands
    from .versions import load_versions

    missing = missing_commands()
    if missing:
        raise ToolError(f"缺少命令：{', '.join(missing)}（CloudShell 自带；在跳板机上运行时需要先安装）")
    versions = load_versions()
    request.console.status("info", "准备 kubectl / helm（锁定版本，校验 sha256）")
    tools = ensure_tools(versions, bin_dir(), detect_arch())
    env = {"AWS_REGION": request.region, "AWS_DEFAULT_REGION": request.region}
    ctx = InstallContext(
        name=request.name,
        region=request.region,
        account=request.account,
        config=request.config,
        config_path=request.config_path,
        paths=DeploymentPaths.for_name(request.name).ensure(),
        versions=versions,
        tools=tools,
        runner=SubprocessRunner(env, extra_path=[bin_dir()]),
        console=request.console,
        infra_dir=INFRA_DIR,
        cdk_bootstrapped=request.cdk_bootstrapped,
    )
    run_install(ctx, resume=request.resume)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qwenctl", description="Qwen-VL on EKS 部署工具")
    parser.add_argument("--version", action="version", version=f"qwenctl {__version__}")
    parser.add_argument("--debug", action="store_true", help="出错时打印完整的调用栈")
    sub = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("preflight", "只做环境检查（只读）"),
        ("deploy", "环境检查 + 提问 + 确认 + 部署"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--name", default=DEFAULT_NAME, help=f"部署名称（默认 {DEFAULT_NAME}）")
        cmd.add_argument("--region", default=None, help="默认使用 CloudShell 的 AWS_REGION")
        cmd.add_argument("--config", default=None, help="高级配置文件 config.yaml（可选）")
        if name == "deploy":
            cmd.add_argument("--yes", action="store_true", help="不提问，全部使用默认值")
            cmd.add_argument(
                "--approve", action="store_true", help="不再确认，直接部署（和 --yes 一起用于自动化）"
            )
            cmd.add_argument("--plan", action="store_true", help="只生成并保存配置，不部署")
            cmd.add_argument("--resume", action="store_true", help="用上次保存的配置，从中断的阶段继续")
            cmd.add_argument(
                "--output", default=None, help="配置保存路径（默认 ~/.qwenctl/<name>/config.json）"
            )
    return parser


def validate_name(name: str) -> None:
    if not _NAME.fullmatch(name):
        raise ConfigError("部署名称只能用小写字母、数字和连字符，3~20 个字符，以字母开头")


def main(
    argv: Optional[Sequence[str]] = None,
    gateway_factory: GatewayFactory = _default_gateway,
    console: Optional[Console] = None,
    install: Optional[InstallFn] = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_CONFIG_ERROR
    con = console or Console(assume_defaults=getattr(args, "yes", False))
    try:
        validate_name(args.name)
        install_fn = install or _default_install  # 调用时再解析，测试可以替换
        if getattr(args, "resume", False):
            return _resume(con, gateway_factory(args.region), args, install_fn)
        advanced = load_advanced(args.config)
        gw = gateway_factory(args.region)
        report = run_preflight(gw, args.name, advanced)
        show_preflight(con, report)
        if not report.passed:
            con.line()
            con.status("fail", "环境检查没有通过，请先处理上面标 ✗ 的问题")
            return EXIT_PREFLIGHT_FAILED
        if args.command == "preflight":
            return EXIT_OK
        return _deploy(con, report, gw, advanced, args, install_fn)
    except (ConfigError, VersionsError) as exc:
        con.status("fail", str(exc))
        return EXIT_CONFIG_ERROR
    except GatewayError as exc:
        con.status("fail", str(exc))
        return EXIT_AWS_ERROR
    except (InstallError, ToolError) as exc:
        return _install_failed(con, exc, args.name)
    except Aborted as exc:
        con.line()
        con.status("info", f"{exc}，没有做任何改动")
        return EXIT_ABORTED
    except KeyboardInterrupt:
        con.line()
        con.status(
            "info",
            "已中断。已经提交给 CloudFormation 的变更会继续执行；之后运行 ./qwenctl deploy --resume 继续",
        )
        return EXIT_ABORTED
    except Exception as exc:  # noqa: BLE001 — 兜底：客户不应该看到 Python 调用栈
        con.status("fail", f"意外错误：{type(exc).__name__}: {exc}")
        con.line("      请把这段输出发给交付团队，或加 --debug 重新运行查看详情")
        if args.debug:
            con.line(traceback.format_exc())
        return EXIT_UNEXPECTED


def _install_failed(con: Console, exc: Exception, name: str) -> int:
    phase = getattr(exc, "phase", "准备工具")
    con.line()
    con.status("fail", f"部署在「{phase}」阶段失败：{exc}")
    con.line(f"      处理好之后运行 ./qwenctl deploy --name {name} --resume，会从这个阶段继续")
    return EXIT_INSTALL_FAILED


def _deploy(
    con: Console,
    report: PreflightReport,
    gw: AwsGateway,
    advanced: Mapping[str, Any],
    args: argparse.Namespace,
    install: InstallFn,
) -> int:
    answers, selection = ask_all(con, report, gw, advanced)
    system_type = pick_system_instance_type(selection, report.offerings)
    if system_type is None:
        con.status("fail", "所选 AZ 里没有可用的系统节点机型")
        return EXIT_PREFLIGHT_FAILED
    endpoints = plan_endpoints(
        advanced["network"]["interface_endpoints"],
        answers.network,
        answers.features,
        selection.zone_ids,
        gw.endpoint_service_zones(wanted_interface_endpoints(answers.features)),
    )
    config = DeploymentConfig(
        name=report.name,
        account=report.account,
        region=report.region,
        admin_principal_arn=report.principal_arn,
        selection=selection,
        system_instance_type=system_type,
        observability_mode=report.observability_mode,
        answers=answers,
        endpoints=endpoints,
        advanced=advanced,
    )
    con.heading("[3/3] 确认")
    for line in summary_lines(config, report):
        con.line(line)
    if not args.plan:
        con.line("  确认后会在这个账号里创建上面的资源（CloudFormation + Helm），首次约 40 分钟")
    if not con.ask_bool("继续？", True):
        raise Aborted("已取消")
    path = save_config(config, args.output or DeploymentPaths.for_name(report.name).config)
    con.status("ok", f"配置已保存：{path}")
    if args.plan or (not con.interactive and not args.approve):
        hint = "" if args.plan else "（非交互模式需要再加 --approve 才会部署）"
        con.status("info", f"只保存了配置，没有在账号里创建任何资源{hint}")
        return EXIT_OK
    _confirm_bootstrap(con, report, args.approve)
    request = _request(con, report.name, report.region, report.account, config.to_dict(), path)
    install(InstallRequest(**{**request.__dict__, "cdk_bootstrapped": report.cdk_bootstrapped}))
    _print_done(con, report.name)
    return EXIT_OK


def _confirm_bootstrap(con: Console, report: PreflightReport, approved: bool) -> None:
    if report.cdk_bootstrapped:
        return
    con.line()
    con.status(
        "info",
        "首次部署需要在这个账号和 region 执行 cdk bootstrap（创建 CDKToolkit 栈：一个 S3 桶和几个 IAM 角色）",
    )
    if not approved and not con.ask_bool("同意执行 cdk bootstrap？", True):
        raise Aborted("没有同意 cdk bootstrap")


def _resume(con: Console, gw: AwsGateway, args: argparse.Namespace, install: InstallFn) -> int:
    path = args.output or DeploymentPaths.for_name(args.name).config
    try:
        with open(path, encoding="utf-8") as handle:
            config: Dict[str, Any] = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"找不到可以继续的部署配置 {path}，请先运行 ./qwenctl deploy") from exc
    _validate_saved_config(config, gw)
    con.status("info", f"继续部署 {config['name']}（{config['account']} / {config['region']}）")
    request = _request(con, config["name"], config["region"], config["account"], config, path)
    bootstrapped = gw.stack_status("CDKToolkit") is not None
    install(InstallRequest(**{**request.__dict__, "cdk_bootstrapped": bootstrapped, "resume": True}))
    _print_done(con, config["name"])
    return EXIT_OK


_ACCOUNT = re.compile(r"^\d{12}$")
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")


def _validate_saved_config(config: Mapping[str, Any], gw: AwsGateway) -> None:
    """保存的配置会进入 aws/cdk 命令行和本地路径：继续之前重新校验，并确认没有换账号或 region。"""
    try:
        name, account, region = str(config["name"]), str(config["account"]), str(config["region"])
    except KeyError as exc:
        raise ConfigError(f"保存的部署配置缺少字段：{exc}") from exc
    validate_name(name)
    if not _ACCOUNT.fullmatch(account) or not _REGION.fullmatch(region):
        raise ConfigError("保存的部署配置里账号或 region 格式不对，请重新运行 ./qwenctl deploy")
    caller_account, _ = gw.caller_identity()
    if caller_account != account or gw.region() != region:
        raise ConfigError(
            f"当前登录的是 {caller_account} / {gw.region()}，但这个部署属于 {account} / {region}。"
            "请在正确的账号和 region 打开 CloudShell"
        )


def _request(
    con: Console, name: str, region: str, account: str, config: Mapping[str, Any], path: str
) -> InstallRequest:
    return InstallRequest(
        name, region, account, config, path, cdk_bootstrapped=False, resume=False, console=con
    )


def _print_done(con: Console, name: str) -> None:
    paths = DeploymentPaths.for_name(name)
    con.line()
    con.status("ok", f"部署完成：{name}")
    con.line(f"      kubectl 配置：export KUBECONFIG={paths.kubeconfig}")
    con.line(
        "      客户的非 GPU 业务直接 kubectl apply 即可，会调度到通用 CPU 节点池（Karpenter 按需扩出节点）"
    )
