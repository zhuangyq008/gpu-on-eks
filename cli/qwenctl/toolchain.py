"""客户端工具：下载 versions.json 锁定版本的 kubectl / helm 到 ~/.qwenctl/bin，并校验 sha256。

sha256 写在 versions.json 里（发布交付包时确定），不从下载源现取：下载源被篡改时也能发现。
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import tarfile
import tempfile
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional

ARCH_MAP = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 120
REQUIRED_COMMANDS = ("aws", "node", "npm")

Fetch = Callable[[str], bytes]


class ToolError(Exception):
    """工具下载、校验或安装失败。"""


def detect_arch(machine: Optional[str] = None) -> str:
    raw = (machine or platform.machine()).lower()
    if raw not in ARCH_MAP:
        raise ToolError(f"不支持的 CPU 架构：{raw}")
    return ARCH_MAP[raw]


def missing_commands(which: Callable[[str], Optional[str]] = shutil.which) -> List[str]:
    """CloudShell 都自带；在跳板机上运行时可能缺。"""
    return [c for c in REQUIRED_COMMANDS if which(c) is None]


def https_fetch(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ToolError(f"只允许 HTTPS 下载：{url}")
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp:
        data = resp.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ToolError(f"下载内容超过 {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB：{url}")
    return data


def ensure_tool(
    name: str, spec: Mapping[str, Any], target_dir: str, arch: str, fetch: Fetch = https_fetch
) -> str:
    """返回可执行文件路径。已经装过同版本、并且文件没被改过时直接复用。"""
    version = spec["version"]
    target = os.path.join(target_dir, f"{name}-{version}")
    marker = f"{target}.sha256"
    if os.path.isfile(target) and os.path.isfile(marker) and _read(marker) == _sha256_file(target):
        return target
    expected = spec["sha256"].get(arch)
    if not expected:
        raise ToolError(f"versions.json 里没有 {name} {version} 的 {arch} 校验和")
    url = spec["url"].format(version=version, arch=arch)
    data = fetch(url)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ToolError(f"{name} 校验失败：期望 {expected}，实际 {actual}（{url}）")
    member = spec.get("archiveMember")
    binary = _extract(data, member.format(arch=arch), name) if member else data
    _write_executable(target_dir, target, binary)
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write(hashlib.sha256(binary).hexdigest())
    return target


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


def ensure_tools(
    versions: Mapping[str, Any], target_dir: str, arch: str, fetch: Fetch = https_fetch
) -> Dict[str, str]:
    os.makedirs(target_dir, mode=0o700, exist_ok=True)
    for directory in (os.path.dirname(os.path.abspath(target_dir)), target_dir):
        os.chmod(directory, 0o700)
    return {
        name: ensure_tool(name, spec, target_dir, arch, fetch) for name, spec in versions["tools"].items()
    }


def _extract(archive: bytes, member: str, name: str) -> bytes:
    """只取指定的那个文件，不做通用解压（避免路径穿越）。"""
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            info = tar.getmember(member)
            if not info.isfile():
                raise ToolError(f"{name} 压缩包里的 {member} 不是普通文件")
            handle = tar.extractfile(info)
            if handle is None:
                raise ToolError(f"无法读取 {name} 压缩包里的 {member}")
            return handle.read()
    except (tarfile.TarError, KeyError) as exc:
        raise ToolError(f"{name} 压缩包格式不对：{exc}") from exc


def _write_executable(directory: str, target: str, content: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tool-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
