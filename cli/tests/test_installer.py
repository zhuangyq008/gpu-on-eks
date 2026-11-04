from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import stat
from typing import Dict, List, Optional, Sequence

import pytest

from qwenctl.installer import (
    PHASES,
    InstallContext,
    InstallError,
    eks_outputs,
    karpenter_values,
    load_state,
    pull_chart,
    run_install,
)
from qwenctl.paths import DeploymentPaths
from qwenctl.runner import CommandError, CommandResult
from qwenctl.serving import vllm_deployments
from qwenctl.versions import load_versions

from .conftest import make_console
from .sample_config import generate

EKS_OUTPUTS = {
    "ClusterName": "qwen-vl",
    "ClusterSecurityGroupId": "sg-0123",
    "KarpenterNodeRoleName": "node-role",
    "KarpenterInterruptionQueueName": "queue-1",
    "PrivateSubnetIds": "subnet-0a,subnet-0b",
    "VpcId": "vpc-0abc",
    "WebAclArn": "arn:aws:wafv2:ap-southeast-5:123456789012:regional/webacl/qwen-vl-web/abc",
}
CHART_BYTES = {
    "aws-load-balancer-controller": b"fake-lbc-chart",
    "karpenter-crd": b"fake-crd-chart",
    "karpenter": b"fake-karpenter-chart",
    "kube-prometheus-stack": b"fake-kps-chart",
    "keda": b"fake-keda-chart",
}


def _versions(tampered: bool = False) -> dict:
    versions = copy.deepcopy(load_versions())
    for chart, content in CHART_BYTES.items():
        digest = hashlib.sha256(content + (b"x" if tampered else b"")).hexdigest()
        versions["charts"][chart]["sha256"] = digest
    return versions


SERVING_OUTPUTS = {"ModelBucketName": "qwen-vl-serving-modelbucket-abc"}
READY_POD = {
    "metadata": {"name": "vllm-g7e-1gpu-0"},
    "status": {"phase": "Running", "containerStatuses": [{"ready": True, "state": {"running": {}}}]},
}


