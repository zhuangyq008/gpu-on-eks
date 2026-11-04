from dataclasses import replace

from qwenctl.advanced import load_advanced
from qwenctl.gateway import CapacityReservation
from qwenctl.preflight import FAIL, INFO, OK, WARN, run_preflight

from .conftest import FakeGateway

ADV = load_advanced(None)


def _run(gw: FakeGateway):
    return run_preflight(gw, "qwen-vl", ADV)


def _titles(report, status):
    return [c.title for c in report.checks if c.status == status]


def test_malaysia_happy_path(gw):
    report = _run(gw)
    assert report.passed
    assert report.selection.zone_ids == ("apse5-az1", "apse5-az2")
    assert report.gpu_available_vcpus == 768.0
    assert report.upper_bound_for(report.selection) == 96
    assert report.system_instance_type == "m7g.large"
    assert report.observability_mode == "cloudwatch"
    assert report.bedrock_overflow_available is False
    assert report.principal_arn == "arn:aws:iam::123456789012:role/Admin"
    assert any("G/VT 配额 768 vCPU" in t for t in _titles(report, OK))
    assert any("g7e.2xlarge 只在" in t for t in _titles(report, WARN))
    assert any("本 region 没有 Amazon Managed Grafana" in t for t in _titles(report, INFO))
    assert any("通用 CPU 节点池" in t for t in _titles(report, OK))


def test_zero_gpu_quota_fails_with_increase_command(gw):
    report = _run(replace(gw, quotas={"L-DB2E81BA": 4.0, "L-1216C47A": 1152.0}))
    assert not report.passed
    failed = [c for c in report.checks if c.status == FAIL]
    assert "request-service-quota-increase" in failed[0].detail


def test_low_gpu_quota_warns_and_limits_upper_bound(gw):
    report = _run(
        replace(gw, quotas={"L-DB2E81BA": 32.0, "L-1216C47A": 1152.0}, running={"gpu": 8.0, "standard": 0})
    )
    assert report.passed
    assert report.upper_bound_for(report.selection) == 3
    assert any("最多 3 个副本" in t for t in _titles(report, WARN))


def test_unknown_quota_falls_back_to_default_upper_bound(gw):
    report = _run(replace(gw, quotas={}, running_error=True))
    assert report.gpu_available_vcpus is None
    assert report.upper_bound_for(report.selection) == 8
    assert "无法读取 G/VT 实例配额" in _titles(report, WARN)
    assert any("通用 CPU 节点池" in t for t in _titles(report, WARN))


def test_running_vcpus_error_is_reported_as_unknown(gw):
    report = _run(replace(gw, running_error=True))
    assert report.gpu_available_vcpus == 768.0
    gpu = next(c for c in report.checks if c.title.startswith("G/VT 配额"))
    assert gpu.status == WARN
    assert "已用 未知" in gpu.title
    assert "ec2:DescribeInstances" in gpu.detail


def test_low_standard_quota_warns(gw):
    report = _run(replace(gw, quotas={"L-DB2E81BA": 768.0, "L-1216C47A": 100.0}))
    warn = [c for c in report.checks if c.status == WARN and "通用 CPU 节点池" in c.title]
    assert "L-1216C47A" in warn[0].detail


def test_root_identity_fails(gw):
    report = _run(replace(gw, caller_arn="arn:aws:iam::123456789012:root"))
    assert not report.passed
    assert "当前身份不能用于部署" in _titles(report, FAIL)


def test_unresolved_role_warns(gw):
    report = _run(replace(gw, resolved_role=None))
    assert report.principal_arn == "arn:aws:iam::123456789012:role/Admin"
    assert any("iam:GetRole" in t for t in _titles(report, WARN))


def test_grafana_mode_requires_amg_and_idc(gw):
    assert _run(replace(gw, grafana=True, idc=1)).observability_mode == "grafana"
    no_idc = _run(replace(gw, grafana=True, idc=0))
    assert no_idc.observability_mode == "cloudwatch"
    assert any("未启用 IAM Identity Center" in t for t in _titles(no_idc, INFO))


def test_bedrock_cdk_and_existing_deployment(gw):
    stacks = {"CDKToolkit": "UPDATE_COMPLETE", "qwen-vl-bootstrap": "CREATE_COMPLETE"}
    report = _run(replace(gw, bedrock=True, stacks=stacks))
    assert report.bedrock_overflow_available
    assert report.cdk_bootstrapped
    assert report.existing_deployment
    assert "CDK 已经 bootstrap" in _titles(report, OK)


