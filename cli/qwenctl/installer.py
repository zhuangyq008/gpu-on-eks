"""客户端安装流程（docs/03 §1.2）：按阶段执行，每个阶段都可以重复执行。

AWS 资源走 CloudFormation（cdk deploy，服务端执行）；集群内组件由本机执行 helm/kubectl。
CloudShell 断线后运行 ./qwenctl deploy --resume，从第一个未完成的阶段继续。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import compile_cache
from .addons import (
    KEDA_NAMESPACE,
    PROMETHEUS_NAMESPACE,
    PROMETHEUS_RELEASE,
    keda_values,
    namespace_manifest,
    prometheus_values,
)
from .autoscaling import pod_monitor, scaled_objects
from .console import Console
from .manifests import render
from .paths import DeploymentPaths, write_private
from .runner import CommandError, Runner
from .serving import base_manifests, model_prefix, sync_job, vllm_deployments

KARPENTER_NAMESPACE = "kube-system"
HELM_TIMEOUT = "10m"
PROMETHEUS_TIMEOUT = 600


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
    """先下载 chart 包并按 versions.json 里的 sha256 校验，再从本地文件安装（tag / 版本可能被重新推送）。"""
    spec = ctx.versions["charts"][chart]
    directory = os.path.join(os.path.dirname(ctx.paths.root), "charts")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    package = os.path.join(directory, f"{chart}-{spec['version']}.tgz")
    if not (os.path.isfile(package) and sha256_file(package) == spec["sha256"]):
        env, registry_args = helm_registry_isolation(ctx.paths.root)
        repository = spec["repository"]
        source = (
            [f"{repository}/{chart}"] if repository.startswith("oci://") else [chart, "--repo", repository]
        )
        ctx.runner.run(
            [ctx.tools["helm"], "pull", *source, "--version", spec["version"]]
            + ["--destination", directory, *registry_args],
            env=env,
        )
        actual = sha256_file(package) if os.path.isfile(package) else "（没有下载到文件）"
        if actual != spec["sha256"]:
            if os.path.isfile(package):
                os.unlink(package)
            raise InstallError("helm", f"chart {chart} 校验失败：期望 {spec['sha256']}，实际 {actual}")
    return package


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _helm(
    ctx: InstallContext,
    release: str,
    chart: str,
    extra: Sequence[str] = (),
    namespace: str = KARPENTER_NAMESPACE,
) -> None:
    package = pull_chart(ctx, chart)
    ctx.runner.run(
        [
            ctx.tools["helm"],
            "upgrade",
            "--install",
            release,
            package,
            "--namespace",
            namespace,
            "--create-namespace",
        ]
        + ["--kubeconfig", ctx.paths.kubeconfig, "--wait", "--timeout", HELM_TIMEOUT, *extra]
    )


def _helm_values(ctx: InstallContext, name: str, values: Mapping[str, Any]) -> List[str]:
    path = os.path.join(ctx.paths.root, f"{name}-values.json")
    write_private(path, json.dumps(values, indent=2))
    return ["-f", path]


def _metrics(ctx: InstallContext) -> None:
    _apply(ctx, [namespace_manifest(PROMETHEUS_NAMESPACE)])
    _helm(
        ctx,
        PROMETHEUS_RELEASE,
        "kube-prometheus-stack",
        _helm_values(ctx, "prometheus", prometheus_values(ctx.config["serving"]["namespace"])),
        namespace=PROMETHEUS_NAMESPACE,
    )
    # Prometheus 的 StatefulSet 由 operator 异步创建，helm 不会等它；等它可用再装 KEDA，
    # 否则 KEDA 第一次查询会被拒绝（虽然 fallback 能兜住，但会刷一堆错误日志）
    ctx.runner.run(
        _kubectl(
            ctx,
            "wait",
            f"prometheus.monitoring.coreos.com/{PROMETHEUS_RELEASE}-prometheus",
            "-n",
            PROMETHEUS_NAMESPACE,
            "--for=condition=Available",
            f"--timeout={PROMETHEUS_TIMEOUT}s",
        )
    )


def _keda(ctx: InstallContext) -> None:
    _apply(ctx, [namespace_manifest(KEDA_NAMESPACE)])
    _helm(ctx, "keda", "keda", _helm_values(ctx, "keda", keda_values()), namespace=KEDA_NAMESPACE)


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


FIELD_MANAGER = "qwenctl"
# Deployment 的 replicas 由单独的 field manager 持有：qwenctl 的 apply 省略 replicas（交给 KEDA），
# 如果 replicas 只归 qwenctl 所有，server-side apply 会把它删掉、回到默认的 1——回退 profile 就会
# 平白拉起一台 GPU 节点。见 Kubernetes 文档「Server-Side Apply → Transferring ownership」
REPLICAS_MANAGER = "qwenctl-replicas"


def _apply(ctx: InstallContext, manifests: Sequence[Mapping[str, Any]], manager: str = FIELD_MANAGER) -> None:
    payload = json.dumps({"apiVersion": "v1", "kind": "List", "items": list(manifests)})
    ctx.runner.run(
        _kubectl(ctx, "apply", "--server-side", "--field-manager", manager, "-f", "-"), input_text=payload
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


def _s3_exists(ctx: InstallContext, bucket: str, key: str) -> Tuple[bool, str]:
    """(对象是否存在, 404 以外的错误信息)。没有 s3:ListBucket 权限时，S3 对不存在的对象返回 403 而不是 404。"""
    try:
        ctx.runner.run(
            ["aws", "s3api", "head-object", "--bucket", bucket, "--key", key, "--region", ctx.region],
            capture=True,
        )
    except CommandError as exc:
        return False, "" if "An error occurred (404)" in exc.output else exc.output.strip()[:200]
    return True, ""


def _already_synced(ctx: InstallContext, bucket: str) -> bool:
    """S3 上有 _COMPLETE 标记就不用再起 Job（起 Job 要拉起 CPU 节点、拉 9 GB 镜像）。

    标记在 revision 专属的前缀下，只有同步 Job 的角色能写（docs/03 §1.3）。同步脚本或镜像升级后
    不会重新校验已完成的 revision；要强制重新同步，删掉这个标记即可。"""
    key = model_prefix(ctx.config, ctx.versions) + "_COMPLETE"
    exists, detail = _s3_exists(ctx, bucket, key)
    if detail:
        # 照常起 Job（Job 自己会逐文件校验），但要让客户知道
        ctx.console.status(
            "info",
            f"没法确认 s3://{bucket}/{key} 是否存在（{detail}），照常运行同步 Job（已同步的文件会跳过）",
        )
    return exists


def _model_sync(ctx: InstallContext) -> None:
    ns = ctx.config["serving"]["namespace"]
    bucket = _model_bucket(ctx)
    if _already_synced(ctx, bucket):
        ctx.console.status(
            "ok", f"s3://{bucket}/{model_prefix(ctx.config, ctx.versions)} 已经同步并校验过，跳过"
        )
        _apply(ctx, base_manifests(ctx.config))
        return
    manifests = sync_job(ctx.config, ctx.versions, bucket)
    job = next(m["metadata"]["name"] for m in manifests if m["kind"] == "Job")
    _apply(ctx, base_manifests(ctx.config) + manifests)
    deadline = _now() + SYNC_TIMEOUT
    while _now() < deadline:
        status = _get_json(ctx, "-n", ns, "get", "job", job).get("status", {})
        if status.get("succeeded"):
            ctx.console.status("ok", f"权重已同步到 s3://{bucket}/{model_prefix(ctx.config, ctx.versions)}")
            return
        failed = [
            c for c in status.get("conditions", []) if c.get("type") == "Failed" and c.get("status") == "True"
        ]
        if failed:
            logs = ctx.runner.run(_kubectl(ctx, "-n", ns, "logs", f"job/{job}", "--tail=20"), capture=True)
            raise InstallError("model-sync", f"权重同步失败：{failed[0].get('message', '')}\n{logs.stdout}")
        _sleep(POLL_INTERVAL)
    raise InstallError("model-sync", f"权重同步超过 {SYNC_TIMEOUT // 60} 分钟还没完成")


def _deployment_replicas(ctx: InstallContext, ns: str) -> Dict[str, int]:
    """已有 Deployment 的当前副本数。命名空间不存在时 kubectl 返回空列表；其他错误（权限、超时）要报出来，
    不能当成「还没有」——否则会按新建处理，把副本数重置成初始值。"""
    items = _get_json(ctx, "-n", ns, "get", "deployments", "-l", "app=qwen-vl").get("items", [])
    return {d["metadata"]["name"]: int(d["spec"].get("replicas", 1)) for d in items}


def _hand_over_replicas(ctx: InstallContext, ns: str, replicas: Mapping[str, int]) -> None:
    """把 replicas 交给 REPLICAS_MANAGER（值不变）。

    每个 Deployment 单独 apply：只带 replicas 的部分对象如果包在 v1 List 里，kubectl 会在客户端按完整
    Deployment 做 schema 校验（缺 selector / template）而拒绝；单个对象由服务端校验合并后的结果。"""
    for name, count in sorted(replicas.items()):
        partial = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": ns},
            "spec": {"replicas": count},
        }
        ctx.runner.run(
            _kubectl(ctx, "apply", "--server-side", "--field-manager", REPLICAS_MANAGER, "-f", "-"),
            input_text=json.dumps(partial),
        )


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
    ns = ctx.config["serving"]["namespace"]
    try:
        scalers = scaled_objects(ctx.config)
    except ValueError as exc:
        raise InstallError("serving", str(exc)) from exc
    current = _deployment_replicas(ctx, ns)
    # 已有的：先交出 replicas（兼容旧版本，或者上次在交接前中断），再做省略 replicas 的 apply
    _hand_over_replicas(ctx, ns, current)
    deployments = vllm_deployments(ctx.config, ctx.versions, _model_bucket(ctx), frozenset(current))
    _apply(ctx, base_manifests(ctx.config) + deployments + [pod_monitor(ctx.config)] + scalers)
    # 新建的：创建时带着初始副本数，马上交出去
    _hand_over_replicas(
        ctx,
        ns,
        {d["metadata"]["name"]: d["spec"]["replicas"] for d in deployments if "replicas" in d["spec"]},
    )
    pods = _wait_for_serving(ctx, ns)
    if pods:
        _capture_compile_caches(ctx, ns, pods, _expected_cache_keys(deployments))


def _wait_for_serving(ctx: InstallContext, ns: str) -> List[Mapping[str, Any]]:
    """等到有副本就绪，返回当时的 Pod 列表；超时返回空列表（只提示，不让部署失败）。"""
    started, last = _now(), ""
    while _now() - started < SERVING_TIMEOUT:
        pods = _get_json(ctx, "-n", ns, "get", "pods", "-l", "app=qwen-vl").get("items", [])
        summary = (
            ", ".join(sorted(f"{p['metadata']['name']}={_pod_summary(p)}" for p in pods)) or "（还没有 Pod）"
        )
        if summary != last:
            ctx.console.status("info", f"{int(_now() - started)}s：{summary}")
            last = summary
        if any(_pod_summary(p) == "Ready" for p in pods):
            ctx.console.status("ok", f"推理服务已就绪（{int(_now() - started)}s）")
            return pods
        _sleep(POLL_INTERVAL)
    ctx.console.status(
        "warn",
        f"{SERVING_TIMEOUT // 60} 分钟内没有就绪的 vLLM 副本。主力机型没货时 KEDA 会自动扩出回退机型，"
        "这里还没有就绪，通常是回退机型也没有容量，或者 GPU 配额不够",
    )
    ctx.console.line(f"      查看原因：kubectl -n {ns} get events --sort-by=.lastTimestamp | tail")
    ctx.console.line(f"      查看扩缩容：kubectl -n {ns} get scaledobject,hpa")
    return []


def _expected_cache_keys(deployments: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    """按配置刚生成的 Deployment 里，每个 profile 的缓存 key。"""
    return {
        d["metadata"]["labels"]["qwenctl/gpu-profile"]: d["spec"]["template"]["metadata"]["annotations"][
            compile_cache.ANNOTATION
        ]
        for d in deployments
    }


def _profile_of(pod_name: str, profiles: Iterable[str]) -> Optional[str]:
    """按 Pod 名（vllm-<profile>-<ReplicaSet 哈希>-<后缀>，不可改）认 profile，不看可改的 label。"""
    return next(
        (p for p in profiles if re.fullmatch(f"vllm-{re.escape(p)}-[a-z0-9]+-[a-z0-9]+", pod_name)), None
    )


def _capture_compile_caches(
    ctx: InstallContext, ns: str, pods: Sequence[Mapping[str, Any]], expected: Mapping[str, str]
) -> None:
    """每个 profile 取一个就绪的 Pod，把它冷编译出来的缓存存到 S3（回退 profile 有副本就绪时也一并保存）。

    缓存里有生成的代码，这里是写入端的闸门（vLLM 的角色只读，见 compile_cache.py）：
    key 只用按配置算出的值；Pod 上的 annotation 必须和它一致（否则是旧 Pod 或者被改过），不一致就不存。"""
    chosen: Dict[str, Mapping[str, Any]] = {}
    for pod in pods:
        profile = _profile_of(pod["metadata"]["name"], expected)
        if profile and profile not in chosen and _pod_summary(pod) == "Ready":
            chosen[profile] = pod
    for profile, pod in chosen.items():
        name, key = pod["metadata"]["name"], expected[profile]
        if (pod["metadata"].get("annotations") or {}).get(compile_cache.ANNOTATION) != key:
            ctx.console.status("info", f"{name} 不是按当前配置启动的副本，这次不保存编译缓存")
        elif _cache_missing(ctx, _model_bucket(ctx), key, profile):
            _upload_compile_cache(ctx, ns, name, profile, key)


def _cache_missing(ctx: InstallContext, bucket: str, key: str, profile: str) -> bool:
    exists, detail = _s3_exists(ctx, bucket, key)
    if exists:
        ctx.console.status("ok", f"{profile}：编译缓存已经在 S3 上，新副本冷启动会跳过编译")
    elif detail:  # 例如没有 s3:ListBucket 时的 403：照常保存，条件写入保证不会覆盖已有的缓存
        ctx.console.status("info", f"{profile}：查不清楚编译缓存是否已存在（{detail}），用条件写入保存")
    return not exists


def _upload_compile_cache(ctx: InstallContext, ns: str, pod: str, profile: str, key: str) -> None:
    """打包（流式写到私有临时文件）→ 检查压缩包 → 条件写入。失败只提示，不影响部署。"""
    path = os.path.join(ctx.paths.root, "compile-cache.tar.gz")
    try:
        size = ctx.runner.run_to_file(
            _kubectl(ctx, "-n", ns, *compile_cache.capture_command(pod)),
            path,
            compile_cache.MAX_ARCHIVE_BYTES,
            compile_cache.CAPTURE_OK_RETURNCODES,
        )
        problem = compile_cache.check_archive(path)
        if problem:
            ctx.console.status("warn", f"{profile}：编译缓存没有通过检查（{problem}），没有保存")
        elif _put_if_absent(ctx, _model_bucket(ctx), key, path):
            ctx.console.status(
                "ok", f"{profile}：已把编译缓存（{size / 1e6:.0f} MB）存到 S3，之后的冷启动省掉约 90 s 的编译"
            )
        else:
            ctx.console.status("ok", f"{profile}：编译缓存刚被别的部署进程保存过，保留已有的那份")
    except (CommandError, OSError) as exc:
        ctx.console.status(
            "warn",
            f"{profile}：保存编译缓存没有成功（{_reason(exc)}）。不影响服务，之后的冷启动要多花约 90 s 编译",
        )
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


def _reason(exc: BaseException) -> str:
    if isinstance(exc, CommandError):
        return exc.output.strip()[:200] or str(exc)
    return str(exc)


def _put_if_absent(ctx: InstallContext, bucket: str, key: str, path: str) -> bool:
    """只在对象不存在时写入（S3 条件写）。返回是否由我们写入；别人先写了（412）返回 False。"""
    try:
        ctx.runner.run(
            [
                "aws",
                "s3api",
                "put-object",
                "--bucket",
                bucket,
                "--key",
                key,
                "--body",
                path,
                "--if-none-match",
                "*",
                "--checksum-algorithm",
                "SHA256",
                "--region",
                ctx.region,
            ],
            capture=True,
        )
    except CommandError as exc:
        if "PreconditionFailed" in exc.output or "(412)" in exc.output:
            return False
        raise
    return True


PHASES: Tuple[Phase, ...] = (
    ("cdk-deps", "准备 CDK（依赖和编译）", _cdk_deps),
    ("cdk-bootstrap", "CDK bootstrap（只有首次需要）", _cdk_bootstrap),
    ("cdk-deploy", "部署 AWS 资源：网络、EKS（CloudFormation，首次约 15 分钟）", _cdk_deploy),
    ("kubeconfig", "配置 kubectl", _kubeconfig),
    ("karpenter", "安装 Karpenter", _karpenter),
    ("nodepools", "创建节点池：通用 CPU（非 GPU 业务）+ GPU", _nodepools),
    ("verify", "检查集群状态", _verify),
    ("metrics", "安装集群内 Prometheus（给 KEDA 做扩缩容查询）", _metrics),
    ("keda", "安装 KEDA", _keda),
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
