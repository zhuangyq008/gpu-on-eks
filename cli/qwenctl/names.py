"""Kubernetes 名称（DNS-1123 label）。这些名字会拼进 K8s 清单和 PromQL，校验同时承担防注入的作用。"""

from __future__ import annotations

import re
from typing import Any

K8S_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def is_k8s_name(value: Any) -> bool:
    return isinstance(value, str) and K8S_NAME.fullmatch(value) is not None


# Route 53 托管区域：和 infra/lib/config.ts 的 hostedZoneSchema 一致（两边都校验，口径必须相同）
HOSTED_ZONE_ID = re.compile(r"^Z[0-9A-Z]{1,32}$")
DOMAIN_NAME = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def is_hosted_zone(zone_id: Any, name: Any) -> bool:
    """区域 ID 和名字会拼进 IAM ARN 和 ExternalDNS 的配置。"""
    return (
        isinstance(zone_id, str)
        and isinstance(name, str)
        and len(name) <= 253
        and HOSTED_ZONE_ID.fullmatch(zone_id) is not None
        and DOMAIN_NAME.fullmatch(name) is not None
    )