def test_stack_errors_degrade_to_warnings(gw):
    report = _run(replace(gw, stack_error=True))
    assert report.passed
    assert "无法检查 CDK bootstrap" in _titles(report, WARN)
    assert "无法检查是否已有同名部署" in _titles(report, WARN)


def test_denied_permissions_fail(gw):
    report = _run(replace(gw, denied=("eks:CreateCluster",)))
    assert not report.passed
    assert [c.detail for c in report.checks if c.status == FAIL] == ["eks:CreateCluster"]


def test_simulate_error_warns(gw):
    report = _run(replace(gw, simulate_error=True))
    assert report.passed
    assert any("SimulatePrincipalPolicy" in t for t in _titles(report, WARN))


def test_odcr_in_selected_zones_is_reported(gw):
    reservations = (
        CapacityReservation("cr-1", "g7e.2xlarge", "apse5-az2", 2, True),
        CapacityReservation("cr-2", "g7e.2xlarge", "apse5-az3", 2, True),
        CapacityReservation("cr-3", "g7e.2xlarge", "apse5-az2", 1, False),
    )
    report = _run(replace(gw, reservations=reservations))
    info = [c for c in report.checks if c.status == INFO and "ODCR" in c.title]
    assert "cr-1" in info[0].detail and "cr-2" not in info[0].detail
    assert "open：可直接使用" in info[0].detail and "targeted：需要按 ID 指定" in info[0].detail


def test_missing_prices_warn(gw):
    report = _run(replace(gw, price_map={}))
    assert "无法获取部分机型的价格" in _titles(report, WARN)


def test_region_without_gpu_fails(gw):
    offerings = {z: frozenset({"m7g.large"}) for z in ("apse5-az1", "apse5-az2", "apse5-az3")}
    report = _run(replace(gw, offering_map=offerings))
    assert not report.passed
    assert report.selection is None
    assert "GPU 可用区" in _titles(report, FAIL)


def test_missing_system_node_type_fails(gw):
    offerings = {
        "apse5-az1": frozenset({"g6.12xlarge"}),
        "apse5-az2": frozenset({"g7e.2xlarge", "g6.12xlarge", "m7g.large"}),
    }
    report = _run(replace(gw, offering_map=offerings))
    assert "系统节点" in _titles(report, FAIL)


def test_quota_check_mentions_fallback_vcpu_cost(gw):
    gpu = next(c for c in _run(gw).checks if c.title.startswith("G/VT 配额"))
    assert "回退到 g6.12xlarge 时每个副本占 24 vCPU" in gpu.detail


def test_unknown_idc_and_bedrock_are_warnings_not_facts(gw):
    report = _run(replace(gw, grafana=True, idc=None, bedrock=None))
    assert report.observability_mode == "cloudwatch"
    assert report.bedrock_overflow_available is False
    warns = _titles(report, WARN)
    assert any("无法判断是否启用了 IAM Identity Center" in t for t in warns)
    assert any("无法判断 Bedrock" in t for t in warns)


def test_broken_existing_deployment_fails(gw):
    report = _run(replace(gw, stacks={"qwen-vl-bootstrap": "ROLLBACK_COMPLETE"}))
    assert not report.passed
    failed = next(c for c in report.checks if c.status == FAIL)
    assert "ROLLBACK_COMPLETE" in failed.title and "destroy" in failed.detail


def test_in_progress_deployment_fails(gw):
    report = _run(replace(gw, stacks={"qwen-vl-bootstrap": "UPDATE_IN_PROGRESS"}))
    assert any("正在变更中" in t for t in _titles(report, FAIL))


def test_broken_cdk_toolkit_warns(gw):
    report = _run(replace(gw, stacks={"CDKToolkit": "UPDATE_ROLLBACK_FAILED"}))
    assert report.cdk_bootstrapped
    assert any("UPDATE_ROLLBACK_FAILED" in t for t in _titles(report, WARN))


def test_replica_question_is_capped_like_saved_config_validation():
    """配额很大时提问上限也不超过 MAX_GPU_REPLICAS，否则保存的配置之后 --resume 会被拒绝。"""
    from qwenctl.catalog import MAX_GPU_REPLICAS
    from qwenctl.questions import ask_capacity

    from .conftest import make_console

    huge = FakeGateway(quotas={"L-DB2E81BA": 100_000.0, "L-1216C47A": 1152.0})
    report = _run(huge)
    assert report.upper_bound_for(report.selection) > MAX_GPU_REPLICAS
    con, out = make_console(assume_defaults=True)
    choice = ask_capacity(con, report, report.selection)
    assert f"上限 {MAX_GPU_REPLICAS}" in out.getvalue()
    assert choice.max_replicas <= MAX_GPU_REPLICAS
