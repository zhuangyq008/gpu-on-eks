from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from typing import List, Optional, Sequence

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
from qwenctl.versions import load_versions

from .conftest import make_console
from .sample_config import generate

EKS_OUTPUTS = {
    "ClusterName": "qwen-vl",
    "ClusterSecurityGroupId": "sg-0123",
    "KarpenterNodeRoleName": "node-role",
    "KarpenterInterruptionQueueName": "queue-1",
    "PrivateSubnetIds": "subnet-0a,subnet-0b",
}
CHART_BYTES = {"karpenter-crd": b"fake-crd-chart", "karpenter": b"fake-karpenter-chart"}


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


class FakeRunner:
    def __init__(
        self,
        fail_on: Optional[str] = None,
        cfn_error: bool = False,
        job_status: Optional[dict] = None,
        pods: Optional[list] = None,
    ) -> None:
        self.calls: List[dict] = []
        self.fail_on = fail_on
        self.cfn_error = cfn_error
        self.job_status = job_status if job_status is not None else {"succeeded": 1}
        self.pods = pods if pods is not None else [READY_POD]

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
            chart = args[args.index("pull") + 1].rsplit("/", 1)[-1]
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
        if "get" in args and "job" in args:
            return CommandResult(0, json.dumps({"status": self.job_status}))
        if "get" in args and "pods" in args:
            return CommandResult(0, json.dumps({"items": self.pods}))
        if "logs" in args:
            return CommandResult(0, "Traceback: boom")
        return CommandResult(0, "")

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


def _applied(runner, kind):
    return [
        i for c in runner.calls if c["input"] for i in json.loads(c["input"])["items"] if i["kind"] == kind
    ]


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
    assert "没有就绪的 vLLM 副本" in text and "scale deploy/vllm-<回退 profile>" in text


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
