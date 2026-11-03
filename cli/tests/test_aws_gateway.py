"""Boto3Gateway 的解析逻辑：用 MagicMock 代替 boto3 client，不访问网络。"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

from qwenctl.aws_gateway import Boto3Gateway
from qwenctl.gateway import GatewayError


def _client_error(code: str, message: str = "boom") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Op")


def _client(pages: Optional[Dict[str, List[Dict[str, Any]]]] = None, **methods: Any) -> MagicMock:
    client = MagicMock()
    pages = pages or {}

    def get_paginator(operation: str) -> MagicMock:
        paginator = MagicMock()
        paginator.paginate.return_value = pages[operation]
        return paginator

    client.get_paginator.side_effect = get_paginator
    for name, value in methods.items():
        setattr(client, name, value)
    return client


class FakeSession:
    def __init__(self, clients: Dict[str, MagicMock], region: str = "ap-southeast-5") -> None:
        self.region_name = region
        self._clients = clients

    def client(self, name: str, region_name: Optional[str] = None, config: Any = None) -> MagicMock:
        return self._clients[name]


def _gw(**clients: MagicMock) -> Boto3Gateway:
    return Boto3Gateway(session=FakeSession({k.replace("_", "-"): v for k, v in clients.items()}))


def test_requires_region():
    with pytest.raises(GatewayError, match="无法确定 region"):
        Boto3Gateway(session=FakeSession({}, region=None))


def test_caller_identity_and_missing_credentials():
    sts = _client(get_caller_identity=MagicMock(return_value={"Account": "1", "Arn": "arn"}))
    assert _gw(sts=sts).caller_identity() == ("1", "arn")
    broken = _client(get_caller_identity=MagicMock(side_effect=NoCredentialsError()))
    with pytest.raises(GatewayError, match="没有可用的 AWS 凭证"):
        _gw(sts=broken).caller_identity()


def test_role_arn():
    iam = _client(get_role=MagicMock(return_value={"Role": {"Arn": "arn:role"}}))
    assert _gw(iam=iam).role_arn("Admin") == "arn:role"
    denied = _client(get_role=MagicMock(side_effect=_client_error("AccessDenied")))
    assert _gw(iam=denied).role_arn("Admin") is None


def test_zones_and_offerings():
    ec2 = _client(
        pages={
            "describe_instance_type_offerings": [
                {"InstanceTypeOfferings": [{"InstanceType": "g7e.2xlarge", "Location": "apse5-az2"}]},
                {"InstanceTypeOfferings": [{"InstanceType": "g6.12xlarge", "Location": "apse5-az2"}]},
            ]
        },
        describe_availability_zones=MagicMock(
            return_value={
                "AvailabilityZones": [
                    {"ZoneId": "apse5-az1", "ZoneName": "ap-southeast-5a", "State": "available"},
                    {"ZoneId": "apse5-az9", "ZoneName": "ap-southeast-5z", "State": "impaired"},
                ]
            }
        ),
    )
    gw = _gw(ec2=ec2)
    assert [z.zone_id for z in gw.zones()] == ["apse5-az1"]
    assert gw.offerings(["g7e.2xlarge"]) == {"apse5-az2": frozenset({"g7e.2xlarge", "g6.12xlarge"})}


def test_quota_falls_back_to_default_value():
    sq = _client(
        get_service_quota=MagicMock(side_effect=_client_error("NoSuchResourceException")),
        get_aws_default_service_quota=MagicMock(return_value={"Quota": {"Value": 64.0}}),
    )
    assert _gw(service_quotas=sq).quota("ec2", "L-X") == 64.0
    ok = _client(get_service_quota=MagicMock(return_value={"Quota": {"Value": 768.0}}))
    assert _gw(service_quotas=ok).quota("ec2", "L-X") == 768.0
    denied = _client(get_service_quota=MagicMock(side_effect=_client_error("AccessDenied")))
    assert _gw(service_quotas=denied).quota("ec2", "L-X") is None


def test_running_vcpus_counts_only_requested_family():
    instances = [
        {"InstanceType": "g7e.2xlarge"},
        {"InstanceType": "g7e.2xlarge"},
        {"InstanceType": "g7e.2xlarge", "InstanceLifecycle": "spot"},
        {"InstanceType": "m7g.large"},
    ]
    ec2 = _client(
        pages={"describe_instances": [{"Reservations": [{"Instances": instances}]}]},
        describe_instance_types=MagicMock(
            side_effect=lambda InstanceTypes: {
                "InstanceTypes": [
                    {"InstanceType": t, "VCpuInfo": {"DefaultVCpus": {"g7e.2xlarge": 8, "m7g.large": 2}[t]}}
                    for t in InstanceTypes
                ]
            }
        ),
    )
    gw = _gw(ec2=ec2)
    assert gw.running_vcpus("gpu") == 16.0
    assert gw.running_vcpus("standard") == 2.0


def test_prices_parse_on_demand_and_tolerate_errors():
    product = {
        "terms": {"OnDemand": {"t1": {"priceDimensions": {"d1": {"pricePerUnit": {"USD": "5.7616300000"}}}}}}
    }
    pricing = _client(
        get_products=MagicMock(
            side_effect=[
                {"PriceList": [json.dumps(product)]},
                {"PriceList": []},
                _client_error("AccessDenied"),
            ]
        )
    )
    prices = _gw(pricing=pricing).prices(["g7e.2xlarge", "x.large", "y.large"])
    assert prices == {"g7e.2xlarge": pytest.approx(5.76163), "x.large": None, "y.large": None}


def test_service_available():
    ok = _client(list_workspaces=MagicMock(return_value={"workspaces": []}))
    assert _gw(grafana=ok).service_available("grafana") is True
    no_endpoint = _client(
        list_workspaces=MagicMock(side_effect=EndpointConnectionError(endpoint_url="https://x"))
    )
    assert _gw(grafana=no_endpoint).service_available("grafana") is False
    denied = _client(list_workspaces=MagicMock(side_effect=_client_error("AccessDenied")))
    assert _gw(grafana=denied).service_available("grafana") is True


def test_idc_and_bedrock():
    sso = _client(list_instances=MagicMock(return_value={"Instances": [{}]}))
    bedrock = _client(
        list_foundation_models=MagicMock(
            return_value={"modelSummaries": [{"modelId": "qwen.qwen3-vl-235b-a22b:0"}]}
        )
    )
    gw = _gw(sso_admin=sso, bedrock=bedrock)
    assert gw.idc_instance_count() == 1
    assert gw.bedrock_model_available("qwen.qwen3-vl-235b-a22b") is True
    assert gw.bedrock_model_available("qwen.other") is False


def test_idc_and_bedrock_unknown_vs_unavailable():
    sso = _client(list_instances=MagicMock(side_effect=_client_error("AccessDeniedException")))
    denied = _client(list_foundation_models=MagicMock(side_effect=_client_error("AccessDeniedException")))
    assert _gw(sso_admin=sso).idc_instance_count() is None
    assert _gw(bedrock=denied).bedrock_model_available("x") is None
    no_service = _client(
        list_foundation_models=MagicMock(side_effect=EndpointConnectionError(endpoint_url="x"))
    )
    assert _gw(bedrock=no_service).bedrock_model_available("x") is False


def test_stack_status():
    def describe(StackName):
        if StackName == "missing":
            raise _client_error("ValidationError", "Stack with id missing does not exist")
        if StackName == "deleted":
            return {"Stacks": [{"StackStatus": "DELETE_COMPLETE"}]}
        if StackName == "denied":
            raise _client_error("AccessDenied")
        return {"Stacks": [{"StackStatus": "ROLLBACK_COMPLETE"}]}

    gw = _gw(cloudformation=_client(describe_stacks=MagicMock(side_effect=describe)))
    assert gw.stack_status("CDKToolkit") == "ROLLBACK_COMPLETE"
    assert gw.stack_status("missing") is None
    assert gw.stack_status("deleted") is None
    with pytest.raises(GatewayError):
        gw.stack_status("denied")


def test_capacity_reservations_and_permissions():
    ec2 = _client(
        pages={
            "describe_capacity_reservations": [
                {
                    "CapacityReservations": [
                        {
                            "CapacityReservationId": "cr-1",
                            "InstanceType": "g7e.2xlarge",
                            "AvailabilityZoneId": "apse5-az2",
                            "AvailableInstanceCount": 2,
                            "InstanceMatchCriteria": "targeted",
                        }
                    ]
                }
            ]
        }
    )
    iam = _client(
        pages={
            "simulate_principal_policy": [
                {
                    "EvaluationResults": [
                        {"EvalActionName": "eks:CreateCluster", "EvalDecision": "allowed"},
                        {"EvalActionName": "iam:CreateRole", "EvalDecision": "explicitDeny"},
                    ]
                }
            ]
        }
    )
    gw = _gw(ec2=ec2, iam=iam)
    reservation = gw.capacity_reservations(["g7e.2xlarge"])[0]
    assert reservation.zone_id == "apse5-az2" and reservation.open_matching is False
    assert gw.denied_actions("arn:role", ["eks:CreateCluster", "iam:CreateRole"]) == ("iam:CreateRole",)


def test_existing_vpc_queries():
    ec2 = _client(
        pages={
            "describe_vpcs": [
                {
                    "Vpcs": [
                        {
                            "VpcId": "vpc-1",
                            "CidrBlock": "10.0.0.0/16",
                            "Tags": [{"Key": "Name", "Value": "f"}],
                            "CidrBlockAssociationSet": [
                                {"CidrBlock": "10.0.0.0/16", "CidrBlockState": {"State": "associated"}},
                                {"CidrBlock": "100.64.0.0/16", "CidrBlockState": {"State": "associated"}},
                                {"CidrBlock": "100.65.0.0/16", "CidrBlockState": {"State": "disassociated"}},
                            ],
                        }
                    ]
                }
            ],
            "describe_subnets": [
                {
                    "Subnets": [
                        {
                            "SubnetId": "s-1",
                            "AvailabilityZoneId": "apse5-az1",
                            "AvailabilityZone": "ap-southeast-5a",
                            "CidrBlock": "10.0.0.0/19",
                            "AvailableIpAddressCount": 8000,
                        },
                        {
                            "SubnetId": "s-v6",
                            "AvailabilityZoneId": "apse5-az1",
                            "AvailabilityZone": "ap-southeast-5a",
                            "AvailableIpAddressCount": 0,
                            "Ipv6Native": True,
                        },
                    ]
                }
            ],
            "describe_route_tables": [
                {
                    "RouteTables": [
                        {
                            "RouteTableId": "rtb-1",
                            "Associations": [{"Main": True}, {"SubnetId": "s-1"}],
                            "Routes": [
                                {
                                    "DestinationCidrBlock": "10.0.0.0/16",
                                    "GatewayId": "local",
                                    "State": "active",
                                },
                                {
                                    "DestinationCidrBlock": "0.0.0.0/0",
                                    "NatGatewayId": "nat-1",
                                    "State": "active",
                                },
                            ],
                        }
                    ]
                }
            ],
            "describe_vpc_endpoints": [
                {
                    "VpcEndpoints": [
                        {"ServiceName": "com.amazonaws.ap-southeast-5.sts", "VpcEndpointType": "Interface"},
                        {
                            "ServiceName": "com.amazonaws.ap-southeast-5.ecr.api",
                            "VpcEndpointType": "Interface",
                            "State": "deleting",
                        },
                        {
                            "ServiceName": "com.amazonaws.ap-southeast-5.s3",
                            "VpcEndpointType": "Gateway",
                            "RouteTableIds": ["rtb-1"],
                        },
                        {
                            "ServiceName": "aws.sagemaker.ap-southeast-5.notebook",
                            "VpcEndpointType": "Interface",
                        },
                    ]
                }
            ],
        },
        describe_vpc_attribute=MagicMock(
            side_effect=lambda VpcId, Attribute: {
                "enableDnsSupport": {"EnableDnsSupport": {"Value": True}},
                "enableDnsHostnames": {"EnableDnsHostnames": {"Value": False}},
            }[Attribute]
        ),
    )
    gw = _gw(ec2=ec2)
    vpc = gw.vpcs()[0]
    assert vpc.name == "f" and vpc.secondary_cidrs == ("100.64.0.0/16",)
    assert [s.subnet_id for s in gw.subnets("vpc-1")] == ["s-1"]
    table = gw.route_tables("vpc-1")[0]
    assert table.is_main and table.subnet_ids == ("s-1",) and table.default_route_target == "nat-1"
    endpoints = gw.endpoint_services("vpc-1")
    assert endpoints.interface == frozenset({"sts"})
    assert endpoints.s3_gateway_route_table_ids == frozenset({"rtb-1"})
    dns = gw.vpc_dns("vpc-1")
    assert dns.support is True and dns.hostnames is False


def test_endpoint_service_zones():
    ec2 = _client(
        pages={
            "describe_vpc_endpoint_services": [
                {
                    "ServiceDetails": [
                        {
                            "ServiceName": "com.amazonaws.ap-southeast-5.sts",
                            "ServiceType": [{"ServiceType": "Interface"}],
                            "AvailabilityZones": ["ap-southeast-5a", "ap-southeast-5b"],
                        },
                        {
                            "ServiceName": "com.amazonaws.ap-southeast-5.s3",
                            "ServiceType": [{"ServiceType": "Gateway"}],
                            "AvailabilityZones": ["ap-southeast-5a"],
                        },
                    ]
                }
            ]
        },
        describe_availability_zones=MagicMock(
            return_value={
                "AvailabilityZones": [
                    {"ZoneId": "apse5-az1", "ZoneName": "ap-southeast-5a", "State": "available"},
                    {"ZoneId": "apse5-az2", "ZoneName": "ap-southeast-5b", "State": "available"},
                ]
            }
        ),
    )
    assert _gw(ec2=ec2).endpoint_service_zones(["sts", "s3"]) == {
        "sts": frozenset({"apse5-az1", "apse5-az2"})
    }
    broken = MagicMock()
    broken.get_paginator.side_effect = _client_error("UnauthorizedOperation")
    assert _gw(ec2=broken).endpoint_service_zones(["sts"]) is None


def test_certificates_and_error_wrapping():
    acm = _client(
        pages={
            "list_certificates": [
                {"CertificateSummaryList": [{"CertificateArn": "arn:c", "DomainName": "a.com"}]}
            ]
        }
    )
    assert _gw(acm=acm).issued_certificates()[0].domain == "a.com"
    broken = MagicMock()
    broken.get_paginator.side_effect = _client_error("AccessDeniedException")
    with pytest.raises(GatewayError, match="无法列出 ACM 证书（AccessDeniedException）"):
        _gw(acm=broken).issued_certificates()


def test_region_falls_back_to_aws_region_env(monkeypatch):
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setenv("AWS_REGION", "ap-southeast-5")
    assert Boto3Gateway().region() == "ap-southeast-5"
