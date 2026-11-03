import hashlib
import io
import os
import tarfile
from unittest.mock import MagicMock, patch

import pytest

from qwenctl import toolchain
from qwenctl.toolchain import ToolError, detect_arch, ensure_tool, ensure_tools, https_fetch, missing_commands

KUBECTL_BYTES = b"#!/bin/sh\necho kubectl\n"


def _tarball(member: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(member)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


HELM_TAR = _tarball("linux-amd64/helm", b"#!/bin/sh\necho helm\n")
SPECS = {
    "kubectl": {
        "version": "v1.36.6",
        "url": "https://dl.k8s.io/release/{version}/bin/linux/{arch}/kubectl",
        "sha256": {"amd64": hashlib.sha256(KUBECTL_BYTES).hexdigest()},
    },
    "helm": {
        "version": "v3.22.1",
        "url": "https://get.helm.sh/helm-{version}-linux-{arch}.tar.gz",
        "archiveMember": "linux-{arch}/helm",
        "sha256": {"amd64": hashlib.sha256(HELM_TAR).hexdigest()},
    },
}


class FakeFetch:
    def __init__(self, payloads):
        self.payloads = payloads
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return self.payloads[url]


PAYLOADS = {
    "https://dl.k8s.io/release/v1.36.6/bin/linux/amd64/kubectl": KUBECTL_BYTES,
    "https://get.helm.sh/helm-v3.22.1-linux-amd64.tar.gz": HELM_TAR,
}


@pytest.mark.parametrize("machine,arch", [("x86_64", "amd64"), ("aarch64", "arm64"), ("AMD64", "amd64")])
def test_detect_arch(machine, arch):
    assert detect_arch(machine) == arch


def test_detect_arch_rejects_unknown():
    with pytest.raises(ToolError, match="不支持的 CPU 架构"):
        detect_arch("riscv64")


def test_missing_commands():
    assert missing_commands(lambda c: None if c == "node" else f"/usr/bin/{c}") == ["node"]


def test_installs_verified_binaries_and_reuses_them(tmp_path):
    fetch = FakeFetch(PAYLOADS)
    tools = ensure_tools({"tools": SPECS}, str(tmp_path / "bin"), "amd64", fetch)
    assert tools["kubectl"].endswith("kubectl-v1.36.6")
    with open(tools["helm"], "rb") as handle:
        assert handle.read() == b"#!/bin/sh\necho helm\n"
    assert os.access(tools["kubectl"], os.X_OK)
    assert ensure_tools({"tools": SPECS}, str(tmp_path / "bin"), "amd64", fetch) == tools
    assert len(fetch.urls) == 2  # 第二次直接复用


def test_checksum_mismatch_is_rejected_and_nothing_written(tmp_path):
    fetch = FakeFetch({**PAYLOADS, "https://dl.k8s.io/release/v1.36.6/bin/linux/amd64/kubectl": b"tampered"})
    with pytest.raises(ToolError, match="kubectl 校验失败"):
        ensure_tool("kubectl", SPECS["kubectl"], str(tmp_path), "amd64", fetch)
    assert os.listdir(tmp_path) == []


def test_missing_arch_checksum(tmp_path):
    with pytest.raises(ToolError, match="arm64 校验和"):
        ensure_tool("kubectl", SPECS["kubectl"], str(tmp_path), "arm64", FakeFetch(PAYLOADS))


def test_archive_without_expected_member(tmp_path):
    bad = _tarball("other/helm", b"x")
    spec = {**SPECS["helm"], "sha256": {"amd64": hashlib.sha256(bad).hexdigest()}}
    fetch = FakeFetch({"https://get.helm.sh/helm-v3.22.1-linux-amd64.tar.gz": bad})
    with pytest.raises(ToolError, match="压缩包格式不对"):
        ensure_tool("helm", spec, str(tmp_path), "amd64", fetch)


def test_https_only():
    with pytest.raises(ToolError, match="只允许 HTTPS"):
        https_fetch("http://example.com/kubectl")


def test_download_size_limit(monkeypatch):
    monkeypatch.setattr(toolchain, "MAX_DOWNLOAD_BYTES", 4)
    response = MagicMock()
    response.read.return_value = b"12345"
    response.__enter__.return_value = response
    with (
        patch("qwenctl.toolchain.urllib.request.urlopen", return_value=response),
        pytest.raises(ToolError, match="下载内容超过"),
    ):
        https_fetch("https://example.com/big")
    response.read.return_value = b"1234"
    with patch("qwenctl.toolchain.urllib.request.urlopen", return_value=response):
        assert https_fetch("https://example.com/ok") == b"1234"


def test_tampered_cached_binary_is_replaced(tmp_path):
    fetch = FakeFetch(PAYLOADS)
    path = ensure_tool("kubectl", SPECS["kubectl"], str(tmp_path), "amd64", fetch)
    with open(path, "wb") as handle:
        handle.write(b"evil")
    assert ensure_tool("kubectl", SPECS["kubectl"], str(tmp_path), "amd64", fetch) == path
    with open(path, "rb") as handle:
        assert handle.read() == KUBECTL_BYTES
    assert len(fetch.urls) == 2


def test_tool_directories_are_owner_only(tmp_path):
    import stat

    target = tmp_path / ".qwenctl" / "bin"
    ensure_tools({"tools": SPECS}, str(target), "amd64", FakeFetch(PAYLOADS))
    for directory in (target, target.parent):
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
