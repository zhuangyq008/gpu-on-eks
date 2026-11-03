import json
import os
import stat
from dataclasses import replace

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
    con, out = make_console(["1", "1", "", "", "", "", "n"])
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
    con, out = make_console(["2", "1", "1", "", "", "", "", "y"])
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
    con, out = make_console(["1", "1", "", "", "", "", "y", "y"])
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
    con, out = make_console(["1", "1", "", "", "", "", "y", "n"])
    assert main(["deploy"], _factory(gw), con, install) == EXIT_ABORTED
    assert install.requests == []
    assert "cdk bootstrap" in out.getvalue()


def test_yes_without_approve_only_saves_config(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    install = RecordingInstall()
    con, _ = make_console(assume_defaults=True)
    assert main(["deploy", "--yes"], _factory(gw), con, install) == EXIT_OK
    assert install.requests == []
    con, _ = make_console(assume_defaults=True)
    assert main(["deploy", "--yes", "--approve"], _factory(gw), con, install) == EXIT_OK
    assert len(install.requests) == 1


def test_plan_never_installs(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    install = RecordingInstall()
    con, out = make_console(["1", "1", "", "", "", "", "y"])
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
