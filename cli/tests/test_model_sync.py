"""assets/model_sync.py（在集群 Job 里运行）的完整性校验逻辑：用假的 S3 和假的 HF 下载。"""

from __future__ import annotations

import importlib.util
import json
import os
from types import SimpleNamespace
from typing import Dict, Optional

import pytest
from botocore.exceptions import ClientError

# 这个脚本在 vLLM 镜像里运行，huggingface_hub 不是 qwenctl（CloudShell 端）的依赖
pytest.importorskip("huggingface_hub")

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "qwenctl", "assets", "model_sync.py")
spec = importlib.util.spec_from_file_location("model_sync", SCRIPT)
model_sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model_sync)

HELLO = b"hello\n"
HELLO_SHA256 = "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"
HELLO_GIT_SHA1 = "ce013625030ba8dba906f756967f9e9ca394464a"  # git hash-object 的结果


class FakeS3:
    def __init__(self, objects: Optional[Dict[str, dict]] = None) -> None:
        self.objects = objects or {}
        self.uploads = []

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        obj = self.objects[Key]
        return {"ContentLength": obj["size"], "Metadata": obj.get("meta", {})}

    def upload_file(self, local, bucket, key, ExtraArgs, Config):
        with open(local, "rb") as handle:
            data = handle.read()
        self.uploads.append(key)
        self.objects[key] = {"size": len(data), "meta": ExtraArgs["Metadata"]}

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = {"size": len(Body), "body": Body}


def _sibling(name, content=HELLO, lfs=True, sha256=HELLO_SHA256):
    return SimpleNamespace(
        rfilename=name,
        size=len(content),
        lfs=SimpleNamespace(sha256=sha256) if lfs else None,
        blob_id=None if lfs else HELLO_GIT_SHA1,
    )


@pytest.fixture
def fake_download(monkeypatch, tmp_path):
    def download(model, name, revision, local_dir):
        path = os.path.join(local_dir, name)
        with open(path, "wb") as handle:
            handle.write(HELLO)
        return path

    monkeypatch.setattr(model_sync, "hf_hub_download", download)


def test_hashes_match_git_and_sha256(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(HELLO)
    assert model_sync.file_hashes(str(path)) == {"sha256": HELLO_SHA256, "git-sha1": HELLO_GIT_SHA1}


def test_expected_hash_prefers_lfs_sha256():
    assert model_sync.expected_hash(_sibling("a.safetensors")) == ("sha256", HELLO_SHA256)
    assert model_sync.expected_hash(_sibling("config.json", lfs=False)) == ("git-sha1", HELLO_GIT_SHA1)
    with pytest.raises(model_sync.IntegrityError):
        model_sync.expected_hash(SimpleNamespace(rfilename="x", lfs=None, blob_id=None))


def test_verified_upload_records_hash_metadata(tmp_path, fake_download):
    s3 = FakeS3()
    for sib in (_sibling("model.safetensors"), _sibling("config.json", lfs=False)):
        sha, copied = model_sync.sync_file(s3, "b", "p/", "m", "r", sib, str(tmp_path))
        assert sha == HELLO_SHA256 and copied == len(HELLO)
    meta = s3.objects["p/config.json"]["meta"]
    assert meta == {"sha256": HELLO_SHA256, "expected-hash": HELLO_GIT_SHA1, "hash-algorithm": "git-sha1"}
    assert not os.listdir(tmp_path)  # 上传后删除本地副本


def test_hash_mismatch_fails_without_uploading(tmp_path, fake_download):
    s3 = FakeS3()
    with pytest.raises(model_sync.IntegrityError, match="校验失败"):
        model_sync.sync_file(
            s3, "b", "p/", "m", "r", _sibling("bad.safetensors", sha256="0" * 64), str(tmp_path)
        )
    assert s3.uploads == []


def test_already_verified_object_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(model_sync, "hf_hub_download", lambda *a, **k: pytest.fail("不应该重新下载"))
    s3 = FakeS3(
        {
            "p/model.safetensors": {
                "size": len(HELLO),
                "meta": {"expected-hash": HELLO_SHA256, "sha256": HELLO_SHA256},
            }
        }
    )
    assert model_sync.sync_file(s3, "b", "p/", "m", "r", _sibling("model.safetensors"), str(tmp_path)) == (
        HELLO_SHA256,
        0,
    )


def test_same_size_but_unverified_object_is_replaced(tmp_path, fake_download):
    s3 = FakeS3({"p/model.safetensors": {"size": len(HELLO), "meta": {}}})
    model_sync.sync_file(s3, "b", "p/", "m", "r", _sibling("model.safetensors"), str(tmp_path))
    assert s3.uploads == ["p/model.safetensors"]


def test_main_writes_manifest_and_skips_when_complete(monkeypatch, fake_download):
    siblings = [_sibling(".gitattributes"), _sibling("model.safetensors"), _sibling("config.json", lfs=False)]
    monkeypatch.setattr(
        model_sync,
        "HfApi",
        lambda: SimpleNamespace(model_info=lambda *a, **k: SimpleNamespace(siblings=siblings)),
    )
    s3 = FakeS3()
    monkeypatch.setattr(model_sync.boto3, "client", lambda *a, **k: s3)
    for key, value in {"MODEL_ID": "m", "MODEL_REVISION": "rev", "S3_BUCKET": "b", "S3_PREFIX": "p/"}.items():
        monkeypatch.setenv(key, value)
    assert model_sync.main() == 0
    manifest = json.loads(s3.objects["p/_COMPLETE"]["body"])
    assert set(manifest["files"]) == {"model.safetensors", "config.json"}
    assert manifest["files"]["config.json"]["sha256"] == HELLO_SHA256
    uploads = len(s3.uploads)
    assert model_sync.main() == 0 and len(s3.uploads) == uploads  # 已完成：直接跳过
