"""vLLM 编译缓存（torch.compile / Inductor / Triton 产物，约 250 MB，压缩后约 33 MB）。

冷启动里 torch.compile 约 31 s，首次 profiling 里的 kernel JIT 约 60 s；有缓存时两者合计约 1 s（docs/03 §9.4）。

- 读：vLLM Pod 的 initContainer（复用 vLLM 镜像，不多拉镜像）从 S3 恢复到 CACHE_ROOT；没有缓存就冷编译。
- 写：缓存里有生成的代码，被投毒等于在之后所有 vLLM Pod 里执行代码。所以 vLLM 的角色只读；
  由 qwenctl 用部署者自己的凭证，在 serving 阶段从刚就绪的 Pod 里打包上传（installer._capture_compile_caches）。
- key：镜像 digest、模型 revision、profile（决定 GPU 型号）、vLLM 参数、缓存格式版本的哈希。
  任何一个变了就是新 key，旧缓存不会被误用。
- 上传前（check_archive）和恢复时（assets/compile_cache_restore.py）用同一套规则和上限：
  只能是普通文件和目录、不能指向目录外、数量和大小有上限。恢复脚本是独立文件（挂进 vLLM 镜像），
  上限经环境变量传过去，这里是唯一的来源。
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import posixpath
import tarfile
from typing import Any, Dict, List, Mapping, Optional, Sequence

Manifest = Dict[str, Any]

# 和 serving.vllm_deployment 共用：emptyDir 卷名、挂载点、vLLM 容器名
VOLUME = "cache"
MOUNT = "/models"
CACHE_ROOT = f"{MOUNT}/vllm-cache"
VLLM_CONTAINER = "vllm"

ANNOTATION = "qwenctl/compile-cache"
ARCHIVE = "vllm-cache.tar.gz"
# 缓存的打包方式或内容约定变了就加 1，让所有旧缓存失效
FORMAT = 1
# 正常规模：约 1 千个文件、解压后约 250 MB、压缩后约 33 MB
MAX_ARCHIVE_BYTES = 512 * 1024**2
MAX_MEMBERS = 50_000
MAX_UNPACKED_BYTES = 1024**3

RESTORE_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "assets", "compile_cache_restore.py"
)
SCRIPT_VOLUME = "compile-cache-restore"
SCRIPT_MOUNT = "/scripts"

# kubectl exec 把远端 tar 的退出码透传回来：1 = 有文件在读的时候变了（见 capture_command）
CAPTURE_OK_RETURNCODES = (0, 1)


def cache_key(*, prefix: str, profile: str, image: str, revision: str, args: Sequence[str]) -> str:
    material = {
        "format": FORMAT,
        "image": image,
        "revision": revision,
        "profile": profile,
        "args": list(args),
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]
    return f"{prefix}{profile}/{digest}/{ARCHIVE}"


@functools.lru_cache(maxsize=1)
def _script() -> str:
    with open(RESTORE_SCRIPT, encoding="utf-8") as handle:
        return handle.read()


def configmap_name() -> str:
    """内容变了就换名字：已有 Pod 引用的旧 ConfigMap 不会被改掉。"""
    return f"compile-cache-restore-{hashlib.sha256(_script().encode()).hexdigest()[:8]}"


def configmap(namespace: str) -> Manifest:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": configmap_name(), "namespace": namespace},
        "data": {"compile_cache_restore.py": _script()},
    }


def restore_container(
    *, image: str, bucket: str, key: str, region: str, security_context: Mapping[str, Any]
) -> Manifest:
    env = {
        "AWS_REGION": region,
        "QWENCTL_CACHE_MAX_BYTES": str(MAX_ARCHIVE_BYTES),
        "QWENCTL_CACHE_MAX_MEMBERS": str(MAX_MEMBERS),
        "QWENCTL_CACHE_MAX_UNPACKED": str(MAX_UNPACKED_BYTES),
    }
    return {
        "name": "restore-compile-cache",
        "image": image,  # 和 vLLM 同一个镜像：节点上已经有了
        "command": ["python3", f"{SCRIPT_MOUNT}/compile_cache_restore.py", bucket, key, CACHE_ROOT],
        "env": [{"name": k, "value": v} for k, v in env.items()],
        "resources": {"requests": {"cpu": "1", "memory": "512Mi"}, "limits": {"memory": "1Gi"}},
        "securityContext": dict(security_context),
        "volumeMounts": [
            {"name": VOLUME, "mountPath": MOUNT},
            {"name": SCRIPT_VOLUME, "mountPath": SCRIPT_MOUNT, "readOnly": True},
        ],
    }


def restore_volume() -> Manifest:
    return {"name": SCRIPT_VOLUME, "configMap": {"name": configmap_name()}}


def capture_command(pod: str) -> List[str]:
    """kubectl 参数：在 vLLM 容器里打包缓存目录，输出到 stdout。

    vLLM 可能还在写缓存目录：GNU tar 遇到「读的时候文件变了」会退出 1，但压缩包本身完整，
    交给 check_archive 判断（截断的包会被它拒绝）。"""
    tar = ["tar", "czf", "-", "--warning=no-file-changed", "-C", CACHE_ROOT, "."]
    return ["exec", pod, "-c", VLLM_CONTAINER, "--", *tar]


def _member_problem(member: tarfile.TarInfo) -> Optional[str]:
    """和恢复脚本 Limits 的规则一致。"""
    normalized = posixpath.normpath(member.name)
    if member.name.startswith("/") or normalized == ".." or normalized.startswith("../"):
        return f"{member.name!r} 指向缓存目录之外"
    if not (member.isfile() or member.isdir()):
        return f"{member.name!r} 不是普通文件或目录"  # Inductor / Triton 缓存里不会有链接、设备文件
    return None


def check_archive(path: str) -> Optional[str]:
    """上传前的最后一道检查（由部署者执行）：只看成员列表，不解压。有问题返回原因。"""
    members, unpacked = 0, 0
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                problem = _member_problem(member)
                if problem:
                    return problem
                members, unpacked = members + 1, unpacked + member.size
                if members > MAX_MEMBERS or unpacked > MAX_UNPACKED_BYTES:
                    return f"超过上限（{members} 个文件 / {unpacked} 字节）"
    except (tarfile.TarError, OSError, EOFError) as exc:
        return f"不是有效的 tar.gz（{exc}）"
    return None
