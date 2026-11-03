"""读取仓库根目录的 versions.json（与 CDK 共用的锁定版本）。"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .paths import VERSIONS_FILE

_REQUIRED = ("kubernetes", "bottlerocket", "eksAddons", "tools", "charts")


class VersionsError(Exception):
    """versions.json 缺失或格式不对（交付包不完整）。"""


def load_versions(path: Optional[str] = None) -> Dict[str, Any]:
    target = path or VERSIONS_FILE
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise VersionsError(f"无法读取 {target}，交付包可能不完整：{exc}") from exc
    missing = [k for k in _REQUIRED if k not in data]
    if missing:
        raise VersionsError(f"{target} 缺少字段：{', '.join(missing)}")
    return data
