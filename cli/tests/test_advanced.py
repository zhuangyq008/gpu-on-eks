import pytest

from qwenctl.advanced import DEFAULTS, ConfigError, deep_merge, load_advanced, validate


def test_no_file_returns_copy_of_defaults():
    merged = load_advanced(None)
    assert merged == DEFAULTS
    merged["general_nodes"]["cpu_limit"] = 1
    assert DEFAULTS["general_nodes"]["cpu_limit"] == 200


def test_deep_merge_does_not_mutate_inputs():
    base = {"a": {"b": 1, "c": [1]}}
    override = {"a": {"b": 2}}
    merged = deep_merge(base, override)
    assert merged == {"a": {"b": 2, "c": [1]}}
    assert base == {"a": {"b": 1, "c": [1]}}
    merged["a"]["c"].append(2)
    assert base["a"]["c"] == [1]


@pytest.mark.parametrize(
    "override,message",
    [
        ({"servng": {}}, "未知的配置项：servng"),
        ({"serving": {"max_model_len": "big"}}, "serving.max_model_len 应该是 int"),
        ({"serving": {"max_model_len": True}}, "serving.max_model_len 应该是 int"),
        ({"serving": 3}, "serving 应该是一个对象"),
        ({"general_nodes": {"arch": ["x86"]}}, "general_nodes.arch 只能取"),
        ({"general_nodes": {"arch": []}}, "general_nodes.arch 只能取"),
        ({"network": {"nat_gateways": 3}}, "network.nat_gateways 只能取"),
        ({"serving": {"gpu_memory_utilization": 0.99}}, "0.5~0.98"),
        ({"compliance": {"kms_key_arn": 12}}, "compliance.kms_key_arn 的类型不对"),
        ({"scaling": {"schedule": "daily"}}, "scaling.schedule 的类型不对"),
        (
            {"observability": {"log_retention_days": {"app": 10}}},
            "observability.log_retention_days.app 只能取",
        ),
        ({"network": {"interface_endpoints": True}}, "network.interface_endpoints 应该是 str"),
        ({"network": {"interface_endpoints": "yes"}}, "network.interface_endpoints 只能取"),
        ({"network": {"cidr": "10.0.0.0/18"}}, "/16 或 /17"),
        ({"network": {"cidr": "10.0.0.1/16"}}, "不是合法的 CIDR"),
        ({"compliance": {"tags": {"Owner": 1}}}, "key 和 value 都必须是字符串"),
        ({"compliance": {"extra_admin_role_arns": ["arn:aws:iam::123:user/x"]}}, "不是 IAM 角色 ARN"),
        ({"scaling": {"waiting_threshold": 0}}, "1~1000"),
        ({"scaling": {"waiting_threshold": float("nan")}}, "1~1000"),
        ({"scaling": {"c_sla": {"g7e.2xlarge": 32}}}, "key 是 GPU profile 名"),
        ({"scaling": {"c_sla": {"g7e-1gpu": "40"}}}, "1~1024 的整数"),
        ({"scaling": {"c_sla": {"g7e-1gpu": -5}}}, "1~1024 的整数"),
        ({"scaling": {"c_sla": {"g7e-1gpu": True}}}, "1~1024 的整数"),
    ],
)
def test_validate_rejects_bad_values(override, message):
    with pytest.raises(ConfigError, match=message):
        validate(override)


def test_validate_accepts_freeform_and_nullable():
    validate(
        {
            "scaling": {"c_sla": {"g7e-1gpu": 32}, "schedule": {"timezone": "Asia/Kuala_Lumpur"}},
            "compliance": {
                "tags": {"CostCenter": "mfg"},
                "kms_key_arn": "arn:aws:kms:...",
                "extra_admin_role_arns": ["arn:aws:iam::123456789012:role/ops/PlatformAdmin"],
            },
            "general_nodes": {"arch": ["amd64", "arm64"], "cpu_limit": 64},
            "serving": {"gpu_memory_utilization": 0.9},
            "network": {"interface_endpoints": "off", "cidr": "10.20.0.0/17"},
        }
    )


def test_load_yaml_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("general_nodes:\n  cpu_limit: 64\nnetwork:\n  nat_gateways: 2\n", encoding="utf-8")
    merged = load_advanced(str(path))
    assert merged["general_nodes"]["cpu_limit"] == 64
    assert merged["general_nodes"]["arch"] == ["amd64"]
    assert merged["network"]["nat_gateways"] == 2


def test_empty_yaml_file_means_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8")
    assert load_advanced(str(path)) == DEFAULTS


@pytest.mark.parametrize(
    "content,message", [("- a\n- b\n", "顶层应该是一个对象"), ("a: [1,\n", "不是合法的 YAML")]
)
def test_bad_yaml(tmp_path, content, message):
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_advanced(str(path))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="无法读取"):
        load_advanced(str(tmp_path / "nope.yaml"))
