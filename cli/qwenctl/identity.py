"""调用者身份 → 授予 EKS 集群管理员的 IAM principal ARN。"""

from __future__ import annotations

import re
from typing import Optional

_ASSUMED_ROLE = re.compile(
    r"^arn:(?P<partition>[\w-]+):sts::(?P<account>\d{12}):"
    r"assumed-role/(?P<role>[\w+=,.@-]+)/(?P<session>.+)$"
)
_ROOT = re.compile(r"^arn:[\w-]+:iam::\d{12}:root$")


class IdentityError(Exception):
    """当前身份不能用于部署。"""


def assumed_role_name(caller_arn: str) -> Optional[str]:
    match = _ASSUMED_ROLE.match(caller_arn)
    return match.group("role") if match else None


def principal_arn(caller_arn: str, resolved_role_arn: Optional[str] = None) -> str:
    """assumed-role 的 session ARN 要换成 role ARN，EKS access entry 才能识别。

    resolved_role_arn 是通过 iam:GetRole 查到的完整 ARN（带 path）；查不到时去掉 path 拼一个。
    """
    if _ROOT.match(caller_arn):
        raise IdentityError("请不要用 root 用户部署，改用 IAM 角色或 IAM 用户登录控制台")
    if ":federated-user/" in caller_arn:
        raise IdentityError("EKS 不支持把 federated user 设为集群管理员，请用 IAM 角色登录控制台")
    match = _ASSUMED_ROLE.match(caller_arn)
    if not match:
        return caller_arn
    if resolved_role_arn:
        return resolved_role_arn
    return f"arn:{match.group('partition')}:iam::{match.group('account')}:role/{match.group('role')}"
