"""高级配置（config.yaml）：默认值、合并和校验（见 docs/03 §3.3）。"""

from __future__ import annotations

import copy
import ipaddress
import re
from typing import Any, Dict, Mapping, Optional, Tuple

DEFAULTS: Mapping[str, Mapping[str, Any]] = {
    "serving": {
        "max_model_len": 32768,
        "max_images_per_request": 4,
        "max_image_pixels": 2097152,
        "gpu_memory_utilization": 0.92,
        "max_num_seqs": 128,
    },
    "scaling": {
        "c_sla": {},
        "target_utilization": 0.65,
        "waiting_threshold": 4,
        "kv_cache_threshold": 0.8,
        "warm_node_minutes": 10,
        "schedule": None,
    },
    "capacity": {"extra_fallback_types": []},
    "general_nodes": {
        "arch": ["amd64"],
        "capacity_types": ["on-demand"],
        "cpu_limit": 200,
    },
    "network": {
        "cidr": "10.100.0.0/16",
        "nat_gateways": 1,
        "eks_endpoint": "public-and-private",
        # auto = 新建 VPC 时创建、现有 VPC 时不创建；客户用集中式终端节点时设为 off
        "interface_endpoints": "auto",
    },
    "compliance": {"kms_key_arn": None, "tags": {}, "extra_admin_role_arns": []},
    "observability": {
        "amp": "auto",
        "log_retention_days": {"app": 14, "access": 30, "coldstart": 90},
    },
    "image": {"source_registry": "docker.io", "credentials_secret_arn": None},
    "cdk": {"bootstrap_qualifier": None},
}

Path = Tuple[str, ...]

# 默认值为 None 的字段，允许的类型
_NULLABLE: Mapping[Path, Tuple[type, ...]] = {
    ("scaling", "schedule"): (dict,),
    ("compliance", "kms_key_arn"): (str,),
    ("image", "credentials_secret_arn"): (str,),
    ("cdk", "bootstrap_qualifier"): (str,),
}

# 自由结构的 dict：只检查是 dict，不检查里面的 key
_FREEFORM: Tuple[Path, ...] = (("scaling", "c_sla"), ("compliance", "tags"), ("scaling", "schedule"))

# CloudWatch Logs 只接受这些保留天数
LOG_RETENTION_DAYS = frozenset(
    {1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653}
)

_ENUMS: Mapping[Path, frozenset] = {
    ("general_nodes", "arch"): frozenset({"amd64", "arm64"}),
    ("general_nodes", "capacity_types"): frozenset({"on-demand", "spot"}),
    ("network", "eks_endpoint"): frozenset({"public-and-private", "private"}),
    ("network", "nat_gateways"): frozenset({1, 2}),
    ("observability", "amp"): frozenset({"auto", "on", "off"}),
    ("network", "interface_endpoints"): frozenset({"auto", "on", "off"}),
    ("observability", "log_retention_days", "app"): LOG_RETENTION_DAYS,
    ("observability", "log_retention_days", "access"): LOG_RETENTION_DAYS,
    ("observability", "log_retention_days", "coldstart"): LOG_RETENTION_DAYS,
}

_RANGES: Mapping[Path, Tuple[float, float]] = {
    ("serving", "gpu_memory_utilization"): (0.5, 0.98),
    ("serving", "max_images_per_request"): (1, 16),
    ("serving", "max_num_seqs"): (1, 1024),
    ("scaling", "target_utilization"): (0.1, 1.0),
    ("scaling", "kv_cache_threshold"): (0.1, 1.0),
    ("general_nodes", "cpu_limit"): (2, 10000),
}


class ConfigError(Exception):
    """config.yaml 有误。"""


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    """返回新的 dict，不修改输入。"""
    result: Dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result = {**result, key: deep_merge(result[key], value)}
        else:
            result = {**result, key: copy.deepcopy(value)}
    return result


