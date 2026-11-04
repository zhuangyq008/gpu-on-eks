import json
import os
import stat
from dataclasses import replace

import pytest

from qwenctl.cli import (
    EXIT_ABORTED,
    EXIT_AWS_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_INSTALL_FAILED,
    EXIT_OK,
    EXIT_PREFLIGHT_FAILED,
    EXIT_UNEXPECTED,
    main,
)
from qwenctl.gateway import GatewayError

from .conftest import FakeGateway, RecordingInstall, make_console


def _factory(gw):
    return lambda region: gw


def test_preflight_only(gw):
    con, out = make_console()
    assert main(["preflight"], _factory(gw), con) == EXIT_OK
    assert "AZ: apse5-az1 (ap-southeast-5a), apse5-az2 (ap-southeast-5b)" in out.getvalue()


def test_deploy_with_defaults_writes_config(gw, tmp_path):
    target = tmp_path / "cfg" / "qwen-vl.json"
    con, out = make_console(assume_defaults=True)
    code = main(["deploy", "--yes", "--output", str(target)], _factory(gw), con)
    assert code == EXIT_OK
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["schemaVersion"] == 1
    assert data["region"] == "ap-southeast-5"
    assert [z["zoneId"] for z in data["zones"]] == ["apse5-az1", "apse5-az2"]
    assert data["gpu"]["minReplicas"] == 2 and data["gpu"]["maxReplicas"] == 8
    profiles = {p["name"]: p for p in data["gpu"]["profiles"]}
    assert profiles["g7e-1gpu"]["zoneIds"] == ["apse5-az2"]
    assert profiles["g6-tp2"]["tensorParallel"] == 2
    assert profiles["g6-tp2"]["experimental"] is True
    assert data["systemNodes"]["instanceType"] == "m7g.large"
    assert data["advanced"]["general_nodes"]["arch"] == ["amd64"]
    assert data["observability"]["mode"] == "cloudwatch"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    text = out.getvalue()
    assert "[3/3] 确认" in text
    assert "通用 CPU 节点 amd64" in text
    assert "没有在账号里创建任何资源（非交互模式需要再加 --approve 才会部署）" in text


def test_deploy_cancel_at_confirmation_writes_nothing(gw, tmp_path):
    target = tmp_path / "qwen-vl.json"
    con, out = make_console(["1", "1", "", "", "", "", "", "n"])
    code = main(["deploy", "--output", str(target)], _factory(gw), con)
    assert code == EXIT_ABORTED
    assert not target.exists()
    assert "没有做任何改动" in out.getvalue()


def test_preflight_failure_stops_deploy(gw):
    con, out = make_console(assume_defaults=True)
    failing = replace(gw, denied=("eks:CreateCluster",))
    assert main(["deploy", "--yes"], _factory(failing), con) == EXIT_PREFLIGHT_FAILED
    assert "环境检查没有通过" in out.getvalue()


def test_invalid_name(gw):
    con, out = make_console()
    assert main(["preflight", "--name", "Bad_Name"], _factory(gw), con) == EXIT_CONFIG_ERROR
    assert "部署名称" in out.getvalue()


