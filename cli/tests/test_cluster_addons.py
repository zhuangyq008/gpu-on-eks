"""客户 Web 应用用到的插件：提问、配置契约、校验、安装阶段。"""

from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

from qwenctl.addons import (
    KEDA_NAMESPACE,
    PROMETHEUS_NAMESPACE,
    gp3_storage_class,
    load_balancer_controller_values,
)
from qwenctl.advanced import load_advanced
from qwenctl.cli import EXIT_CONFIG_ERROR, EXIT_OK, main
from qwenctl.gateway import HostedZone
from qwenctl.models import migrate_config
from qwenctl.preflight import run_preflight
from qwenctl.questions import ask_external_dns, ask_web_ingress

from .conftest import FakeGateway, RecordingInstall, make_console
from .sample_config import generate

ZONES = tuple(HostedZone(f"Z0{i}ABCDEFGHIJKLMNOP", f"app{i}.example.com") for i in range(1, 13))


def test_web_ingress_defaults_to_enabled():
    con, _ = make_console([], assume_defaults=True)
    assert ask_web_ingress(con) is True
    con, _ = make_console(["n"])
    assert ask_web_ingress(con) is False


def test_external_dns_is_off_by_default_and_needs_zones():
    con, _ = make_console([], assume_defaults=True)
    assert ask_external_dns(con, replace(FakeGateway(), hosted_zones=ZONES[:2])) == ()
    con, out = make_console(["y"])
    assert ask_external_dns(con, FakeGateway()) == ()  # 没有公有托管区域：不问
    assert "没有 Route 53 公有托管区域" in out.getvalue()


def test_external_dns_lists_and_selects_zones():
    con, out = make_console(["y", "2,1,2"])
    chosen = ask_external_dns(con, replace(FakeGateway(), hosted_zones=ZONES[:3]))
    assert chosen == (ZONES[1], ZONES[0])  # 按输入顺序去重
    assert "[3] app3.example.com" in out.getvalue()


@pytest.mark.parametrize("bad", ["0", "4", "a", "1,,x", ",".join(str(i) for i in range(1, 12))])
def test_invalid_zone_selection_is_asked_again(bad):
    con, out = make_console(["y", bad, "1"])
    zones = ZONES[:3] if bad != ",".join(str(i) for i in range(1, 12)) else ZONES
    assert ask_external_dns(con, replace(FakeGateway(), hosted_zones=zones)) == (zones[0],)
    assert "✗" in out.getvalue()


def test_zone_listing_failure_skips_external_dns():
    con, out = make_console(["y"])
    assert ask_external_dns(con, replace(FakeGateway(), hosted_zones_error=True)) == ()
    assert "无法列出 Route 53 托管区域" in out.getvalue()


def test_generated_config_carries_addons():
    config = json.loads(generate("new"))
    assert config["addons"] == {
        "ingress": {"enabled": True},
        "externalDns": {"enabled": False, "hostedZones": []},
    }


def test_old_configs_get_addons_disabled():
    """升级交付包后不能悄悄改动客户的集群：旧配置迁移时插件全部关闭，要启用就改配置再 --update。"""
    config = json.loads(generate("new"))
    del config["addons"]
    migrated = migrate_config(config)
    assert migrated["addons"] == {
        "ingress": {"enabled": False},
        "externalDns": {"enabled": False, "hostedZones": []},
    }
    customer = {
        **config,
        "addons": {"ingress": {"enabled": True}, "externalDns": {"enabled": False, "hostedZones": []}},
    }
    assert migrate_config(customer)["addons"]["ingress"]["enabled"] is True


def _saved(tmp_path, monkeypatch, gw, addons):
    monkeypatch.setenv("HOME", str(tmp_path))
    con, _ = make_console(assume_defaults=True)
    assert main(["deploy", "--yes"], lambda _region: gw, con) == EXIT_OK
    path = tmp_path / ".qwenctl" / "qwen-vl" / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**data, "addons": addons}), encoding="utf-8")


