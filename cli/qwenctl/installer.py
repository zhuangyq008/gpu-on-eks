"""客户端安装流程（docs/03 §1.2）：按阶段执行，每个阶段都可以重复执行。

AWS 资源走 CloudFormation（cdk deploy，服务端执行）；集群内组件由本机执行 helm/kubectl。
CloudShell 断线后运行 ./qwenctl deploy --resume，从第一个未完成的阶段继续。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .console import Console
from .manifests import render
from .paths import DeploymentPaths, write_private
from .runner import CommandError, Runner
from .serving import base_manifests, model_prefix, sync_job, sync_job_name, vllm_deployments

KARPENTER_NAMESPACE = "kube-system"
HELM_TIMEOUT = "10m"


class InstallError(Exception):
    def __init__(self, phase: str, message: str) -> None:
        self.phase = phase
        super().__init__(message)


@dataclass(frozen=True)
class InstallContext:
    name: str
    region: str
    account: str
    config: Mapping[str, Any]
    config_path: str
    paths: DeploymentPaths
    versions: Mapping[str, Any]
    tools: Mapping[str, str]
    runner: Runner
    console: Console
    infra_dir: str
    cdk_bootstrapped: bool

    @property
    def cdk(self) -> str:
        return os.path.join(self.infra_dir, "node_modules", ".bin", "cdk")

    @property
    def qualifier(self) -> Optional[str]:
        return self.config["advanced"]["cdk"]["bootstrap_qualifier"]


Phase = Tuple[str, str, Callable[[InstallContext], None]]


def _cdk_deps(ctx: InstallContext) -> None:
    modules = os.path.join(ctx.infra_dir, "node_modules")
    if not os.path.isdir(os.path.join(modules, "aws-cdk-lib")):
        # --ignore-scripts：依赖的安装脚本会带着执行人的 AWS 凭证运行，CDK 不需要它们
        ctx.runner.run(
            ["npm", "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=ctx.infra_dir
        )
    if os.path.isdir(os.path.join(modules, "typescript")):
        ctx.runner.run(["npm", "run", "build"], cwd=ctx.infra_dir)  # 开发环境：每次重新编译
    elif not os.path.isfile(os.path.join(ctx.infra_dir, "dist", "bin", "app.js")):
        raise InstallError("cdk-deps", "交付包里缺少编译好的 infra/dist，请使用发布版的交付包")


def _qualifier_args(ctx: InstallContext) -> List[str]:
    return ["-c", f"@aws-cdk/core:bootstrapQualifier={ctx.qualifier}"] if ctx.qualifier else []


def _cdk_bootstrap(ctx: InstallContext) -> None:
    if ctx.cdk_bootstrapped:
        ctx.console.status("ok", "CDK 已经 bootstrap，跳过")
        return
    qualifier = ["--qualifier", ctx.qualifier] if ctx.qualifier else []
    # 在 infra/ 下运行时 cdk 会先执行 cdk.json 里的 app，app 需要部署配置
    ctx.runner.run(
        [ctx.cdk, "bootstrap", f"aws://{ctx.account}/{ctx.region}", *qualifier]
        + ["-c", f"configFile={ctx.config_path}", *_qualifier_args(ctx)],
        cwd=ctx.infra_dir,
    )


def _cdk_deploy(ctx: InstallContext) -> None:
    ctx.runner.run(
        [
            ctx.cdk,
            "deploy",
            "--all",
            "--require-approval",
            "never",  # 客户已经在 qwenctl 的确认页确认过
            "--progress",
            "events",
            "-c",
            f"configFile={ctx.config_path}",
            *_qualifier_args(ctx),
            "--outputs-file",
            ctx.paths.outputs,
        ],
        cwd=ctx.infra_dir,
    )


def _kubeconfig(ctx: InstallContext) -> None:
    ctx.runner.run(
        ["aws", "eks", "update-kubeconfig", "--name", ctx.name, "--region", ctx.region]
        + ["--kubeconfig", ctx.paths.kubeconfig, "--alias", ctx.name]
    )
    if os.path.exists(ctx.paths.kubeconfig):
        os.chmod(ctx.paths.kubeconfig, 0o600)


def stack_outputs(ctx: InstallContext, suffix: str) -> Dict[str, str]:
    """以 CloudFormation 为准（outputs.json 可能是删掉重建之前的旧值）；查询失败时才用本地文件。"""
    stack = f"{ctx.name}-{suffix}"
    try:
        result = ctx.runner.run(
            ["aws", "cloudformation", "describe-stacks", "--stack-name", stack, "--region", ctx.region]
            + ["--query", "Stacks[0].Outputs", "--output", "json"],
            capture=True,
        )
        return {o["OutputKey"]: o["OutputValue"] for o in json.loads(result.stdout or "[]")}
    except (CommandError, ValueError):
        if not os.path.isfile(ctx.paths.outputs):
            raise
    with open(ctx.paths.outputs, encoding="utf-8") as handle:
        return dict(json.load(handle).get(stack, {}))


def eks_outputs(ctx: InstallContext) -> Dict[str, str]:
    return stack_outputs(ctx, "eks")


def karpenter_values(name: str, region: str, interruption_queue: str) -> Dict[str, Any]:
    return {
        "settings": {"clusterName": name, "interruptionQueue": interruption_queue},
        "replicas": 2,
        "serviceAccount": {"name": "karpenter"},
        # 跑在系统节点组上（Karpenter 不能管理自己所在的节点）
        "nodeSelector": {"qwenctl/node-role": "system"},
        "tolerations": [{"key": "CriticalAddonsOnly", "operator": "Exists"}],
        "controller": {
            # 节点 IMDS hop limit = 1，Pod 读不到 IMDS，region 必须显式给出
            "env": [{"name": "AWS_REGION", "value": region}],
            "resources": {
                "requests": {"cpu": "500m", "memory": "512Mi"},
                "limits": {"cpu": "1", "memory": "1Gi"},
            },
        },
    }


def helm_registry_isolation(root: str) -> Tuple[Dict[str, str], List[str]]:
    """公开 chart 不需要登录；隔离 registry 凭证，避免本机残留的过期 public.ecr.aws 登录导致 403。"""
    directory = os.path.join(root, "helm-registry")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    config = os.path.join(directory, "config.json")
    write_private(config, "{}")
    return {"DOCKER_CONFIG": directory}, ["--registry-config", config]


def pull_chart(ctx: InstallContext, chart: str) -> str:
    """先下载 chart 包并按 versions.json 里的 sha256 校验，再从本地文件安装（tag 可能被重新推送）。"""
    spec = ctx.versions["charts"][chart]
    directory = os.path.join(os.path.dirname(ctx.paths.root), "charts")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    package = os.path.join(directory, f"{chart}-{spec['version']}.tgz")
    if not (os.path.isfile(package) and sha256_file(package) == spec["sha256"]):
        env, registry_args = helm_registry_isolation(ctx.paths.root)
        ctx.runner.run(
            [ctx.tools["helm"], "pull", f"{spec['repository']}/{chart}", "--version", spec["version"]]
            + ["--destination", directory, *registry_args],
            env=env,
        )
        actual = sha256_file(package) if os.path.isfile(package) else "（没有下载到文件）"
        if actual != spec["sha256"]:
            if os.path.isfile(package):
                os.unlink(package)
            raise InstallError("karpenter", f"chart {chart} 校验失败：期望 {spec['sha256']}，实际 {actual}")
    return package


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _helm(ctx: InstallContext, release: str, chart: str, extra: Sequence[str] = ()) -> None:
    package = pull_chart(ctx, chart)
    ctx.runner.run(
        [ctx.tools["helm"], "upgrade", "--install", release, package, "--namespace", KARPENTER_NAMESPACE]
        + ["--kubeconfig", ctx.paths.kubeconfig, "--wait", "--timeout", HELM_TIMEOUT, *extra]
    )


def _karpenter(ctx: InstallContext) -> None:
    outputs = eks_outputs(ctx)
    queue = outputs.get("KarpenterInterruptionQueueName")
    if not queue:
        raise InstallError("karpenter", "EKS 栈的输出里没有 KarpenterInterruptionQueueName")
    values_path = os.path.join(ctx.paths.root, "karpenter-values.json")
    write_private(values_path, json.dumps(karpenter_values(ctx.name, ctx.region, queue), indent=2))
    _helm(ctx, "karpenter-crd", "karpenter-crd")
    # CRD 由 karpenter-crd 管理（升级时 helm 不会更新 chart 自带的 CRD）
    _helm(ctx, "karpenter", "karpenter", ["--skip-crds", "-f", values_path])


def _kubectl(ctx: InstallContext, *args: str) -> List[str]:
    return [ctx.tools["kubectl"], "--kubeconfig", ctx.paths.kubeconfig, *args]


def _apply(ctx: InstallContext, manifests: Sequence[Mapping[str, Any]]) -> None:
    payload = json.dumps({"apiVersion": "v1", "kind": "List", "items": list(manifests)})
    ctx.runner.run(
        _kubectl(ctx, "apply", "--server-side", "--field-manager", "qwenctl", "-f", "-"), input_text=payload
    )


def _nodepools(ctx: InstallContext) -> None:
    _apply(ctx, render(ctx.config, eks_outputs(ctx), ctx.versions["bottlerocket"]))


def _verify(ctx: InstallContext) -> None:
    ctx.runner.run(
        _kubectl(ctx, "-n", KARPENTER_NAMESPACE, "rollout", "status", "deployment/karpenter", "--timeout=5m")
    )
    # EC2NodeClass Ready 说明 Karpenter 的 IAM、子网、安全组、AMI 都没问题
    ctx.runner.run(
        _kubectl(
            ctx, "wait", "--for=condition=Ready", "ec2nodeclass/general", "ec2nodeclass/gpu", "--timeout=5m"
        )
    )
    ctx.runner.run(_kubectl(ctx, "get", "nodes,nodepools", "-o", "wide"))


# 等待时间（秒）；测试里会替换 _sleep / _now
SYNC_TIMEOUT = 3600
SERVING_TIMEOUT = 1500
POLL_INTERVAL = 15
_sleep = time.sleep
_now = time.monotonic


def _model_bucket(ctx: InstallContext) -> str:
    bucket = stack_outputs(ctx, "serving").get("ModelBucketName")
    if not bucket:
        raise InstallError(
            "model-sync", "推理服务栈的输出里没有 ModelBucketName，请先完成 CloudFormation 部署"
        )
    return bucket


def _get_json(ctx: InstallContext, *args: str) -> Dict[str, Any]:
    result = ctx.runner.run(_kubectl(ctx, *args, "-o", "json"), capture=True)
    return json.loads(result.stdout or "{}")


def _model_sync(ctx: InstallContext) -> None:
    namespace = ctx.config["serving"]["namespace"]
    job = sync_job_name(ctx.versions)
    _apply(ctx, base_manifests(ctx.config) + sync_job(ctx.config, ctx.versions, _model_bucket(ctx)))
    deadline = _now() + SYNC_TIMEOUT
    while _now() < deadline:
        status = _get_json(ctx, "-n", namespace, "get", "job", job).get("status", {})
        if status.get("succeeded"):
            ctx.console.status(
                "ok", f"权重已同步到 s3://{_model_bucket(ctx)}/{model_prefix(ctx.config, ctx.versions)}"
            )
            return
        failed = [
            c for c in status.get("conditions", []) if c.get("type") == "Failed" and c.get("status") == "True"
        ]
        if failed:
            logs = ctx.runner.run(
                _kubectl(ctx, "-n", namespace, "logs", f"job/{job}", "--tail=20"), capture=True
            )
            raise InstallError("model-sync", f"权重同步失败：{failed[0].get('message', '')}\n{logs.stdout}")
        _sleep(POLL_INTERVAL)
    raise InstallError("model-sync", f"权重同步超过 {SYNC_TIMEOUT // 60} 分钟还没完成")


def _pod_summary(pod: Mapping[str, Any]) -> str:
    status = pod.get("status", {})
    for c in status.get("containerStatuses", []):
        waiting = c.get("state", {}).get("waiting")
        if waiting:
            return waiting.get("reason", "Waiting")
        if c.get("ready"):
            return "Ready"
        if "running" in c.get("state", {}):
            return "Running（加载模型中）"
    return status.get("phase", "Unknown")


def _serving(ctx: InstallContext) -> None:
    namespace = ctx.config["serving"]["namespace"]
    _apply(ctx, base_manifests(ctx.config) + vllm_deployments(ctx.config, ctx.versions, _model_bucket(ctx)))
    started, last = _now(), ""
    while _now() - started < SERVING_TIMEOUT:
        pods = _get_json(ctx, "-n", namespace, "get", "pods", "-l", "app=qwen-vl").get("items", [])
        summary = (
            ", ".join(sorted(f"{p['metadata']['name']}={_pod_summary(p)}" for p in pods)) or "（还没有 Pod）"
        )
        if summary != last:
            ctx.console.status("info", f"{int(_now() - started)}s：{summary}")
            last = summary
        if any(_pod_summary(p) == "Ready" for p in pods):
            ctx.console.status("ok", f"推理服务已就绪（{int(_now() - started)}s）")
            return
        _sleep(POLL_INTERVAL)
    ctx.console.status(
        "warn",
        f"{SERVING_TIMEOUT // 60} 分钟内没有就绪的 vLLM 副本，通常是主力 GPU 机型没有容量（ICE）",
    )
    ctx.console.line(f"      查看原因：kubectl -n {namespace} get events --sort-by=.lastTimestamp | tail")
    ctx.console.line(
        f"      临时改用回退机型：kubectl -n {namespace} scale deploy/vllm-<回退 profile> --replicas=1"
    )


PHASES: Tuple[Phase, ...] = (
    ("cdk-deps", "准备 CDK（依赖和编译）", _cdk_deps),
    ("cdk-bootstrap", "CDK bootstrap（只有首次需要）", _cdk_bootstrap),
    ("cdk-deploy", "部署 AWS 资源：网络、EKS（CloudFormation，首次约 15 分钟）", _cdk_deploy),
    ("kubeconfig", "配置 kubectl", _kubeconfig),
    ("karpenter", "安装 Karpenter", _karpenter),
    ("nodepools", "创建节点池：通用 CPU（非 GPU 业务）+ GPU", _nodepools),
    ("verify", "检查集群状态", _verify),
    ("model-sync", "同步模型权重到 S3（首次约 5 分钟）", _model_sync),
    ("serving", "部署 vLLM 推理服务（首次约 5~8 分钟）", _serving),
)


def load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"completed": []}
    return data if isinstance(data.get("completed"), list) else {"completed": []}


def _save_state(path: str, state: Mapping[str, Any]) -> None:
    write_private(path, json.dumps(state, ensure_ascii=False, indent=2))


def run_install(ctx: InstallContext, resume: bool, phases: Sequence[Phase] = PHASES) -> Dict[str, Any]:
    """返回最终状态。resume=True 时跳过已完成的阶段；否则全部重新执行（用于更新）。"""
    state: Dict[str, Any] = load_state(ctx.paths.state) if resume else {"completed": []}
    done = set(state["completed"])
    for index, (key, title, action) in enumerate(phases, start=1):
        ctx.console.heading(f"[{index}/{len(phases)}] {title}")
        if resume and key in done:
            ctx.console.status("ok", "上次已完成，跳过")
            continue
        try:
            action(ctx)
        except CommandError as exc:
            raise InstallError(key, f"{exc}") from exc
        state = {"completed": [*state["completed"], key], "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        _save_state(ctx.paths.state, state)
        ctx.console.status("ok", "完成")
    return state