def test_bad_advanced_config(gw, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("unknown: 1\n", encoding="utf-8")
    con, out = make_console()
    assert main(["preflight", "--config", str(path)], _factory(gw), con) == EXIT_CONFIG_ERROR
    assert "未知的配置项：unknown" in out.getvalue()


def test_gateway_error(gw):
    def broken(region):
        raise GatewayError("没有可用的 AWS 凭证")

    con, out = make_console()
    assert main(["preflight"], broken, con) == EXIT_AWS_ERROR
    assert "没有可用的 AWS 凭证" in out.getvalue()


def test_no_command_prints_help(capsys):
    assert main([], _factory(FakeGateway())) == EXIT_CONFIG_ERROR
    assert "usage" in capsys.readouterr().out


def test_confirmation_page_shows_endpoint_plan(gw, tmp_path):
    con, out = make_console(assume_defaults=True)
    assert main(["deploy", "--yes", "--output", str(tmp_path / "c.json")], _factory(gw), con) == EXIT_OK
    text = out.getvalue()
    assert "接口终端节点：ecr.api, ecr.dkr, sts, eks-auth, sqs" in text
    assert "单 NAT" in text
    data = json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
    assert data["network"]["endpoints"]["interface"] == ["ecr.api", "ecr.dkr", "sts", "eks-auth", "sqs"]
    assert data["access"]["openToInternet"] is False


def test_existing_vpc_config_carries_subnets_and_s3_plan(gw, tmp_path):
    target = tmp_path / "c.json"
    con, out = make_console(["2", "1", "1", "", "", "", "", "", "y"])
    assert main(["deploy", "--plan", "--output", str(target)], _factory(gw), con) == EXIT_OK
    network = json.loads(target.read_text(encoding="utf-8"))["network"]
    assert [s["zoneId"] for s in network["privateSubnets"]] == ["apse5-az1", "apse5-az2"]
    assert network["endpoints"] == {"interface": [], "s3Gateway": True, "s3RouteTableIds": ["rtb-priv-b"]}
    assert "不创建 ecr.api：现有 VPC 默认不创建" in out.getvalue()


def test_unexpected_error_is_reported_without_traceback(gw):
    class Broken(FakeGateway):
        def zones(self):
            raise RuntimeError("boom")

    con, out = make_console()
    assert main(["preflight"], _factory(Broken()), con) == EXIT_UNEXPECTED
    assert "意外错误：RuntimeError: boom" in out.getvalue()
    assert "Traceback" not in out.getvalue()
    con, out = make_console()
    assert main(["--debug", "preflight"], _factory(Broken()), con) == EXIT_UNEXPECTED
    assert "Traceback" in out.getvalue()


def test_ctrl_c_is_a_clean_abort(gw):
    class Interrupted(FakeGateway):
        def zones(self):
            raise KeyboardInterrupt

    con, out = make_console()
    assert main(["preflight"], _factory(Interrupted()), con) == EXIT_ABORTED
    assert "已中断" in out.getvalue() and "--resume" in out.getvalue()


def test_name_with_trailing_newline_is_rejected(gw):
    con, _ = make_console()
    assert main(["preflight", "--name", "qwen-vl\n"], _factory(gw), con) == EXIT_CONFIG_ERROR


# ---- 部署 ----
def test_interactive_deploy_runs_install_with_saved_config(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    install = RecordingInstall()
    con, out = make_console(["1", "1", "", "", "", "", "", "y", "y"])
    stacks = {"CDKToolkit": "CREATE_COMPLETE"}
    assert main(["deploy"], _factory(replace(gw, stacks=stacks)), con, install) == EXIT_OK
    (request,) = install.requests
    assert request.name == "qwen-vl" and request.cdk_bootstrapped is True and request.resume is False
    assert request.config_path == str(tmp_path / ".qwenctl" / "qwen-vl" / "config.json")
    assert request.config["gpu"]["maxReplicas"] == 8
    assert "部署完成：qwen-vl" in out.getvalue()


def test_bootstrap_needs_consent(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    install = RecordingInstall()
    con, out = make_console(["1", "1", "", "", "", "", "", "y", "n"])
    assert main(["deploy"], _factory(gw), con, install) == EXIT_ABORTED
    assert install.requests == []
    assert "cdk bootstrap" in out.getvalue()


def test_yes_without_approve_only_saves_config(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    install = RecordingInstall()
    con, _ = make_console(assume_defaults=True)
    assert main(["deploy", "--yes"], _factory(gw), con, install) == EXIT_OK
    assert install.requests == []
    # 配置已经保存：再次提问会覆盖它，所以被拒绝；部署保存的配置用 --resume
    con, out = make_console(assume_defaults=True)
    assert main(["deploy", "--yes", "--approve"], _factory(gw), con, install) == EXIT_CONFIG_ERROR
    assert "--resume" in out.getvalue() and install.requests == []
    con, _ = make_console()
    assert main(["deploy", "--resume"], _factory(gw), con, install) == EXIT_OK
    assert len(install.requests) == 1


def test_plan_never_installs(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    install = RecordingInstall()
    con, out = make_console(["1", "1", "", "", "", "", "", "y"])
    assert main(["deploy", "--plan"], _factory(gw), con, install) == EXIT_OK
    assert install.requests == []
    assert "确认后会在这个账号里创建" not in out.getvalue()


def test_install_failure_points_to_resume(gw, tmp_path, monkeypatch):
    from qwenctl.installer import InstallError

    monkeypatch.setenv("HOME", str(tmp_path))
    install = RecordingInstall(InstallError("karpenter", "helm 失败"))
    con, out = make_console(assume_defaults=True)
    assert main(["deploy", "--yes", "--approve"], _factory(gw), con, install) == EXIT_INSTALL_FAILED
    text = out.getvalue()
    assert "部署在「karpenter」阶段失败：helm 失败" in text
    assert "./qwenctl deploy --name qwen-vl --resume" in text


def test_resume_uses_saved_config(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    con, _ = make_console(assume_defaults=True)
    assert main(["deploy", "--yes"], _factory(gw), con) == EXIT_OK
    install = RecordingInstall()
    con, out = make_console()
    assert main(["deploy", "--resume"], _factory(gw), con, install) == EXIT_OK
    (request,) = install.requests
    assert request.resume is True and request.config["region"] == "ap-southeast-5"
    assert "继续部署 qwen-vl" in out.getvalue()


def test_resume_without_saved_config(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    con, out = make_console()
    assert main(["deploy", "--resume"], _factory(gw), con, RecordingInstall()) == EXIT_CONFIG_ERROR
    assert "找不到可以继续的部署配置" in out.getvalue()


def test_resume_refuses_other_account_or_tampered_config(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    con, _ = make_console(assume_defaults=True)
    assert main(["deploy", "--yes"], _factory(gw), con) == EXIT_OK
    other = replace(gw, account="999999999999")
    con, out = make_console()
    assert main(["deploy", "--resume"], _factory(other), con, RecordingInstall()) == EXIT_CONFIG_ERROR
    assert "当前登录的是 999999999999" in out.getvalue()
    path = tmp_path / ".qwenctl" / "qwen-vl" / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**data, "region": "--endpoint-url=http://evil"}), encoding="utf-8")
    con, out = make_console()
    assert main(["deploy", "--resume"], _factory(gw), con, RecordingInstall()) == EXIT_CONFIG_ERROR
    assert "格式不对" in out.getvalue()


def test_update_reruns_all_phases_and_migrates_old_config(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    con, _ = make_console(assume_defaults=True)
    assert main(["deploy", "--yes"], _factory(gw), con) == EXIT_OK
    path = tmp_path / ".qwenctl" / "qwen-vl" / "config.json"
    old = json.loads(path.read_text(encoding="utf-8"))
    old.pop("serving")  # 模拟旧版本 qwenctl 保存的配置
    old["gpu"].update(minReplicas=1, maxReplicas=1)
    path.write_text(json.dumps(old), encoding="utf-8")
    install = RecordingInstall()
    con, out = make_console()
    assert main(["deploy", "--update"], _factory(gw), con, install) == EXIT_OK
    (request,) = install.requests
    assert request.resume is False
    assert request.config["serving"]["namespace"] == "qwen-vl"
    assert request.config["gpu"]["maxReplicas"] == 1  # 客户的选择保持不变
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["serving"]["vllmServiceAccount"] == "vllm"
    assert "已为保存的配置补上新版本新增的字段" in out.getvalue()


def test_resume_and_update_are_exclusive(gw):
    con, out = make_console()
    assert (
        main(["deploy", "--resume", "--update"], _factory(gw), con, RecordingInstall()) == EXIT_CONFIG_ERROR
    )
    assert "只能选一个" in out.getvalue()


def test_migrate_fills_profile_sizing_from_catalog_without_overriding():
    from qwenctl.models import migrate_config

    old = {
        "gpu": {"profiles": [{"name": "g6-tp2"}, {"name": "g7e-1gpu", "podCpu": "4"}, {"name": "unknown"}]},
    }
    profiles = migrate_config(old)["gpu"]["profiles"]
    assert profiles[0] == {
        "name": "g6-tp2",
        "podCpu": "20",
        "podMemory": "88Gi",
        "servingLimits": {"maxModelLen": 8192, "maxNumSeqs": 16, "maxImages": 2},
        "cSla": 8,
    }
    assert profiles[1]["podCpu"] == "4" and profiles[1]["podMemory"] == "48Gi"
    assert profiles[2] == {"name": "unknown"}


def test_resume_rejects_tampered_serving_names(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    con, _ = make_console(assume_defaults=True)
    assert main(["deploy", "--yes"], _factory(gw), con) == EXIT_OK
    path = tmp_path / ".qwenctl" / "qwen-vl" / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["serving"]["namespace"] = "../kube-system"
    path.write_text(json.dumps(data), encoding="utf-8")
    con, out = make_console()
    assert main(["deploy", "--update"], _factory(gw), con, RecordingInstall()) == EXIT_CONFIG_ERROR
    assert "serving 的命名格式不对" in out.getvalue()


def test_migrate_fills_new_advanced_defaults_but_keeps_customer_values():
    from qwenctl.models import migrate_config

    old = {
        "gpu": {"profiles": []},
        "advanced": {"serving": {"max_model_len": 4096}, "general_nodes": {"cpu_limit": 64}},
    }
    advanced = migrate_config(old)["advanced"]
    assert advanced["serving"]["max_model_len"] == 4096  # 客户的值
    assert advanced["serving"]["max_num_seqs"] == 128  # 新版本新增的默认值
    assert advanced["general_nodes"] == {"arch": ["amd64"], "capacity_types": ["on-demand"], "cpu_limit": 64}


def test_update_revalidates_hand_edited_advanced_config(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    con, _ = make_console(assume_defaults=True)
    assert main(["deploy", "--yes"], _factory(gw), con) == EXIT_OK
    path = tmp_path / ".qwenctl" / "qwen-vl" / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["advanced"]["serving"]["gpu_memory_utilization"] = 1.5
    path.write_text(json.dumps(data), encoding="utf-8")
    con, out = make_console()
    assert main(["deploy", "--update"], _factory(gw), con, RecordingInstall()) == EXIT_CONFIG_ERROR
    assert "gpu_memory_utilization" in out.getvalue()


def test_resume_rejects_tampered_profile_names(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    con, _ = make_console(assume_defaults=True)
    assert main(["deploy", "--yes"], _factory(gw), con) == EXIT_OK
    path = tmp_path / ".qwenctl" / "qwen-vl" / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["gpu"]["profiles"][0]["name"] = 'g7e"} or vector(99) #'
    path.write_text(json.dumps(data), encoding="utf-8")
    con, out = make_console()
    assert main(["deploy", "--resume"], _factory(gw), con, RecordingInstall()) == EXIT_CONFIG_ERROR
    assert "GPU profile 的名字格式不对" in out.getvalue()


@pytest.mark.parametrize(
    "tamper",
    [
        lambda d: d["gpu"].update(minReplicas=0),
        lambda d: d["gpu"].update(minReplicas=3, maxReplicas=2),
        lambda d: d["gpu"].update(maxReplicas=10_000),
        lambda d: d["gpu"]["profiles"][0].update(cSla=-5),
        lambda d: d["gpu"]["profiles"][0].update(cSla="32"),
        lambda d: d["gpu"]["profiles"][0].update(tensorParallel=True),
    ],
)
def test_resume_rejects_tampered_gpu_numbers(gw, tmp_path, monkeypatch, tamper):
    monkeypatch.setenv("HOME", str(tmp_path))
    con, _ = make_console(assume_defaults=True)
    assert main(["deploy", "--yes"], _factory(gw), con) == EXIT_OK
    path = tmp_path / ".qwenctl" / "qwen-vl" / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    tamper(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    con, out = make_console()
    assert main(["deploy", "--resume"], _factory(gw), con, RecordingInstall()) == EXIT_CONFIG_ERROR
    assert "GPU 副本数或 profile 参数不对" in out.getvalue()


def test_resume_rejects_c_sla_for_unknown_profile_before_installing(gw, tmp_path, monkeypatch):
    """扩缩容配置写错要在安装开始前报，而不是等到最后的 serving 阶段。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    con, _ = make_console(assume_defaults=True)
    assert main(["deploy", "--yes"], _factory(gw), con) == EXIT_OK
    path = tmp_path / ".qwenctl" / "qwen-vl" / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["advanced"]["scaling"]["c_sla"] = {"g7e-1gpus": 40}
    path.write_text(json.dumps(data), encoding="utf-8")
    install = RecordingInstall()
    con, out = make_console()
    assert main(["deploy", "--update"], _factory(gw), con, install) == EXIT_CONFIG_ERROR
    assert install.requests == [] and "g7e-1gpus" in out.getvalue()


def test_resume_rejects_overlapping_bucket_prefixes(gw, tmp_path, monkeypatch):
    """同步 Job 能写模型前缀；缓存前缀落在它下面，就等于同步 Job 能往编译缓存里写代码。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    con, _ = make_console(assume_defaults=True)
    assert main(["deploy", "--yes"], _factory(gw), con) == EXIT_OK
    path = tmp_path / ".qwenctl" / "qwen-vl" / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["serving"]["compileCachePrefix"] = "models/cache/"
    path.write_text(json.dumps(data), encoding="utf-8")
    con, out = make_console()
    assert main(["deploy", "--resume"], _factory(gw), con, RecordingInstall()) == EXIT_CONFIG_ERROR
    assert "serving 的命名格式不对" in out.getvalue()
