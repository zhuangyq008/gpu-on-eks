from dataclasses import replace

import pytest

from qwenctl.advanced import load_advanced
from qwenctl.console import Aborted
from qwenctl.gateway import Certificate, VpcDns
from qwenctl.models import SubnetRef
from qwenctl.preflight import run_preflight
from qwenctl.questions import ask_all, validate_cidrs, validate_email

from .conftest import CPU_TYPES, MY_OFFERINGS, VPC, FakeGateway, Subnet, make_console

ADV = load_advanced(None)
CERT = Certificate("arn:aws:acm:ap-southeast-5:123456789012:certificate/abc", "api.example.com")


def _ask(gw: FakeGateway, answers, assume_defaults=False):
    report = run_preflight(gw, "qwen-vl", ADV)
    # 提问 4 的最后一项是「Web 应用入口」：多给一个回车（默认启用）；没用到的回答会被忽略
    con, out = make_console([*answers, ""], assume_defaults)
    result = ask_all(con, report, gw, ADV)
    return result, out.getvalue()


def test_all_defaults(gw):
    (answers, selection), out = _ask(gw, [], assume_defaults=True)
    assert answers.network.mode == "new"
    assert answers.network.cidr == "10.100.0.0/16"
    assert answers.access.mode == "internal"
    assert (answers.capacity.min_replicas, answers.capacity.max_replicas) == (2, 8)
    assert answers.features.async_channel is True
    assert answers.features.bedrock_overflow is False
    assert answers.features.alert_email is None
    assert selection.zone_ids == ("apse5-az1", "apse5-az2")
    assert "常驻 ≈ $8,412/月" in out
    assert "Bedrock 溢出：当前 region 不可用" in out


def test_existing_vpc_then_public_without_cert_falls_back_to_internal(gw):
    inputs = ["2", "1", "2", "1", "", "", "", "", ""]
    (answers, selection), out = _ask(gw, inputs)
    n = answers.network
    assert n.mode == "existing" and n.vpc_id == "vpc-1"
    assert n.cidr == "10.0.0.0/16" and n.secondary_cidrs == ("100.64.0.0/16",)
    assert n.private_subnets == (
        SubnetRef("subnet-priv-a", "apse5-az1", "10.0.32.0/19", "rtb-priv-a"),
        SubnetRef("subnet-priv-b", "apse5-az2", "10.0.64.0/19", "rtb-priv-b"),
    )
    assert [s.zone_id for s in n.private_subnets] == list(selection.zone_ids)
    assert [s.subnet_id for s in n.public_subnets] == ["subnet-pub-a", "subnet-pub-b"]
    assert n.existing_interface_endpoints == ("sts",)
    assert n.s3_covered_route_table_ids == ("rtb-priv-a",)
    assert "私有子网 subnet-priv-a（10.0.32.0/19）" in out
    assert "没有 ISSUED 状态的 ACM 证书" in out
    assert answers.access.mode == "internal"


def test_public_access_with_cert_and_cidrs(gw):
    inputs = ["1", "2", "1", "bad-cidr", "203.0.113.0/24, 198.51.100.0/24", "", "", "", "", ""]
    (answers, _), out = _ask(replace(gw, certs=(CERT,)), inputs)
    assert answers.access.mode == "public"
    assert answers.access.certificate_arn == CERT.arn
    assert answers.access.allowed_cidrs == ("203.0.113.0/24", "198.51.100.0/24")
    assert "bad-cidr 不是合法的 CIDR" in out


def test_open_to_internet_needs_explicit_confirmation(gw):
    inputs = ["1", "2", "1", "", "n", "0.0.0.0/0", "y", "", "", "", ""]
    (answers, _), out = _ask(replace(gw, certs=(CERT,)), inputs)
    assert answers.access.open_to_internet is True
    assert answers.access.allowed_cidrs == ()
    assert out.count("不限制来源 IP") == 2


def test_open_to_internet_is_refused_in_non_interactive_mode(gw):
    from qwenctl.models import NetworkChoice
    from qwenctl.questions import NetworkResult, _ask_public

    report = run_preflight(gw, "qwen-vl", ADV)
    con, _ = make_console([], assume_defaults=True)
    network = NetworkResult(NetworkChoice(mode="new", cidr="10.100.0.0/16"), report.selection)
    with pytest.raises(Aborted, match="非交互模式"):
        _ask_public(con, network, replace(gw, certs=(CERT,)))


def test_existing_vpc_without_public_subnets_blocks_public_access(gw):
    no_public = tuple(s for s in gw.subnet_list if "pub" not in s.subnet_id)
    inputs = ["2", "1", "2", "1", "", "", "", "", ""]
    (answers, _), out = _ask(replace(gw, subnet_list=no_public, certs=(CERT,)), inputs)
    assert "没有齐全的公有子网" in out
    assert answers.access.mode == "internal"


