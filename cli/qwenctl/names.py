"""Kubernetes 名称（DNS-1123 label）。这些名字会拼进 K8s 清单和 PromQL，校验同时承担防注入的作用。"""

from __future__ import annotations

import re
from typing import Any

K8S_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def is_k8s_name(value: Any) -> bool:
    return isinstance(value, str) and K8S_NAME.fullmatch(value) is not None
