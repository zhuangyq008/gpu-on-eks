"""本地文件位置：~/.qwenctl/（CloudShell 的 $HOME 跨会话保留）。

每个部署一个目录：~/.qwenctl/<name>/{config.json, outputs.json, kubeconfig, state.json}
工具放在 ~/.qwenctl/bin/（按版本命名，多个部署共用）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INFRA_DIR = os.path.join(REPO_ROOT, "infra")
VERSIONS_FILE = os.path.join(REPO_ROOT, "versions.json")


def qwenctl_home(home: Optional[str] = None) -> str:
    return os.path.join(home or os.path.expanduser("~"), ".qwenctl")


def bin_dir(home: Optional[str] = None) -> str:
    return os.path.join(qwenctl_home(home), "bin")


@dataclass(frozen=True)
class DeploymentPaths:
    root: str

    @classmethod
    def for_name(cls, name: str, home: Optional[str] = None) -> DeploymentPaths:
        return cls(os.path.join(qwenctl_home(home), name))

    @property
    def config(self) -> str:
        return os.path.join(self.root, "config.json")

    @property
    def outputs(self) -> str:
        return os.path.join(self.root, "outputs.json")

    @property
    def kubeconfig(self) -> str:
        return os.path.join(self.root, "kubeconfig")

    @property
    def state(self) -> str:
        return os.path.join(self.root, "state.json")

    def ensure(self) -> DeploymentPaths:
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        # makedirs 的 mode 只作用于最后一级、也不会收紧已有目录：显式设置
        for directory in (os.path.dirname(self.root), self.root):
            os.chmod(directory, 0o700)
        return self


def write_private(path: str, text: str) -> None:
    """只有本人可读写（0600），原子替换。临时文件用 O_EXCL 新建，不复用残留文件、不跟随符号链接。"""
    tmp = f"{path}.tmp"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)
    os.replace(tmp, path)
