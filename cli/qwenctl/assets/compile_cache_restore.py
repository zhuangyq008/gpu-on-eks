"""vLLM 的 initContainer：从 S3 恢复编译缓存（torch.compile / Inductor / Triton 产物）到 VLLM_CACHE_ROOT。

在 vLLM 镜像里运行（自带 boto3），用 vLLM 的 Pod Identity 角色（对缓存前缀只读）。
任何失败（没有缓存、没权限、网络中断、压缩包损坏、超过上限）都只打日志、清空目录、退出 0：
vLLM 从干净的状态冷编译，不能挡住启动。

只放行普通文件和目录（缓存里不会有链接；tarfile data 过滤器已知的绕过都和链接有关），
路径不能指向目录外，成员数和解压后大小有上限。上限由 qwenctl 通过环境变量传入（和上传前的检查用同一份）。

用法：python3 compile_cache_restore.py <bucket> <key> <cache_root>
"""

from __future__ import annotations

import contextlib
import os
import posixpath
import shutil
import sys
import tarfile
import time
from typing import Any, Mapping, Sequence


def _limit(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, default))
    except ValueError:
        return default


class Limits:
    """tarfile 的解压过滤器：在 data 过滤器之上只放行普通文件和目录，并累计数量和大小。"""

    def __init__(self, max_members: int, max_unpacked: int) -> None:
        self.max_members, self.max_unpacked = max_members, max_unpacked
        self.members, self.unpacked = 0, 0

    def __call__(self, member: tarfile.TarInfo, path: str) -> tarfile.TarInfo:
        normalized = posixpath.normpath(member.name)
        if member.name.startswith("/") or normalized == ".." or normalized.startswith("../"):
            raise tarfile.TarError(f"{member.name!r} 指向缓存目录之外")
        if not (member.isfile() or member.isdir()):
            raise tarfile.TarError(f"{member.name!r} 不是普通文件或目录")
        member = tarfile.data_filter(member, path)
        self.members, self.unpacked = self.members + 1, self.unpacked + member.size
        if self.members > self.max_members or self.unpacked > self.max_unpacked:
            raise tarfile.TarError(f"超过上限（{self.members} 个文件 / {self.unpacked} 字节）")
        return member


def _clear(root: str) -> None:
    """清空目录，让 vLLM 从干净的状态冷编译。"""
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)


def restore(s3: Any, bucket: str, key: str, root: str, env: Mapping[str, str]) -> str:
    os.makedirs(root, exist_ok=True)
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 —— NoSuchKey、AccessDenied、网络：都退回冷编译
        code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code") or type(exc).__name__
        return f"没有可用的编译缓存（{code}），vLLM 会冷编译"
    max_bytes = _limit(env, "QWENCTL_CACHE_MAX_BYTES", 512 * 1024**2)
    limits = Limits(
        _limit(env, "QWENCTL_CACHE_MAX_MEMBERS", 50_000), _limit(env, "QWENCTL_CACHE_MAX_UNPACKED", 1024**3)
    )
    with contextlib.closing(obj["Body"]):
        size = int(obj.get("ContentLength") or 0)
        if size > max_bytes:
            return f"编译缓存大小异常（{size} 字节），不使用，vLLM 会冷编译"
        try:
            with tarfile.open(fileobj=obj["Body"], mode="r|gz") as archive:
                archive.extractall(root, filter=limits)
        except Exception as exc:  # noqa: BLE001 —— 包括读到一半网络断开（botocore 的流式错误）
            _clear(root)
            return f"编译缓存无法恢复（{type(exc).__name__}: {exc}），已清空，vLLM 会冷编译"
    return f"已恢复编译缓存（{size / 1e6:.0f} MB，{limits.members} 个文件）"


def main(argv: Sequence[str]) -> int:
    started = time.time()
    try:
        bucket, key, root = argv[1:4]
        import boto3  # 只在 vLLM 镜像里需要

        client = boto3.client("s3", region_name=os.environ.get("AWS_REGION"))
        message = restore(client, bucket, key, root, os.environ)
    except Exception as exc:  # noqa: BLE001 —— 任何意外都不能挡住 vLLM 启动，打日志后冷编译
        message = f"恢复编译缓存时出错（{type(exc).__name__}: {exc}），vLLM 会冷编译"
    print(f"{message}，用时 {time.time() - started:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