@pytest.mark.parametrize(
    "addons",
    [
        {
            "ingress": {"enabled": False},
            "externalDns": {"enabled": True, "hostedZones": [{"id": "Z01ABC", "name": "a.com"}]},
        },
        {"ingress": {"enabled": True}, "externalDns": {"enabled": True, "hostedZones": []}},
        {
            "ingress": {"enabled": True},
            "externalDns": {"enabled": True, "hostedZones": [{"id": "../x", "name": "a.com"}]},
        },
        {
            "ingress": {"enabled": True},
            "externalDns": {"enabled": True, "hostedZones": [{"id": "Z01ABC", "name": "A.com."}]},
        },
        {"ingress": {"enabled": "yes"}, "externalDns": {"enabled": False, "hostedZones": []}},
        {"ingress": {}},
    ],
)
def test_resume_rejects_tampered_addons(tmp_path, monkeypatch, addons):
    gw = FakeGateway()
    _saved(tmp_path, monkeypatch, gw, addons)
    con, out = make_console()
    assert main(["deploy", "--resume"], lambda _region: gw, con, RecordingInstall()) == EXIT_CONFIG_ERROR
    assert "addons 不对" in out.getvalue()


def test_valid_external_dns_selection_resumes(tmp_path, monkeypatch):
    gw = FakeGateway()
    zones = [{"id": "Z01ABCDEFGHIJKLMNOP", "name": "app.example.com"}]
    _saved(
        tmp_path,
        monkeypatch,
        gw,
        {"ingress": {"enabled": True}, "externalDns": {"enabled": True, "hostedZones": zones}},
    )
    install = RecordingInstall()
    con, _ = make_console()
    assert main(["deploy", "--resume"], lambda _region: gw, con, install) == EXIT_OK
    assert install.requests[0].config["addons"]["externalDns"]["hostedZones"] == zones


