from qwenctl.endpoints import plan_endpoints, wanted_interface_endpoints
from qwenctl.models import FeatureChoice, NetworkChoice, SubnetRef

from .conftest import SERVICE_ZONES

ZONES = ("apse5-az1", "apse5-az2")
NEW = NetworkChoice(mode="new", cidr="10.100.0.0/16")
EXISTING = NetworkChoice(
    mode="existing",
    cidr="10.0.0.0/16",
    vpc_id="vpc-1",
    private_subnets=(
        SubnetRef("subnet-a", "apse5-az1", "10.0.32.0/19", "rtb-a"),
        SubnetRef("subnet-b", "apse5-az2", "10.0.64.0/19", "rtb-b"),
    ),
    existing_interface_endpoints=("sts",),
    s3_covered_route_table_ids=("rtb-a",),
)
ASYNC = FeatureChoice(async_channel=True, bedrock_overflow=False)


def test_wanted_endpoints_follow_features():
    assert wanted_interface_endpoints(ASYNC)[-1] == "sqs"
    both = FeatureChoice(async_channel=True, bedrock_overflow=True)
    assert wanted_interface_endpoints(both)[-2:] == ("sqs", "bedrock-runtime")
    assert "sqs" not in wanted_interface_endpoints(FeatureChoice(False, False))


def test_new_vpc_auto_creates_all_supported_endpoints():
    plan = plan_endpoints("auto", NEW, ASYNC, ZONES, SERVICE_ZONES)
    assert plan.interface == ("ecr.api", "ecr.dkr", "sts", "eks-auth", "sqs")
    assert plan.skipped == ()
    assert plan.s3_gateway is True and plan.s3_route_table_ids == ()


def test_existing_vpc_auto_skips_interface_endpoints_but_adds_s3_to_uncovered_tables():
    plan = plan_endpoints("auto", EXISTING, ASYNC, ZONES, SERVICE_ZONES)
    assert plan.interface == ()
    assert all("DNS" in reason for _, reason in plan.skipped)
    assert plan.s3_gateway is True
    assert plan.s3_route_table_ids == ("rtb-b",)


def test_existing_vpc_forced_on_skips_ones_already_present():
    plan = plan_endpoints("on", EXISTING, ASYNC, ZONES, SERVICE_ZONES)
    assert plan.interface == ("ecr.api", "ecr.dkr", "eks-auth", "sqs")
    assert ("sts", "VPC 里已经有了") in plan.skipped


def test_off_disables_interface_endpoints():
    plan = plan_endpoints("off", NEW, ASYNC, ZONES, SERVICE_ZONES)
    assert plan.interface == ()
    assert plan.skipped[0][1] == "高级配置已关闭"


def test_services_missing_in_a_zone_are_skipped():
    zones = {**SERVICE_ZONES, "eks-auth": frozenset({"apse5-az1"})}
    plan = plan_endpoints("auto", NEW, ASYNC, ZONES, zones)
    assert "eks-auth" not in plan.interface
    assert ("eks-auth", "在 apse5-az2 不提供，流量走 NAT") in plan.skipped


def test_unknown_service_availability_assumes_supported():
    plan = plan_endpoints("auto", NEW, ASYNC, ZONES, None)
    assert len(plan.interface) == 5


def test_fully_covered_route_tables_need_no_s3_endpoint():
    covered = NetworkChoice(**{**EXISTING.__dict__, "s3_covered_route_table_ids": ("rtb-a", "rtb-b")})
    plan = plan_endpoints("auto", covered, ASYNC, ZONES, SERVICE_ZONES)
    assert plan.s3_gateway is False and plan.s3_route_table_ids == ()
