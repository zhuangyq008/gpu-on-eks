import pytest

from qwenctl.identity import IdentityError, assumed_role_name, principal_arn

ASSUMED = "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Admin_abc/alice@example.com"


def test_assumed_role_is_converted_to_role_arn():
    assert principal_arn(ASSUMED) == "arn:aws:iam::123456789012:role/AWSReservedSSO_Admin_abc"


def test_resolved_role_arn_with_path_wins():
    resolved = "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_Admin_abc"
    assert principal_arn(ASSUMED, resolved) == resolved


def test_iam_user_is_unchanged():
    arn = "arn:aws:iam::123456789012:user/dev1"
    assert principal_arn(arn) == arn
    assert assumed_role_name(arn) is None


def test_partition_is_preserved():
    arn = "arn:aws-cn:sts::123456789012:assumed-role/Ops/s1"
    assert principal_arn(arn) == "arn:aws-cn:iam::123456789012:role/Ops"


def test_root_is_rejected():
    with pytest.raises(IdentityError, match="root"):
        principal_arn("arn:aws:iam::123456789012:root")


def test_assumed_role_name():
    assert assumed_role_name(ASSUMED) == "AWSReservedSSO_Admin_abc"


def test_federated_user_is_rejected():
    with pytest.raises(IdentityError, match="federated user"):
        principal_arn("arn:aws:sts::123456789012:federated-user/bob")