def test_fresh_deploy_is_refused_when_the_deployment_already_exists(tmp_path, monkeypatch):
    """重新提问会覆盖保存的配置；加了 --approve 还会按新回答重新部署。已有部署只能 --update / --resume。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    gw = replace(FakeGateway(), stacks={"CDKToolkit": "UPDATE_COMPLETE", "qwen-vl-eks": "UPDATE_COMPLETE"})
    for argv in (["deploy", "--yes", "--approve"], ["deploy", "--plan"]):
        install = RecordingInstall()
        con, out = make_console(assume_defaults=True)
        assert main(argv, lambda _region: gw, con, install) == EXIT_CONFIG_ERROR
        assert "--update" in out.getvalue() and install.requests == []
    con, _ = make_console(assume_defaults=True)
    other = str(tmp_path / "preview.json")
    assert main(["deploy", "--plan", "--output", other], lambda _region: gw, con) == EXIT_OK


def test_preflight_shows_existing_deployment_without_failing():
    gw = replace(FakeGateway(), stacks={"qwen-vl-eks": "UPDATE_COMPLETE"})
    report = run_preflight(gw, "qwen-vl", load_advanced(None))
    assert report.passed and report.existing_deployment


def test_default_storage_class_is_encrypted_gp3():
    sc = gp3_storage_class()
    assert sc["metadata"]["annotations"]["storageclass.kubernetes.io/is-default-class"] == "true"
    assert sc["provisioner"] == "ebs.csi.aws.com"
    assert sc["parameters"] == {"type": "gp3", "encrypted": "true"}
    assert sc["volumeBindingMode"] == "WaitForFirstConsumer" and sc["allowVolumeExpansion"] is True
    cmk = "arn:aws:kms:ap-southeast-5:123456789012:key/11111111-2222-3333-4444-555555555555"
    assert gp3_storage_class(cmk)["parameters"]["kmsKeyId"] == cmk


def test_load_balancer_controller_values():
    values = load_balancer_controller_values("qwen-vl", "ap-southeast-5", "vpc-0abc")
    assert (values["clusterName"], values["region"], values["vpcId"]) == (
        "qwen-vl",
        "ap-southeast-5",
        "vpc-0abc",
    )
    # Service webhook 默认对所有 Service 生效、失败即拒绝：控制器不可用时集群里任何 Service 都建不了
    assert values["enableServiceMutatorWebhook"] is False
    assert values["enableWaf"] is False  # WAF Classic 在马来西亚没有端点，只用 WAFv2
    assert values["serviceAccount"] == {"create": True, "name": "aws-load-balancer-controller"}
    assert values["replicaCount"] == 2 and values["podDisruptionBudget"] == {"maxUnavailable": 1}
    assert values["nodeSelector"] == {"qwenctl/node-role": "system"}


def test_lbc_values_harden_defaults_and_protect_namespaces():
    values = load_balancer_controller_values("qwen-vl", "ap-southeast-5", "vpc-0abc", ("qwen-vl",))
    assert values["enableShield"] is False and values["enableWaf"] is False
    assert values["defaultSSLPolicy"] == "ELBSecurityPolicy-TLS13-1-2-2021-06"
    assert values["defaultTargetType"] == "ip"
    (expr,) = values["ingressClassParams"]["spec"]["namespaceSelector"]["matchExpressions"]
    assert expr["key"] == "kubernetes.io/metadata.name" and expr["operator"] == "NotIn"
    # 没有认证的推理服务、Prometheus 和系统组件不能被 alb Ingress 发布出去
    assert {"qwen-vl", "monitoring", "keda", "kube-system"} <= set(expr["values"])


ACCOUNT = "123456789012"


def _validations(manifest):
    return [v["expression"] for v in manifest["spec"]["validations"]]


def test_admission_policies_guard_load_balancers():
    from qwenctl.addons import admission_policies

    manifests = admission_policies("ap-southeast-5", ACCOUNT, ("qwen-vl",))
    kinds = [(m["kind"], m["metadata"]["name"]) for m in manifests]
    assert kinds == [
        ("ValidatingAdmissionPolicy", "qwenctl-lb-service-class"),
        ("ValidatingAdmissionPolicyBinding", "qwenctl-lb-service-class"),
        ("ValidatingAdmissionPolicy", "qwenctl-public-alb-waf"),
        ("ValidatingAdmissionPolicyBinding", "qwenctl-public-alb-waf"),
    ]
    service, _, ingress, binding = manifests
    assert service["spec"]["failurePolicy"] == "Fail"  # 在 API server 内部求值，不依赖控制器
    rule = ingress["spec"]["matchConstraints"]["resourceRules"][0]
    assert rule["apiGroups"] == ["networking.k8s.io"] and rule["resources"] == ["ingresses"]
    assert binding["spec"]["validationActions"] == ["Deny"]
    # 正在删除的对象不检查，否则策略生效前建的不合规对象摘不掉 finalizer
    for policy in (service, ingress):
        assert policy["spec"]["matchConditions"] == [
            {"name": "not-being-deleted", "expression": "!has(object.metadata.deletionTimestamp)"}
        ]


@pytest.mark.parametrize(
    "region, account",
    [
        ("ap-southeast-5'", ACCOUNT),
        ("ap-southeast-5", "12345"),
        ("ap-southeast-5", "1234567890123"),
        ("x", ACCOUNT),
    ],
)
def test_admission_policies_reject_values_that_would_break_the_expressions(region, account):
    from qwenctl.addons import admission_policies

    with pytest.raises(ValueError):
        admission_policies(region, account)


def test_load_balancer_services_are_denied_in_protected_namespaces():
    from qwenctl.addons import admission_policies

    class_rule, namespace_rule = _validations(admission_policies("ap-southeast-5", ACCOUNT, ("qwen-vl",))[0])
    assert "has(object.spec.loadBalancerClass)" in class_rule
    assert "namespaceObject.metadata.name in [" in namespace_rule
    for namespace in ("qwen-vl", "kube-system", PROMETHEUS_NAMESPACE, KEDA_NAMESPACE):
        assert f"'{namespace}'" in namespace_rule
    assert "'default'" not in namespace_rule


def test_legacy_ingress_class_annotation_is_denied():
    from qwenctl.addons import admission_policies

    legacy, _ = _validations(admission_policies("ap-southeast-5", ACCOUNT)[2])
    # 旧注解不经过 IngressClassParams，命名空间限制会失效
    assert "'kubernetes.io/ingress.class' in object.metadata.annotations" in legacy
    assert "!= 'alb'" in legacy


def test_public_ingress_needs_a_real_web_acl_arn_in_this_region():
    from qwenctl.addons import admission_policies

    _, waf = _validations(admission_policies("ap-southeast-5", ACCOUNT)[2])
    assert "== 'internet-facing'" in waf
    pattern = re.search(r"\.matches\('([^']+)'\)", waf).group(1)
    good = "arn:aws:wafv2:ap-southeast-5:123456789012:regional/webacl/qwen-vl-ingress/a5157b9d-38a3-497b-b226-81ca93327caf"
    assert re.match(pattern, good)
    for bad in [
        "",
        "none",
        good.replace("ap-southeast-5", "us-east-1"),  # 别的 region 的 WebACL 挂不上
        good.replace("regional/webacl", "global/webacl"),  # CloudFront 用的
        good.replace("wafv2", "waf-regional"),  # 旧版 WAF
        good.replace("123456789012", "1234"),
        good.replace("123456789012", "210987654321"),  # 别的账号的 WebACL
        good + "' || true || '",
    ]:
        assert not re.match(pattern, bad), bad


def test_zones_with_non_standard_names_are_not_offered():
    zones = (
        HostedZone("Z01ABCDEFGHIJ", "app.example.com"),
        HostedZone("Z02ABCDEFGHIJ", "\\052.wild.example.com"),
    )
    con, out = make_console(["y", "1"])
    assert ask_external_dns(con, replace(FakeGateway(), hosted_zones=zones)) == (zones[0],)
    assert "不是标准域名" in out.getvalue() and "wild" not in out.getvalue().split("不是标准域名")[1]


def test_hosted_zone_validation_matches_the_cdk_schema():
    from qwenctl.names import is_hosted_zone

    assert is_hosted_zone("Z0123456789ABC", "app.example.com")
    assert is_hosted_zone("Z0123456789ABC", "xn--fiqs8s.xn--55qx5d")  # 国际化域名（punycode）
    for zone_id, name in [
        ("z0123", "a.com"),
        ("Z01", "A.com"),
        ("Z01", "a.com."),
        ("Z01", "com"),
        ("Z01", "a..com"),
    ]:
        assert not is_hosted_zone(zone_id, name)


def test_saved_config_alone_blocks_a_fresh_deploy(tmp_path, monkeypatch):
    """网络栈建好、EKS 栈还没建时中断，CloudFormation 里还看不到 EKS 栈，但本机已经保存了配置。"""
    gw = FakeGateway()
    con, _ = make_console(assume_defaults=True)
    assert main(["deploy", "--yes"], lambda _region: gw, con) == EXIT_OK
    con, out = make_console(assume_defaults=True)
    assert main(["deploy", "--yes"], lambda _region: gw, con) == EXIT_CONFIG_ERROR
    assert "本机已经保存了它的配置" in out.getvalue()


def test_network_stack_alone_counts_as_existing_deployment():
    gw = replace(FakeGateway(), stacks={"qwen-vl-network": "CREATE_COMPLETE"})
    assert run_preflight(gw, "qwen-vl", load_advanced(None)).existing_deployment
    broken = replace(FakeGateway(), stacks={"qwen-vl-network": "ROLLBACK_COMPLETE"})
    assert not run_preflight(broken, "qwen-vl", load_advanced(None)).passed
