import json
import os
import stat

import pytest

from qwenctl.advanced import load_advanced
from qwenctl.models import (
    AccessChoice,
    Answers,
    CapacityChoice,
    DeploymentConfig,
    EndpointPlan,
    FeatureChoice,
    NetworkChoice,
)
from qwenctl.preflight import run_preflight
from qwenctl.store import default_path, save_config


def _config(gw):
    report = run_preflight(gw, "qwen-vl", load_advanced(None))
    answers = Answers(
        network=NetworkChoice(mode="new", cidr="10.100.0.0/16"),
        access=AccessChoice(mode="public", certificate_arn="arn:cert", allowed_cidrs=("1.2.3.0/24",)),
        capacity=CapacityChoice(2, 4),
        features=FeatureChoice(async_channel=False, bedrock_overflow=False, alert_email="a@b.co"),
    )
    return DeploymentConfig(
        name="qwen-vl",
        account=report.account,
        region=report.region,
        admin_principal_arn=report.principal_arn,
        selection=report.selection,
        system_instance_type="m7g.large",
        observability_mode="cloudwatch",
        answers=answers,
        endpoints=EndpointPlan(("ecr.api",), (("sqs", "不需要"),), True, ()),
        advanced=load_advanced(None),
    )


def test_save_to_default_path_under_home(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = save_config(_config(gw))
    assert path == default_path("qwen-vl") == str(tmp_path / ".qwenctl" / "qwen-vl" / "config.json")
    assert stat.S_IMODE(os.stat(tmp_path / ".qwenctl" / "qwen-vl").st_mode) == 0o700
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    assert data["access"] == {
        "mode": "public",
        "certificateArn": "arn:cert",
        "allowedCidrs": ["1.2.3.0/24"],
        "openToInternet": False,
    }
    assert data["network"]["endpoints"] == {
        "interface": ["ecr.api"],
        "s3Gateway": True,
        "s3RouteTableIds": [],
    }
    assert data["features"] == {"asyncChannel": False, "bedrockOverflow": False, "alertEmail": "a@b.co"}


def test_overwrite_keeps_private_permissions(gw, tmp_path):
    target = tmp_path / "x.json"
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o600)
    save_config(_config(gw), str(target))
    assert json.loads(target.read_text(encoding="utf-8"))["name"] == "qwen-vl"


def test_failed_write_leaves_no_partial_file(gw, tmp_path, monkeypatch):
    target = tmp_path / "x.json"
    target.write_text('{"name": "old"}', encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("qwenctl.store.os.replace", boom)
    with pytest.raises(OSError):
        save_config(_config(gw), str(target))
    assert json.loads(target.read_text(encoding="utf-8")) == {"name": "old"}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]
