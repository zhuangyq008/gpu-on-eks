"""把部署配置保存到本地（~/.qwenctl/<name>/config.json，CloudShell 会跨会话保留）。"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Optional

from .models import DeploymentConfig
from .paths import DeploymentPaths


def default_path(name: str) -> str:
    return DeploymentPaths.for_name(name).config


def save_config(config: DeploymentConfig, path: Optional[str] = None) -> str:
    """原子写入：先写同目录的临时文件，再 rename。中途中断不会留下半截 JSON。"""
    target = os.path.abspath(path or default_path(config.name))
    directory = os.path.dirname(target)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    payload = json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".qwenctl-", suffix=".json", dir=directory)  # 权限 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return target
