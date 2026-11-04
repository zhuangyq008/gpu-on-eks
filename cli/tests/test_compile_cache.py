"""编译缓存：key 计算（qwenctl/compile_cache.py）和 initContainer 里的恢复脚本（assets/compile_cache_restore.py）。"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import tarfile

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ResponseStreamingError

from qwenctl import compile_cache

pytestmark = pytest.mark.skipif(
    not hasattr(tarfile, "data_filter"),
    reason="tarfile 解压过滤器要 Python 3.9.17+ / 3.12（vLLM 镜像是 3.12）",
)

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "qwenctl", "assets", "compile_cache_restore.py")
spec = importlib.util.spec_from_file_location("compile_cache_restore", SCRIPT)
restore_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore_script)


def _archive(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeS3:
    def __init__(self, body=None, error=None, size=None):
        self.body, self.error, self.size = body, error, size
        self.calls = []

    def get_object(self, Bucket, Key):  # 和 boto3 的参数名一致
        self.calls.append((Bucket, Key))
        if self.error:
            raise self.error
        return {"Body": io.BytesIO(self.body), "ContentLength": self.size or len(self.body)}


def _missing():
    return ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")


def _real_tar(tmp_path):
    """和集群里一样用 GNU tar 打包（成员名带 ./ 前缀，还有目录成员）。"""
    source = tmp_path / "source"
    (source / "torch_compile_cache" / "abc").mkdir(parents=True)
    (source / "torch_compile_cache" / "abc" / "model.py").write_bytes(b"compiled")
    (source / "modelinfos").mkdir()
    (source / "modelinfos" / "x.json").write_text("{}")
    archive = tmp_path / "cache.tar.gz"
    tar = ["tar", "czf", str(archive), "--warning=no-file-changed", "-C", str(source), "."]
    subprocess.run(tar, check=True)
    return archive.read_bytes()


def test_real_tar_output_passes_upload_check_and_restores(tmp_path):
    body = _real_tar(tmp_path)
    assert compile_cache.check_archive(str(tmp_path / "cache.tar.gz")) is None
    root = tmp_path / "restored"
    message = restore_script.restore(
        FakeS3(body), "bucket", "compile-cache/g6/k/vllm-cache.tar.gz", str(root), {}
    )
    assert "已恢复编译缓存" in message
    assert (root / "torch_compile_cache" / "abc" / "model.py").read_bytes() == b"compiled"


class _BrokenStream(io.BytesIO):
    """读到一半网络断开（botocore 的流式错误）。"""

    def read(self, size=-1):
        if self.tell() > 200:
            raise ResponseStreamingError(error="connection reset")
        return super().read(min(size, 64) if size and size > 0 else 64)


def test_network_drop_mid_stream_leaves_a_clean_directory(tmp_path):
    files = {f"torch_compile_cache/f{i}.py": os.urandom(4096) for i in range(8)}
    s3 = FakeS3(_archive(files))
    s3.get_object = lambda Bucket, Key: {"Body": _BrokenStream(_archive(files)), "ContentLength": 1}
    root = tmp_path / "cache"
    message = restore_script.restore(s3, "bucket", "key", str(root), {})
    assert "无法恢复" in message and "ResponseStreamingError" in message
    assert os.listdir(root) == []  # 截断的产物不能留给 vLLM


def test_restore_limits_come_from_qwenctl(tmp_path):
    """恢复脚本的上限由 qwenctl 经环境变量传入：和上传前的检查是同一份。"""
    init = compile_cache.restore_container(image="i", bucket="b", key="k", region="r", security_context={})
    env = {e["name"]: e["value"] for e in init["env"]}
    assert int(env["QWENCTL_CACHE_MAX_BYTES"]) == compile_cache.MAX_ARCHIVE_BYTES
    assert int(env["QWENCTL_CACHE_MAX_MEMBERS"]) == compile_cache.MAX_MEMBERS
    assert int(env["QWENCTL_CACHE_MAX_UNPACKED"]) == compile_cache.MAX_UNPACKED_BYTES


def _special(kind, name="torch_compile_cache/x", linkname=""):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.type, info.linkname = kind, linkname
        tar.addfile(info)
    return buffer.getvalue()


MALICIOUS = {
    "上一级目录": _archive({"../escape.py": b"x"}),
    "绝对路径": _archive({"/etc/cron.d/x": b"x"}),
    "符号链接（指向内部）": _special(tarfile.SYMTYPE, linkname="abc/model.py"),
    "符号链接（指向外部）": _special(tarfile.SYMTYPE, linkname="/etc/passwd"),
    "硬链接": _special(tarfile.LNKTYPE, linkname="abc/model.py"),
    "设备文件": _special(tarfile.CHRTYPE),
    "FIFO": _special(tarfile.FIFOTYPE),
}


@pytest.mark.parametrize("case", sorted(MALICIOUS))
def test_upload_check_and_restore_reject_the_same_archives(tmp_path, case):
    body = MALICIOUS[case]
    archive = tmp_path / "a.tgz"
    archive.write_bytes(body)
    assert compile_cache.check_archive(str(archive)) is not None
    root = tmp_path / "cache"
    assert "无法恢复" in restore_script.restore(FakeS3(body), "bucket", "key", str(root), {})
    assert os.listdir(root) == [] and not (tmp_path / "escape.py").exists()


@pytest.mark.parametrize("error", [_missing(), ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")])
def test_missing_or_forbidden_cache_falls_back_to_cold_compile(tmp_path, error):
    message = restore_script.restore(FakeS3(error=error), "bucket", "key", str(tmp_path / "cache"), {})
    assert "vLLM 会冷编译" in message and error.response["Error"]["Code"] in message
    assert os.listdir(tmp_path / "cache") == []


def test_network_error_falls_back(tmp_path):
    error = EndpointConnectionError(endpoint_url="https://s3.ap-southeast-5.amazonaws.com")
    assert "冷编译" in restore_script.restore(FakeS3(error=error), "bucket", "key", str(tmp_path), {})


def test_path_traversal_is_rejected_and_cache_cleared(tmp_path):
    root = tmp_path / "cache"
    body = _archive({"ok.txt": b"fine", "../escaped.txt": b"evil"})
    message = restore_script.restore(FakeS3(body), "bucket", "key", str(root), {})
    assert "无法恢复" in message
    assert not (tmp_path / "escaped.txt").exists()
    assert os.listdir(root) == []  # 解压到一半的内容也清掉，vLLM 从干净状态冷编译


def test_corrupt_archive_is_cleared(tmp_path):
    message = restore_script.restore(FakeS3(b"not a gzip"), "bucket", "key", str(tmp_path), {})
    assert "无法恢复" in message and os.listdir(tmp_path) == []


def test_oversized_object_is_ignored(tmp_path):
    s3 = FakeS3(_archive({"a": b"1"}), size=101)
    assert "大小异常" in restore_script.restore(
        s3, "bucket", "key", str(tmp_path), {"QWENCTL_CACHE_MAX_BYTES": "100"}
    )
    assert os.listdir(tmp_path) == []


def test_main_never_fails_the_init_container(tmp_path, monkeypatch, capsys):
    import boto3

    def broken_client(*_args, **_kwargs):
        raise RuntimeError("no credentials provider")

    monkeypatch.setattr(boto3, "client", broken_client)
    assert restore_script.main(["x", "bucket", "key", str(tmp_path)]) == 0
    assert "vLLM 会冷编译" in capsys.readouterr().out


def test_cache_key_is_stable_and_scoped_by_profile():
    args = ["s3://b/models/", "--max-num-seqs=16"]

    def key(**overrides):
        fields = {"prefix": "compile-cache/", "profile": "g6-tp2", "image": "img@sha256:1", "revision": "rev"}
        return compile_cache.cache_key(**{**fields, **overrides}, args=args)

    assert (
        key() == key() and key().startswith("compile-cache/g6-tp2/") and key().endswith("/vllm-cache.tar.gz")
    )
    assert key(revision="rev2") != key() and key(profile="g7e-1gpu") != key()


def test_capture_runs_tar_inside_the_vllm_container():
    command = compile_cache.capture_command("vllm-g6-tp2-abc-xyz")
    assert command[:4] == ["exec", "vllm-g6-tp2-abc-xyz", "-c", "vllm"]
    assert "--warning=no-file-changed" in command
    assert command[-3:] == ["-C", "/models/vllm-cache", "."]


def _archive_with_link(kind):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("torch_compile_cache/link")
        info.type = kind
        info.linkname = "abc/model.py"  # 指向缓存目录内部，data 过滤器本身会放行
        tar.addfile(info)
    return buffer.getvalue()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_links_are_rejected_even_inside_the_cache_dir(tmp_path, kind):
    """缓存里不会有链接；tarfile data 过滤器已知的绕过（CVE-2025-4517 等）都和链接有关。"""
    message = restore_script.restore(FakeS3(_archive_with_link(kind)), "bucket", "key", str(tmp_path), {})
    assert "无法恢复" in message and os.listdir(tmp_path) == []
    assert compile_cache.check_archive(_write(tmp_path / "a.tgz", _archive_with_link(kind))) is not None


def _write(path, data):
    path.write_bytes(data)
    return str(path)


def test_unpacked_size_limit_applies_on_restore(tmp_path):
    body = _archive({"a": b"x" * 6, "b": b"y" * 6})
    env = {"QWENCTL_CACHE_MAX_UNPACKED": "10"}
    message = restore_script.restore(FakeS3(body), "bucket", "key", str(tmp_path), env)
    assert "超过上限" in message and os.listdir(tmp_path) == []


def test_member_count_limit_applies_before_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(compile_cache, "MAX_MEMBERS", 2)
    path = _write(tmp_path / "a.tgz", _archive({"a": b"1", "b": b"2", "c": b"3"}))
    assert "超过上限" in compile_cache.check_archive(path)


def test_clean_archive_passes_upload_check(tmp_path):
    path = _write(
        tmp_path / "a.tgz", _archive({"torch_compile_cache/abc/model.py": b"x", "modelinfos/m.json": b"{}"})
    )
    assert compile_cache.check_archive(path) is None
    assert "不是有效的 tar.gz" in compile_cache.check_archive(_write(tmp_path / "bad.tgz", b"nope"))