def test_unusable_vpc_loops_back_to_network_question(gw):
    one_zone = (Subnet("subnet-priv-b", "apse5-az2", "ap-southeast-5b", "10.0.64.0/19", 8000),)
    inputs = ["2", "1", "1", "1", "", "", "", "", ""]
    (answers, _), out = _ask(replace(gw, subnet_list=one_zone), inputs)
    assert "这个 VPC 不能用" in out
    assert "空闲 IP 足够的私有子网" in out
    assert answers.network.mode == "new"


def test_vpc_without_dns_is_rejected(gw):
    inputs = ["2", "1", "1", "1", "", "", "", ""]
    (answers, _), out = _ask(replace(gw, dns=VpcDns(support=True, hostnames=False)), inputs)
    assert "enableDnsHostnames" in out
    assert answers.network.mode == "new"


def test_vpc_whose_zones_need_more_quota_is_rejected(gw):
    # 只有 az1、az3 有私有子网：主力机型变成 g6.12xlarge（每节点 48 vCPU），而配额只有 32
    subnets = tuple(s for s in gw.subnet_list if s.zone_id != "apse5-az2")
    low_quota = replace(gw, subnet_list=subnets, quotas={"L-DB2E81BA": 32.0, "L-1216C47A": 1152.0})
    (answers, _), out = _ask(low_quota, ["2", "1", "1", "1", "", "", "", ""])
    assert "只能用 g6.12xlarge，G/VT 配额不够启动 1 个副本" in out
    assert "request-service-quota-increase" in out
    assert answers.network.mode == "new"


def test_vpc_without_system_node_type_is_rejected(gw):
    offerings = {z: types - CPU_TYPES for z, types in MY_OFFERINGS.items()}
    offerings["apse5-az2"] = offerings["apse5-az2"] | {"m7g.large"}
    offerings["apse5-az1"] = offerings["apse5-az1"] | {"m7g.large"}
    report = run_preflight(replace(gw, offering_map=offerings), "qwen-vl", ADV)
    assert report.passed
    subnets = tuple(s for s in gw.subnet_list if s.zone_id != "apse5-az1")
    no_cpu_gw = replace(gw, offering_map=offerings, subnet_list=subnets)
    from qwenctl.questions import VpcRejected, inspect_existing_vpc

    with pytest.raises(VpcRejected, match="系统节点机型"):
        inspect_existing_vpc(VPC, report, no_cpu_gw)


def test_vpc_listing_error_loops_back(gw):
    inputs = ["2", "1", "1", "", "", "", "", ""]
    (answers, _), out = _ask(replace(gw, vpc_error=True), inputs)
    assert "无法列出 VPC" in out
    assert answers.network.mode == "new"


def test_no_vpcs_loops_back(gw):
    inputs = ["2", "1", "1", "", "", "", "", ""]
    (answers, _), out = _ask(replace(gw, vpc_list=()), inputs)
    assert "没有可用的 VPC" in out
    assert answers.network.mode == "new"


def test_single_replica_warns(gw):
    inputs = ["1", "1", "1", "3", "", "", ""]
    (answers, _), out = _ask(gw, inputs)
    assert (answers.capacity.min_replicas, answers.capacity.max_replicas) == (1, 3)
    assert "只有 1 个常驻副本" in out


def test_bedrock_overflow_needs_double_confirmation(gw):
    bedrock_gw = replace(gw, bedrock=True)
    (answers, _), _ = _ask(bedrock_gw, ["1", "1", "", "", "", "y", "y", "ops@example.com"])
    assert answers.features.bedrock_overflow is True
    assert answers.features.alert_email == "ops@example.com"
    (answers, _), _ = _ask(bedrock_gw, ["1", "1", "", "", "n", "y", "n", ""])
    assert answers.features.bedrock_overflow is False
    assert answers.features.async_channel is False


def test_invalid_email_is_rejected(gw):
    (answers, _), out = _ask(gw, ["1", "1", "", "", "", "not-an-email", ""])
    assert "不是合法的邮箱地址" in out
    assert answers.features.alert_email is None


def test_abort_on_eof(gw):
    with pytest.raises(Aborted):
        _ask(gw, ["1"])


def test_requires_selection(gw):
    report = replace(run_preflight(gw, "qwen-vl", ADV), selection=None)
    con, _ = make_console([])
    with pytest.raises(ValueError):
        ask_all(con, report, gw, ADV)


def test_validators():
    assert validate_cidrs("10.0.0.0/8, 192.168.1.0/24") is None
    assert "主机位不为 0，应该写成 10.0.0.0/8" in validate_cidrs("10.0.0.1/8")
    assert "不是合法的 CIDR" in validate_cidrs("300.0.0.0/8")
    assert validate_email("") is None
    assert validate_email("a@b.co") is None