def validate(override: Mapping[str, Any], defaults: Mapping[str, Any] = DEFAULTS, path: Path = ()) -> None:
    for key, value in override.items():
        here = path + (key,)
        dotted = ".".join(here)
        if key not in defaults:
            raise ConfigError(f"未知的配置项：{dotted}")
        _validate_value(here, dotted, value, defaults[key])


def _validate_value(here: Path, dotted: str, value: Any, default: Any) -> None:
    if default is None:
        allowed = _NULLABLE.get(here, (str,))
        if value is not None and not isinstance(value, allowed):
            raise ConfigError(f"{dotted} 的类型不对")
    elif isinstance(default, Mapping) and here not in _FREEFORM:
        if not isinstance(value, Mapping):
            raise ConfigError(f"{dotted} 应该是一个对象")
        validate(value, default, here)
    elif not _same_type(value, default):
        raise ConfigError(f"{dotted} 应该是 {type(default).__name__}，实际是 {type(value).__name__}")
    _validate_enum(here, dotted, value)
    _validate_range(here, dotted, value)
    _validate_special(here, dotted, value)


def _same_type(value: Any, default: Any) -> bool:
    if isinstance(value, bool) or isinstance(default, bool):
        return isinstance(value, bool) and isinstance(default, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float))
    return isinstance(value, type(default))


def _validate_enum(here: Path, dotted: str, value: Any) -> None:
    allowed = _ENUMS.get(here)
    if allowed is None:
        return
    values = value if isinstance(value, list) else [value]
    bad = [v for v in values if v not in allowed]
    if bad or (isinstance(value, list) and not value):
        raise ConfigError(f"{dotted} 只能取 {sorted(allowed, key=str)}，实际是 {value}")


_IAM_ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]+$")


def _validate_special(here: Path, dotted: str, value: Any) -> None:
    """有特殊格式要求的字段。"""
    if here == ("compliance", "tags"):
        bad = [k for k, v in value.items() if not isinstance(k, str) or not isinstance(v, str)]
        if bad:
            raise ConfigError(f"{dotted} 的 key 和 value 都必须是字符串：{bad}")
    elif here == ("compliance", "extra_admin_role_arns"):
        bad = [v for v in value if not isinstance(v, str) or not _IAM_ROLE_ARN.match(v)]
        if bad:
            raise ConfigError(f"{dotted} 里有不是 IAM 角色 ARN 的值：{bad}")
    elif here == ("network", "cidr"):
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise ConfigError(f"{dotted} 不是合法的 CIDR：{value}") from exc
        if network.version != 4 or not 16 <= network.prefixlen <= 17:
            raise ConfigError(
                f"{dotted} 必须是 /16 或 /17 的 IPv4 网段（2 个 /19 私有子网 + 2 个 /24 公有子网）"
            )


def _validate_range(here: Path, dotted: str, value: Any) -> None:
    bounds = _RANGES.get(here)
    if bounds and not (bounds[0] <= value <= bounds[1]):
        raise ConfigError(f"{dotted} 应该在 {bounds[0]}~{bounds[1]} 之间，实际是 {value}")


def load_advanced(path: Optional[str]) -> Dict[str, Any]:
    """读取 config.yaml 并和默认值合并。不传路径时直接返回默认值。"""
    if not path:
        return deep_merge(DEFAULTS, {})
    override = _read_yaml(path)
    validate(override)
    return deep_merge(DEFAULTS, override)


def _read_yaml(path: str) -> Mapping[str, Any]:
    try:
        import yaml  # CloudShell 一般自带；只有用 config.yaml 时才需要
    except ImportError as exc:
        raise ConfigError("读取 config.yaml 需要 PyYAML：pip3 install --user pyyaml") from exc
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except OSError as exc:
        raise ConfigError(f"无法读取 {path}：{exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} 不是合法的 YAML：{exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"{path} 的顶层应该是一个对象")
    return data
