import json

import pytest

from qwenctl.versions import VersionsError, load_versions


def test_repository_versions_file_is_complete():
    versions = load_versions()
    assert versions["kubernetes"] == "1.36"
    assert set(versions["tools"]) == {"kubectl", "helm"}
    for spec in versions["tools"].values():
        assert set(spec["sha256"]) == {"amd64", "arm64"}
        assert all(len(h) == 64 for h in spec["sha256"].values())
    assert versions["charts"]["karpenter"]["version"] == versions["charts"]["karpenter-crd"]["version"]


def test_missing_file_and_fields(tmp_path):
    with pytest.raises(VersionsError, match="交付包可能不完整"):
        load_versions(str(tmp_path / "nope.json"))
    partial = tmp_path / "versions.json"
    partial.write_text(json.dumps({"kubernetes": "1.36"}), encoding="utf-8")
    with pytest.raises(VersionsError, match="缺少字段：bottlerocket"):
        load_versions(str(partial))