def _cache_archive(members=None) -> bytes:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in (members or {"torch_compile_cache/abc/model.py": b"compiled"}).items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeRunner:
    def __init__(
        self,
        fail_on: Optional[str] = None,
        cfn_error: bool = False,
        job_status: Optional[dict] = None,
        pods: Optional[list] = None,
        deployments: Optional[Dict[str, int]] = None,
        get_error: bool = False,
        synced: bool = False,
        cached: bool = False,
        capture_bytes: Optional[bytes] = None,
        capture_fails: bool = False,
        put_error: Optional[str] = None,
    ) -> None:
        self.calls: List[dict] = []
        self.fail_on = fail_on
        self.cfn_error = cfn_error
        self.job_status = job_status if job_status is not None else {"succeeded": 1}
        self.pods = pods if pods is not None else [READY_POD]
        self.deployments = deployments or {}
        self.get_error = get_error
        self.head_error = "An error occurred (404) when calling the HeadObject operation: Not Found"
        self.synced = synced
        self.cached = cached
        self.capture_bytes = capture_bytes if capture_bytes is not None else _cache_archive()
        self.capture_fails = capture_fails
        self.put_error = put_error

    def run(
        self, args: Sequence[str], *, cwd=None, input_text=None, capture=False, env=None
    ) -> CommandResult:
        self.calls.append(
            {"args": list(args), "cwd": cwd, "input": input_text, "capture": capture, "env": env}
        )
        if self.fail_on and self.fail_on in " ".join(args):
            raise CommandError(args, 1, "boom")
        if "deploy" in args and "--outputs-file" in args:
            with open(args[args.index("--outputs-file") + 1], "w", encoding="utf-8") as handle:
                json.dump({"qwen-vl-network": {}, "qwen-vl-eks": EKS_OUTPUTS}, handle)
        if "pull" in args:
            chart = args[args.index("pull") + 1].rsplit("/", 1)[-1]  # oci://repo/chart 或 chart --repo URL
            version = args[args.index("--version") + 1]
            destination = args[args.index("--destination") + 1]
            with open(os.path.join(destination, f"{chart}-{version}.tgz"), "wb") as handle:
                handle.write(CHART_BYTES[chart])
        if "describe-stacks" in args:
            if self.cfn_error:
                raise CommandError(args, 255, "AccessDenied")
            stack = args[args.index("--stack-name") + 1]
            outputs = SERVING_OUTPUTS if stack.endswith("-serving") else EKS_OUTPUTS
            return CommandResult(
                0, json.dumps([{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()])
            )
        if "put-object" in args and self.put_error:
            raise CommandError(args, 254, self.put_error)
        if "head-object" in args:
            key = args[args.index("--key") + 1]
            exists = self.synced if key.endswith("_COMPLETE") else self.cached
            if not exists:
                raise CommandError(args, 254, self.head_error)
        if "get" in args and "job" in args:
            return CommandResult(0, json.dumps({"status": self.job_status}))
        if "get" in args and "pods" in args:
            return CommandResult(0, json.dumps({"items": self.pods}))
        if "get" in args and "deployments" in args:
            if self.get_error:
                raise CommandError(args, 1, "Unauthorized")
            items = [{"metadata": {"name": n}, "spec": {"replicas": r}} for n, r in self.deployments.items()]
            return CommandResult(0, json.dumps({"items": items}))
        if "logs" in args:
            return CommandResult(0, "Traceback: boom")
        return CommandResult(0, "")

    def run_to_file(self, args: Sequence[str], path: str, max_bytes: int, ok_returncodes=(0,)) -> int:
        self.capture_ok_returncodes = tuple(ok_returncodes)
        self.calls.append({"args": list(args), "cwd": None, "input": None, "capture": True, "env": None})
        if self.capture_fails:
            raise CommandError(args, 1, "container not found")
        if len(self.capture_bytes) > max_bytes:
            raise CommandError(args, 1, f"输出超过 {max_bytes} 字节，已停止")
        with open(path, "wb") as handle:
            handle.write(self.capture_bytes)
        return len(self.capture_bytes)

    def commands(self) -> List[str]:
        return [" ".join(c["args"]) for c in self.calls]

    def helm_installs(self) -> List[str]:
        return [c for c in self.commands() if "upgrade --install" in c]


def _infra(tmp_path, with_modules=False, with_typescript=False, with_dist=True):
    infra = tmp_path / "infra"
    infra.mkdir(exist_ok=True)
    if with_modules:
        (infra / "node_modules" / "aws-cdk-lib").mkdir(parents=True, exist_ok=True)
    if with_typescript:
        (infra / "node_modules" / "typescript").mkdir(parents=True, exist_ok=True)
    if with_dist:
        (infra / "dist" / "bin").mkdir(parents=True, exist_ok=True)
        (infra / "dist" / "bin" / "app.js").write_text("", encoding="utf-8")
    return str(infra)


def _ctx(tmp_path, runner, infra=None, bootstrapped=False, config=None, versions=None):
    paths = DeploymentPaths(str(tmp_path / "home" / ".qwenctl" / "qwen-vl")).ensure()
    con, out = make_console()
    ctx = InstallContext(
        name="qwen-vl",
        region="ap-southeast-5",
        account="123456789012",
        config=config or json.loads(generate("new")),
        config_path=str(tmp_path / "config.json"),
        paths=paths,
        versions=versions or _versions(),
        tools={"helm": "/bin/helm", "kubectl": "/bin/kubectl"},
        runner=runner,
        console=con,
        infra_dir=infra or _infra(tmp_path),
        cdk_bootstrapped=bootstrapped,
    )
    return ctx, out


def test_full_install_runs_every_phase_in_order(tmp_path):
    runner = FakeRunner()
    ctx, out = _ctx(tmp_path, runner)
    state = run_install(ctx, resume=False)
    assert state["completed"] == [key for key, _, _ in PHASES]
    cmds = runner.commands()
    assert cmds[0] == "npm ci --omit=dev --ignore-scripts --no-audit --no-fund"
    bootstrap = runner.calls[1]["args"]
    assert bootstrap[1:3] == ["bootstrap", "aws://123456789012/ap-southeast-5"]
    # cdk 在 infra/ 下会先执行 app，app 必须拿到部署配置（真实部署时踩到过）
    assert f"configFile={ctx.config_path}" in bootstrap
    deploy = runner.calls[2]["args"]
    assert deploy[1:4] == ["deploy", "--all", "--require-approval"]
    assert f"configFile={ctx.config_path}" in deploy
    assert cmds[3].startswith("aws eks update-kubeconfig --name qwen-vl --region ap-southeast-5")
    pull = next(c for c in runner.calls if "pull" in c["args"])
    assert pull["args"][2] == "oci://public.ecr.aws/karpenter/karpenter-crd"
    assert pull["env"]["DOCKER_CONFIG"].endswith("helm-registry")
    installs = runner.helm_installs()
    assert installs[0].startswith("/bin/helm upgrade --install karpenter-crd ")
    assert installs[0].split()[4].endswith("charts/karpenter-crd-1.15.1.tgz")
    assert "--skip-crds" in installs[1] and "--wait" in installs[1]
    apply = next(c for c in runner.calls if "apply" in c["args"] and "NodePool" in (c["input"] or ""))
    assert apply["args"][-4:] == ["--field-manager", "qwenctl", "-f", "-"]
    items = json.loads(apply["input"])["items"]
    assert [i["metadata"]["name"] for i in items if i["kind"] == "NodePool"] == [
        "general",
        "gpu-g7e-1gpu",
        "gpu-g6-tp2",
    ]
    assert any("ec2nodeclass/general" in c for c in cmds)
    assert out.getvalue().count("✓ 完成") == len(PHASES)


def test_private_files_are_owner_only(tmp_path):
    ctx, _ = _ctx(tmp_path, FakeRunner())
    run_install(ctx, resume=False)
    for path in (ctx.paths.state, os.path.join(ctx.paths.root, "karpenter-values.json")):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    for directory in (ctx.paths.root, os.path.dirname(ctx.paths.root)):
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700


def test_tampered_chart_is_rejected_and_removed(tmp_path):
    ctx, _ = _ctx(tmp_path, FakeRunner(), versions=_versions(tampered=True))
    with pytest.raises(InstallError, match="chart karpenter-crd 校验失败"):
        pull_chart(ctx, "karpenter-crd")
    assert os.listdir(os.path.join(os.path.dirname(ctx.paths.root), "charts")) == []


def test_verified_chart_is_reused_without_pulling(tmp_path):
    runner = FakeRunner()
    ctx, _ = _ctx(tmp_path, runner)
    first = pull_chart(ctx, "karpenter")
    assert pull_chart(ctx, "karpenter") == first
    assert sum("pull" in c["args"] for c in runner.calls) == 1


def test_bootstrapped_account_skips_bootstrap_and_dev_env_rebuilds(tmp_path):
    runner = FakeRunner()
    infra = _infra(tmp_path, with_modules=True, with_typescript=True)
    ctx, out = _ctx(tmp_path, runner, infra=infra, bootstrapped=True)
    run_install(ctx, resume=False)
    assert runner.commands()[0] == "npm run build"
    assert not any(c["args"][1:2] == ["bootstrap"] for c in runner.calls)
    assert "CDK 已经 bootstrap，跳过" in out.getvalue()


def test_customer_package_without_dist_fails_clearly(tmp_path):
    infra = _infra(tmp_path, with_modules=True, with_dist=False)
    ctx, _ = _ctx(tmp_path, FakeRunner(), infra=infra)
    with pytest.raises(InstallError, match="缺少编译好的 infra/dist"):
        run_install(ctx, resume=False)


def test_failure_records_progress_and_resume_continues(tmp_path):
    ctx, _ = _ctx(tmp_path, FakeRunner(fail_on="upgrade --install karpenter-crd"))
    with pytest.raises(InstallError) as info:
        run_install(ctx, resume=False)
    assert info.value.phase == "karpenter"
    assert load_state(ctx.paths.state)["completed"] == [
        "cdk-deps",
        "cdk-bootstrap",
        "cdk-deploy",
        "kubeconfig",
    ]
    runner = FakeRunner()
    ctx2, out = _ctx(tmp_path, runner)
    run_install(ctx2, resume=True)
    assert runner.helm_installs()[0].startswith("/bin/helm upgrade --install karpenter-crd")
    assert not any("deploy" in c["args"] for c in runner.calls)
    assert out.getvalue().count("上次已完成，跳过") == 4


def test_qualifier_is_passed_to_bootstrap_and_deploy(tmp_path):
    config = json.loads(generate("new"))
    config["advanced"]["cdk"]["bootstrap_qualifier"] = "custqual"
    runner = FakeRunner()
    ctx, _ = _ctx(tmp_path, runner, config=config)
    run_install(ctx, resume=False)
    cmds = runner.commands()
    assert "--qualifier custqual" in cmds[1]
    assert "@aws-cdk/core:bootstrapQualifier=custqual" in cmds[2]


def test_outputs_come_from_cloudformation_first(tmp_path):
    runner = FakeRunner()
    ctx, _ = _ctx(tmp_path, runner)
    with open(ctx.paths.outputs, "w", encoding="utf-8") as handle:
        json.dump({"qwen-vl-eks": {"KarpenterInterruptionQueueName": "stale"}}, handle)
    assert eks_outputs(ctx)["KarpenterInterruptionQueueName"] == "queue-1"


def test_outputs_fall_back_to_local_file_when_cloudformation_fails(tmp_path):
    ctx, _ = _ctx(tmp_path, FakeRunner(cfn_error=True))
    with pytest.raises(CommandError):
        eks_outputs(ctx)
    with open(ctx.paths.outputs, "w", encoding="utf-8") as handle:
        json.dump({"qwen-vl-eks": EKS_OUTPUTS}, handle)
    assert eks_outputs(ctx)["ClusterSecurityGroupId"] == "sg-0123"


def test_karpenter_values_pin_controller_to_system_nodes_and_set_region():
    values = karpenter_values("qwen-vl", "ap-southeast-5", "queue-1")
    assert values["settings"] == {"clusterName": "qwen-vl", "interruptionQueue": "queue-1"}
    assert values["nodeSelector"] == {"qwenctl/node-role": "system"}
    assert {"key": "CriticalAddonsOnly", "operator": "Exists"} in values["tolerations"]
    assert {"name": "AWS_REGION", "value": "ap-southeast-5"} in values["controller"]["env"]


def test_corrupt_state_is_ignored(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{oops", encoding="utf-8")
    assert load_state(str(path)) == {"completed": []}
    assert load_state(str(tmp_path / "missing.json")) == {"completed": []}


def test_chart_cache_and_registry_config_are_owner_only(tmp_path):
    ctx, _ = _ctx(tmp_path, FakeRunner())
    charts = os.path.join(os.path.dirname(ctx.paths.root), "charts")
    os.makedirs(charts, mode=0o755)
    os.chmod(charts, 0o755)
    pull_chart(ctx, "karpenter")
    assert stat.S_IMODE(os.stat(charts).st_mode) == 0o700
    registry = os.path.join(ctx.paths.root, "helm-registry", "config.json")
    assert stat.S_IMODE(os.stat(registry).st_mode) == 0o600


def test_write_private_replaces_stale_tmp_with_loose_permissions(tmp_path):
    from qwenctl.paths import write_private

    target = tmp_path / "state.json"
    stale = tmp_path / "state.json.tmp"
    stale.write_text("old", encoding="utf-8")
    os.chmod(stale, 0o666)
    write_private(str(target), "{}")
    assert target.read_text(encoding="utf-8") == "{}"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert not stale.exists()


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch):
    """把等待循环的时钟换成假的：每次 sleep 前进对应秒数，测试不真的等待。"""
    from qwenctl import installer

    clock = {"t": 0.0}
    monkeypatch.setattr(installer, "_now", lambda: clock["t"])
    monkeypatch.setattr(installer, "_sleep", lambda seconds: clock.__setitem__("t", clock["t"] + seconds))
    return clock


def _items(payload):
    data = json.loads(payload)
    return data["items"] if data.get("kind") == "List" else [data]


def _applied(runner, kind, manager="qwenctl"):
    return [
        i
        for c in runner.calls
        if c["input"] and c["args"][c["args"].index("--field-manager") + 1] == manager
        for i in _items(c["input"])
        if i["kind"] == kind
    ]


def _manager_order(runner):
    """每次 apply 的 (field manager, [Deployment 名]) ，按调用顺序。"""
    order = []
    for c in runner.calls:
        if c["input"] and "--field-manager" in c["args"]:
            names = [i["metadata"]["name"] for i in _items(c["input"]) if i["kind"] == "Deployment"]
            if names:
                order.append((c["args"][c["args"].index("--field-manager") + 1], names))
    return order


def test_model_sync_and_serving_phases(tmp_path):
    runner = FakeRunner()
    ctx, out = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    (job,) = _applied(runner, "Job")
    env = {e["name"]: e["value"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["S3_BUCKET"] == "qwen-vl-serving-modelbucket-abc"
    assert env["S3_PREFIX"] == "models/qwen3.5-35b-a3b-fp8/9d1823d2dee6/"
    deployments = {d["metadata"]["name"]: d["spec"]["replicas"] for d in _applied(runner, "Deployment")}
    assert deployments == {"vllm-g7e-1gpu": 2, "vllm-g6-tp2": 0}
    text = out.getvalue()
    assert "权重已同步到 s3://qwen-vl-serving-modelbucket-abc/models/" in text
    assert "推理服务已就绪" in text


def test_model_sync_failure_shows_job_logs(tmp_path):
    failed = {"conditions": [{"type": "Failed", "status": "True", "message": "BackoffLimitExceeded"}]}
    ctx, _ = _ctx(tmp_path, FakeRunner(job_status=failed))
    with pytest.raises(InstallError) as info:
        run_install(ctx, resume=False)
    assert info.value.phase == "model-sync"
    assert "BackoffLimitExceeded" in str(info.value) and "Traceback: boom" in str(info.value)


def test_model_sync_timeout(tmp_path):
    ctx, _ = _ctx(tmp_path, FakeRunner(job_status={"active": 1}))
    with pytest.raises(InstallError, match="权重同步超过 60 分钟"):
        run_install(ctx, resume=False)


def test_serving_without_capacity_warns_instead_of_hanging(tmp_path):
    pending = {"metadata": {"name": "vllm-g7e-1gpu-0"}, "status": {"phase": "Pending"}}
    ctx, out = _ctx(tmp_path, FakeRunner(pods=[pending]))
    state = run_install(ctx, resume=False)
    assert state["completed"][-1] == "serving"
    text = out.getvalue()
    assert "vllm-g7e-1gpu-0=Pending" in text
    assert "没有就绪的 vLLM 副本" in text and "KEDA 会自动扩出回退机型" in text
    assert "get scaledobject,hpa" in text


def test_pod_summary_states():
    from qwenctl.installer import _pod_summary

    waiting = {"status": {"containerStatuses": [{"state": {"waiting": {"reason": "ImagePullBackOff"}}}]}}
    loading = {"status": {"containerStatuses": [{"ready": False, "state": {"running": {}}}]}}
    assert _pod_summary(waiting) == "ImagePullBackOff"
    assert _pod_summary(loading) == "Running（加载模型中）"
    assert _pod_summary(READY_POD) == "Ready"
    assert _pod_summary({"status": {}}) == "Unknown"


def test_missing_bucket_output(tmp_path, monkeypatch):
    monkeypatch.setitem(SERVING_OUTPUTS, "ModelBucketName", "")
    ctx, _ = _ctx(tmp_path, FakeRunner())
    with pytest.raises(InstallError, match="ModelBucketName"):
        run_install(ctx, resume=False)


def test_platform_charts_go_to_their_namespaces_on_system_nodes(tmp_path):
    runner = FakeRunner()
    ctx, _ = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    installs = {c["args"][3]: c["args"] for c in runner.calls if "upgrade" in c["args"]}
    assert installs["kps"][installs["kps"].index("--namespace") + 1] == "monitoring"
    assert installs["keda"][installs["keda"].index("--namespace") + 1] == "keda"
    assert "--create-namespace" in installs["keda"]
    pulls = [c["args"] for c in runner.calls if "pull" in c["args"]]
    assert ["keda", "--repo", "https://kedacore.github.io/charts"] == next(p for p in pulls if "keda" in p)[
        2:5
    ]
    with open(os.path.join(ctx.paths.root, "prometheus-values.json"), encoding="utf-8") as handle:
        values = json.load(handle)
    assert values["prometheus"]["prometheusSpec"]["nodeSelector"] == {"qwenctl/node-role": "system"}
    assert values["grafana"] == {"enabled": False}


def test_serving_applies_autoscaling_and_leaves_existing_replicas_to_keda(tmp_path):
    runner = FakeRunner(deployments={"vllm-g7e-1gpu": 3})
    ctx, _ = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    deployments = {d["metadata"]["name"]: d["spec"] for d in _applied(runner, "Deployment")}
    assert "replicas" not in deployments["vllm-g7e-1gpu"]  # 已存在：副本数归 KEDA
    assert deployments["vllm-g6-tp2"]["replicas"] == 0  # 首次创建：回退从 0 开始
    assert [o["metadata"]["name"] for o in _applied(runner, "ScaledObject")] == [
        "vllm-g7e-1gpu",
        "vllm-g6-tp2",
    ]
    assert len(_applied(runner, "PodMonitor")) == 1


def test_model_sync_skips_job_when_marker_exists(tmp_path):
    runner = FakeRunner(synced=True)
    ctx, out = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    assert _applied(runner, "Job") == []
    head = next(c["args"] for c in runner.calls if "head-object" in c["args"])
    assert head[head.index("--key") + 1] == "models/qwen3.5-35b-a3b-fp8/9d1823d2dee6/_COMPLETE"
    assert "已经同步并校验过，跳过" in out.getvalue()


def test_keda_is_installed_only_after_prometheus_is_available(tmp_path):
    runner = FakeRunner()
    ctx, _ = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    commands = runner.commands()
    wait = next(
        i for i, c in enumerate(commands) if "wait prometheus.monitoring.coreos.com/kps-prometheus" in c
    )
    assert "-n monitoring --for=condition=Available" in commands[wait]
    kps = next(i for i, c in enumerate(commands) if "upgrade --install kps" in c)
    keda = next(i for i, c in enumerate(commands) if "upgrade --install keda" in c)
    assert kps < wait < keda


def test_replicas_are_handed_to_a_separate_field_manager(tmp_path):
    """qwenctl 的 apply 省略 replicas 时，replicas 不能只归 qwenctl 所有，否则 SSA 会把它重置为 1。"""
    runner = FakeRunner(deployments={"vllm-g7e-1gpu": 3})
    ctx, _ = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    assert _manager_order(runner) == [
        ("qwenctl-replicas", ["vllm-g7e-1gpu"]),  # 已有的：先交出（值不变），再省略 replicas
        ("qwenctl", ["vllm-g7e-1gpu", "vllm-g6-tp2"]),
        ("qwenctl-replicas", ["vllm-g6-tp2"]),  # 新建的：创建后马上交出
    ]
    # 部分对象不能包在 v1 List 里：kubectl 会在客户端按完整 Deployment 校验而拒绝（真实集群上踩过）
    payloads = [
        json.loads(c["input"]) for c in runner.calls if c["input"] and "qwenctl-replicas" in c["args"]
    ]
    assert payloads and all(p["kind"] == "Deployment" for p in payloads)
    handover = {d["metadata"]["name"]: d for d in _applied(runner, "Deployment", "qwenctl-replicas")}
    assert handover["vllm-g7e-1gpu"]["spec"] == {"replicas": 3}
    assert handover["vllm-g6-tp2"]["spec"] == {"replicas": 0}


def test_unreadable_deployments_fail_instead_of_resetting_replicas(tmp_path):
    runner = FakeRunner(get_error=True)
    ctx, _ = _ctx(tmp_path, runner)
    with pytest.raises(InstallError) as err:
        run_install(ctx, resume=False)
    assert err.value.phase == "serving"
    assert _applied(runner, "Deployment") == []


def test_unexpected_marker_error_warns_and_runs_the_job(tmp_path):
    runner = FakeRunner()
    runner.head_error = "An error occurred (403) when calling the HeadObject operation: Forbidden"
    ctx, out = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    assert len(_applied(runner, "Job")) == 1
    assert "没法确认" in out.getvalue() and "403" in out.getvalue()


def test_missing_marker_runs_the_job_quietly(tmp_path):
    runner = FakeRunner()
    ctx, out = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    assert len(_applied(runner, "Job")) == 1
    assert "没法确认" not in out.getvalue()


def test_platform_namespaces_get_pod_security_before_helm(tmp_path):
    runner = FakeRunner()
    ctx, _ = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    labels = {n["metadata"]["name"]: n["metadata"]["labels"] for n in _applied(runner, "Namespace")}
    for ns in ("monitoring", "keda"):
        assert labels[ns]["pod-security.kubernetes.io/enforce"] == "baseline"

    def first_call(predicate):
        return next(i for i, c in enumerate(runner.calls) if predicate(c))

    def creates_namespace(name):
        return lambda c: bool(c["input"]) and any(
            i["kind"] == "Namespace" and i["metadata"]["name"] == name for i in _items(c["input"])
        )

    def helm_installs(release):
        return lambda c: "upgrade" in c["args"] and release in c["args"]

    assert first_call(creates_namespace("monitoring")) < first_call(helm_installs("kps"))
    assert first_call(creates_namespace("keda")) < first_call(helm_installs("keda"))


def test_fresh_install_creates_with_initial_replicas_then_hands_them_over(tmp_path):
    runner = FakeRunner()
    ctx, _ = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    assert _manager_order(runner) == [
        ("qwenctl", ["vllm-g7e-1gpu", "vllm-g6-tp2"]),
        ("qwenctl-replicas", ["vllm-g6-tp2"]),
        ("qwenctl-replicas", ["vllm-g7e-1gpu"]),
    ]
    handover = {d["metadata"]["name"]: d["spec"] for d in _applied(runner, "Deployment", "qwenctl-replicas")}
    assert handover == {
        "vllm-g7e-1gpu": {"replicas": ctx.config["gpu"]["minReplicas"]},
        "vllm-g6-tp2": {"replicas": 0},
    }


def test_bad_profile_name_in_saved_config_is_an_install_error(tmp_path):
    runner = FakeRunner()
    ctx, _ = _ctx(tmp_path, runner)
    bad = [{**ctx.config["gpu"]["profiles"][0], "name": "g7e.*"}]
    ctx = dataclasses.replace(ctx, config={**ctx.config, "gpu": {**ctx.config["gpu"], "profiles": bad}})
    with pytest.raises(InstallError, match="PromQL") as err:
        run_install(ctx, resume=False)
    assert err.value.phase == "serving"


def _expected_key(tmp_path, profile="g7e-1gpu"):
    """按配置算出的 key（和 installer 用来校验的是同一份）。"""
    ctx, _ = _ctx(tmp_path / "probe", FakeRunner())
    deployments = vllm_deployments(ctx.config, ctx.versions, SERVING_OUTPUTS["ModelBucketName"])
    return next(
        d["spec"]["template"]["metadata"]["annotations"]["qwenctl/compile-cache"]
        for d in deployments
        if d["metadata"]["labels"]["qwenctl/gpu-profile"] == profile
    )


def _annotated_pod(key, profile="g7e-1gpu", name=None):
    metadata = {
        "name": name or f"vllm-{profile}-5d8f7c9b6-x2k4q",
        "labels": {"app": "qwen-vl", "qwenctl/gpu-profile": profile},
        "annotations": {"qwenctl/compile-cache": key},
    }
    return {**READY_POD, "metadata": metadata}


def _uploads(runner):
    return [c["args"] for c in runner.calls if "put-object" in c["args"]]


def test_fresh_compile_cache_is_uploaded_with_deployer_credentials(tmp_path):
    key = _expected_key(tmp_path)
    runner = FakeRunner(pods=[_annotated_pod(key)])
    ctx, out = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    exec_call = next(c["args"] for c in runner.calls if "exec" in c["args"])
    assert exec_call[exec_call.index("exec") + 1] == "vllm-g7e-1gpu-5d8f7c9b6-x2k4q" and "tar" in exec_call
    (upload,) = _uploads(runner)
    assert upload[upload.index("--bucket") + 1] == SERVING_OUTPUTS["ModelBucketName"]
    assert upload[upload.index("--key") + 1] == key
    # 条件写入，不覆盖已有缓存；带校验和
    assert upload[upload.index("--if-none-match") + 1] == "*"
    assert upload[upload.index("--checksum-algorithm") + 1] == "SHA256"
    assert "已把编译缓存" in out.getvalue()
    assert not os.path.exists(os.path.join(ctx.paths.root, "compile-cache.tar.gz"))  # 临时文件删掉


def test_existing_compile_cache_is_not_overwritten(tmp_path):
    runner = FakeRunner(pods=[_annotated_pod(_expected_key(tmp_path))], cached=True)
    ctx, out = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    assert _uploads(runner) == [] and not any("exec" in c["args"] for c in runner.calls)
    assert "编译缓存已经在 S3 上" in out.getvalue()


def test_compile_cache_failure_only_warns(tmp_path):
    runner = FakeRunner(pods=[_annotated_pod(_expected_key(tmp_path))], capture_fails=True)
    ctx, out = _ctx(tmp_path, runner)
    state = run_install(ctx, resume=False)
    assert state["completed"][-1] == "serving"
    assert "保存编译缓存没有成功" in out.getvalue() and _uploads(runner) == []


def test_oversized_compile_cache_is_not_uploaded(tmp_path, monkeypatch):
    from qwenctl import compile_cache

    monkeypatch.setattr(compile_cache, "MAX_ARCHIVE_BYTES", 4)
    runner = FakeRunner(pods=[_annotated_pod(_expected_key(tmp_path))])
    ctx, out = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    assert _uploads(runner) == [] and "超过 4 字节" in out.getvalue()


@pytest.mark.parametrize(
    "pod",
    [
        # 篡改的 annotation：想借部署者的凭证覆盖模型权重
        _annotated_pod("models/qwen3.5-35b-a3b-fp8/9d1823d2dee6/model-00001-of-00014.safetensors"),
        # 还没滚动更新的旧 Pod（key 是旧配置算的）
        _annotated_pod("compile-cache/g7e-1gpu/0000000000000000/vllm-cache.tar.gz"),
        # 别的 profile 的 key 贴到这个 profile 的 Pod 上
        _annotated_pod("compile-cache/g6-tp2/0000000000000000/vllm-cache.tar.gz"),
        # 改了 label 想冒充别的 profile：profile 按 Pod 名（不可改）认
        _annotated_pod("ignored", profile="g6-tp2", name="vllm-g7e-1gpu-5d8f7c9b6-x2k4q"),
        # Pod 名对不上任何 profile：不是候选
        _annotated_pod("k", name="vllm-x"),
    ],
)
def test_capture_only_trusts_keys_computed_from_config(tmp_path, pod):
    runner = FakeRunner(pods=[pod])
    ctx, _ = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    assert _uploads(runner) == [] and not any("exec" in c["args"] for c in runner.calls)


def test_label_cannot_redirect_capture_to_another_profiles_key(tmp_path):
    """label 改成 g6-tp2、annotation 也换成 g6-tp2 的合法 key：Pod 名还是 g7e 的，不能存。"""
    g6_key = _expected_key(tmp_path, "g6-tp2")
    pod = _annotated_pod(g6_key, profile="g6-tp2", name="vllm-g7e-1gpu-5d8f7c9b6-x2k4q")
    runner = FakeRunner(pods=[pod])
    ctx, _ = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    assert _uploads(runner) == []


def test_unclear_existence_still_saves_with_conditional_write(tmp_path):
    """没有 s3:ListBucket 时 head-object 返回 403：照常保存，靠 If-None-Match 保证不覆盖已有缓存。"""
    runner = FakeRunner(pods=[_annotated_pod(_expected_key(tmp_path))])
    runner.head_error = "An error occurred (403) when calling the HeadObject operation: Forbidden"
    ctx, out = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    (upload,) = _uploads(runner)
    assert upload[upload.index("--if-none-match") + 1] == "*"
    assert "用条件写入保存" in out.getvalue()


def test_capture_tolerates_tar_reporting_changed_files(tmp_path):
    """vLLM 还在写缓存时 tar 退出 1，压缩包本身完整（交给 check_archive 判断）。"""
    runner = FakeRunner(pods=[_annotated_pod(_expected_key(tmp_path))])
    ctx, _ = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    assert runner.capture_ok_returncodes == (0, 1)


def test_concurrent_writer_wins_without_error(tmp_path):
    runner = FakeRunner(
        pods=[_annotated_pod(_expected_key(tmp_path))],
        put_error="An error occurred (PreconditionFailed) when calling the PutObject operation",
    )
    ctx, out = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    assert len(_uploads(runner)) == 1 and "刚被别的部署进程保存过" in out.getvalue()


@pytest.mark.parametrize(
    "members, reason",
    [
        ({"../escape.py": b"x"}, "指向缓存目录之外"),
        ({"/etc/cron.d/x": b"x"}, "指向缓存目录之外"),
    ],
)
def test_suspicious_archive_is_not_uploaded(tmp_path, members, reason):
    runner = FakeRunner(pods=[_annotated_pod(_expected_key(tmp_path))], capture_bytes=_cache_archive(members))
    ctx, out = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    assert _uploads(runner) == [] and reason in out.getvalue()


def test_every_profile_with_a_ready_pod_gets_its_cache_saved(tmp_path):
    pods = [_annotated_pod(_expected_key(tmp_path, p), profile=p) for p in ("g7e-1gpu", "g6-tp2")]
    runner = FakeRunner(pods=pods)
    ctx, _ = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    keys = sorted(u[u.index("--key") + 1] for u in _uploads(runner))
    assert keys == sorted(_expected_key(tmp_path, p) for p in ("g7e-1gpu", "g6-tp2"))


def _lbc_installs(runner):
    return [c for c in runner.commands() if "upgrade --install aws-load-balancer-controller" in c]


def test_addons_phase_creates_encrypted_default_storage_and_installs_lbc(tmp_path):
    runner = FakeRunner()
    ctx, out = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    (sc,) = _applied(runner, "StorageClass")
    assert sc["parameters"]["encrypted"] == "true" and sc["parameters"]["type"] == "gp3"
    (install,) = _lbc_installs(runner)
    assert "--namespace kube-system" in install
    with open(
        os.path.join(ctx.paths.root, "aws-load-balancer-controller-values.json"), encoding="utf-8"
    ) as handle:
        values = json.load(handle)
    assert values["vpcId"] == "vpc-0abc" and values["enableServiceMutatorWebhook"] is False
    assert "alb.ingress.kubernetes.io/wafv2-acl-arn: arn:aws:wafv2:" in out.getvalue()


def test_addons_phase_runs_after_keda_and_before_model_sync(tmp_path):
    runner = FakeRunner()
    ctx, _ = _ctx(tmp_path, runner)
    keys = [key for key, _title, _action in PHASES]
    assert keys.index("keda") < keys.index("addons") < keys.index("model-sync")
    state = run_install(ctx, resume=False)
    assert "addons" in state["completed"]


def test_lbc_is_skipped_when_web_ingress_is_not_selected(tmp_path):
    runner = FakeRunner()
    ctx, out = _ctx(tmp_path, runner)
    addons = {**ctx.config["addons"], "ingress": {"enabled": False}}
    ctx = dataclasses.replace(ctx, config={**ctx.config, "addons": addons})
    run_install(ctx, resume=False)
    assert _lbc_installs(runner) == []
    assert len(_applied(runner, "StorageClass")) == 1  # 默认存储照样创建
    assert "跳过 AWS Load Balancer Controller" in out.getvalue()


def test_storage_class_uses_customer_cmk(tmp_path):
    runner = FakeRunner()
    ctx, _ = _ctx(tmp_path, runner)
    cmk = "arn:aws:kms:ap-southeast-5:123456789012:key/11111111-2222-3333-4444-555555555555"
    advanced = {
        **ctx.config["advanced"],
        "compliance": {**ctx.config["advanced"]["compliance"], "kms_key_arn": cmk},
    }
    ctx = dataclasses.replace(ctx, config={**ctx.config, "advanced": advanced})
    run_install(ctx, resume=False)
    assert _applied(runner, "StorageClass")[0]["parameters"]["kmsKeyId"] == cmk


def test_missing_vpc_output_fails_the_addons_phase(tmp_path, monkeypatch):
    from qwenctl import installer

    outputs = {k: v for k, v in EKS_OUTPUTS.items() if k != "VpcId"}
    monkeypatch.setattr(installer, "eks_outputs", lambda _ctx: outputs)
    runner = FakeRunner()
    ctx, _ = _ctx(tmp_path, runner)
    with pytest.raises(InstallError) as err:
        run_install(ctx, resume=False)
    assert err.value.phase == "addons" and "VpcId" in str(err.value)


def test_admission_policies_are_applied_with_the_controller(tmp_path):
    runner = FakeRunner()
    ctx, _ = _ctx(tmp_path, runner)
    run_install(ctx, resume=False)
    policies = _applied(runner, "ValidatingAdmissionPolicy")
    assert [p["metadata"]["name"] for p in policies] == ["qwenctl-lb-service-class", "qwenctl-public-alb-waf"]
    # 按部署的 region 和推理命名空间生成
    assert "'qwen-vl'" in policies[0]["spec"]["validations"][1]["expression"]
    assert f":wafv2:{ctx.region}:{ctx.account}:" in policies[1]["spec"]["validations"][1]["expression"]
    assert any(
        "annotate storageclass gp2 storageclass.kubernetes.io/is-default-class=false" in c
        for c in runner.commands()
    )


def test_no_admission_policies_without_the_controller(tmp_path):
    runner = FakeRunner()
    ctx, _ = _ctx(tmp_path, runner)
    addons = {**ctx.config["addons"], "ingress": {"enabled": False}}
    ctx = dataclasses.replace(ctx, config={**ctx.config, "addons": addons})
    run_install(ctx, resume=False)
    assert _applied(runner, "ValidatingAdmissionPolicy") == []
    # 以前启用过、后来关掉：把护栏一起删掉
    delete = [c for c in runner.commands() if " delete validatingadmissionpolicybinding," in c]
    assert len(delete) == 1
    assert "qwenctl-lb-service-class qwenctl-public-alb-waf --ignore-not-found" in delete[0]
    assert not any("upgrade --install aws-load-balancer-controller" in c for c in runner.commands())


def test_disabling_ingress_warns_when_the_controller_is_still_installed(tmp_path):
    class InstalledRunner(FakeRunner):
        def run(self, args, **kwargs):
            if "list" in args and "--filter" in args:
                return CommandResult(0, "aws-load-balancer-controller\n")
            return super().run(args, **kwargs)

    for runner, warned in ((FakeRunner(), False), (InstalledRunner(), True)):
        ctx, out = _ctx(tmp_path / str(warned), runner)
        addons = {**ctx.config["addons"], "ingress": {"enabled": False}}
        ctx = dataclasses.replace(ctx, config={**ctx.config, "addons": addons})
        run_install(ctx, resume=False)
        assert ("还装着 aws-load-balancer-controller" in out.getvalue()) is warned
        # 只提示，不自动卸载：还有 Ingress 时卸载会遗留负载均衡器
        assert not any("uninstall" in c for c in runner.commands())


def test_missing_gp2_is_fine_but_other_errors_fail(tmp_path):
    runner = FakeRunner(fail_on="annotate storageclass gp2")
    ctx, _ = _ctx(tmp_path, runner)
    with pytest.raises(InstallError) as err:
        run_install(ctx, resume=False)
    assert err.value.phase == "addons"

    class NotFoundRunner(FakeRunner):
        def run(self, args, **kwargs):
            if "annotate" in args:
                raise CommandError(
                    args, 1, 'Error from server (NotFound): storageclasses.storage.k8s.io "gp2" not found'
                )
            return super().run(args, **kwargs)

    ok = NotFoundRunner()
    ctx, _ = _ctx(tmp_path / "second", ok)
    assert "addons" in run_install(ctx, resume=False)["completed"]
