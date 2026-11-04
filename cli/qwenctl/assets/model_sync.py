"""在集群内 Job 里运行：把 HF 上锁定版本（revision）的模型同步到 S3，并逐个文件校验完整性。

- 运行在 vLLM 镜像里（自带 huggingface_hub + hf_xet + boto3），不在运行时 pip install。
- 完整性：大文件（LFS）对比 HF 公布的 sha256；小文件对比 git blob sha1。不一致直接失败。
  通过校验的 sha256 写进 S3 对象元数据和 _COMPLETE 清单（审计用，配合桶版本控制可回滚）。
- 可重复执行：S3 上元数据里的 sha256 和 HF 一致的文件跳过；全部完成后写 _COMPLETE。
- 逐个文件下载 → 校验 → 上传 → 删除本地副本，本地磁盘只需要放得下最大的一个分片。
- 凭证来自 EKS Pod Identity（boto3 默认凭证链）。

环境变量：MODEL_ID、MODEL_REVISION、S3_BUCKET、S3_PREFIX（以 / 结尾）、AWS_REGION
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError
from huggingface_hub import HfApi, hf_hub_download

MARKER = "_COMPLETE"
TRANSFER = TransferConfig(multipart_chunksize=64 * 1024 * 1024, max_concurrency=16)
SKIP_FILES = {".gitattributes"}


class IntegrityError(Exception):
    pass


def expected_hash(sibling):
    """返回 (算法, 期望值)：LFS 文件用 sha256，普通文件用 git blob sha1。"""
    lfs = getattr(sibling, "lfs", None)
    if lfs is not None and getattr(lfs, "sha256", None):
        return "sha256", lfs.sha256
    if getattr(sibling, "blob_id", None):
        return "git-sha1", sibling.blob_id
    raise IntegrityError(f"HF 没有提供 {sibling.rfilename} 的校验值")


def file_hashes(path):
    sha256, size = hashlib.sha256(), os.path.getsize(path)
    blob = hashlib.sha1(f"blob {size}\0".encode())
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            sha256.update(block)
            blob.update(block)
    return {"sha256": sha256.hexdigest(), "git-sha1": blob.hexdigest()}


def remote_meta(s3, bucket, key):
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return {"size": head["ContentLength"], **head.get("Metadata", {})}


def sync_file(s3, bucket, prefix, model, revision, sibling, work):
    name = sibling.rfilename
    algorithm, expected = expected_hash(sibling)
    remote = remote_meta(s3, bucket, prefix + name)
    if remote and remote.get("expected-hash") == expected and remote.get("size") == sibling.size:
        print(f"  跳过（已校验）{name}", flush=True)
        return remote["sha256"], 0
    t0 = time.time()
    local = hf_hub_download(model, name, revision=revision, local_dir=work)
    hashes = file_hashes(local)
    if hashes[algorithm] != expected:
        raise IntegrityError(f"{name} 校验失败：{algorithm} 期望 {expected}，实际 {hashes[algorithm]}")
    t1 = time.time()
    metadata = {"sha256": hashes["sha256"], "expected-hash": expected, "hash-algorithm": algorithm}
    s3.upload_file(local, bucket, prefix + name, ExtraArgs={"Metadata": metadata}, Config=TRANSFER)
    os.remove(local)
    print(
        f"  {name}：{sibling.size / 1e9:.2f} GB，下载+校验 {t1 - t0:.0f}s，上传 {time.time() - t1:.0f}s",
        flush=True,
    )
    return hashes["sha256"], sibling.size


def main():
    model, revision = os.environ["MODEL_ID"], os.environ["MODEL_REVISION"]
    bucket, prefix = os.environ["S3_BUCKET"], os.environ["S3_PREFIX"]
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION"))
    if remote_meta(s3, bucket, prefix + MARKER) is not None:
        print(f"s3://{bucket}/{prefix} 已经完整同步并校验过，跳过", flush=True)
        return 0
    info = HfApi().model_info(model, revision=revision, files_metadata=True)
    siblings = [s for s in info.siblings if s.rfilename not in SKIP_FILES]
    total = sum(s.size or 0 for s in siblings)
    print(f"{model}@{revision[:12]}：{len(siblings)} 个文件，共 {total / 1e9:.1f} GB", flush=True)
    work = tempfile.mkdtemp(prefix="model-sync-")
    started, copied, manifest = time.time(), 0, {}
    try:
        for sibling in siblings:
            sha256, size = sync_file(s3, bucket, prefix, model, revision, sibling, work)
            manifest[sibling.rfilename] = {"size": sibling.size, "sha256": sha256}
            copied += size
    finally:
        shutil.rmtree(work, ignore_errors=True)
    body = {"model": model, "revision": revision, "files": manifest, "syncedAt": int(time.time())}
    s3.put_object(Bucket=bucket, Key=prefix + MARKER, Body=json.dumps(body, indent=1).encode())
    elapsed = time.time() - started
    rate = copied / 1e6 / max(elapsed, 1)
    print(
        f"完成：本次复制 {copied / 1e9:.1f} GB，用时 {elapsed:.0f}s（{rate:.0f} MB/s），全部文件校验通过",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except IntegrityError as exc:
        print(f"完整性校验失败：{exc}", flush=True)
        sys.exit(2)
