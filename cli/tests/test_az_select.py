import pytest

from qwenctl.az_select import AzSelectionError, Zone, select_azs
from qwenctl.catalog import PROFILES

from .conftest import MY_OFFERINGS, MY_ZONES


def test_malaysia_picks_az2_and_az1():
    sel = select_azs(MY_ZONES, MY_OFFERINGS, PROFILES)
    assert sel.zone_ids == ("apse5-az1", "apse5-az2")
    placements = {p.profile.name: p.zone_ids for p in sel.placements}
    assert placements == {"g7e-1gpu": ("apse5-az2",), "g6-tp2": ("apse5-az1", "apse5-az2")}
    assert sel.primary.profile.name == "g7e-1gpu"


def test_malaysia_warns_about_single_az_g7e_and_experimental_fallback():
    sel = select_azs(MY_ZONES, MY_OFFERINGS, PROFILES)
    text = "\n".join(sel.warnings)
    assert "g7e.2xlarge 只在 apse5-az2 (ap-southeast-5b) 有货" in text
    assert "g6.12xlarge 兜底" in text
    assert "需要 PoC 验证" in text
    assert "没有 AZ 级高可用" not in text


def test_prefers_zones_with_both_types_and_breaks_ties_by_zone_id():
    zones = tuple(Zone(f"use1-az{i}", f"us-east-1{c}") for i, c in zip(range(1, 5), "abcd"))
    offerings = {
        "use1-az1": frozenset({"g6e.2xlarge"}),
        "use1-az2": frozenset({"g7e.2xlarge", "g6e.2xlarge"}),
        "use1-az3": frozenset({"g7e.2xlarge"}),
        "use1-az4": frozenset({"g7e.2xlarge", "g6e.2xlarge"}),
    }
    sel = select_azs(zones, offerings, PROFILES)
    assert sel.zone_ids == ("use1-az2", "use1-az4")
    assert sel.warnings == ()


def test_result_does_not_depend_on_input_order():
    a = select_azs(MY_ZONES, MY_OFFERINGS, PROFILES)
    b = select_azs(tuple(reversed(MY_ZONES)), MY_OFFERINGS, PROFILES)
    assert a == b


def test_eligible_zones_restrict_choice_and_warn_when_gpu_in_one_zone():
    sel = select_azs(MY_ZONES, MY_OFFERINGS, PROFILES, eligible_zone_ids={"apse5-az1", "apse5-az3"})
    assert sel.zone_ids == ("apse5-az1", "apse5-az3")
    assert sel.primary.profile.name == "g6-tp2"
    assert any("GPU 副本只能放在 apse5-az1" in w for w in sel.warnings)


def test_no_gpu_anywhere_raises():
    offerings = {z.zone_id: frozenset({"m7g.large"}) for z in MY_ZONES}
    with pytest.raises(AzSelectionError, match="没有任何可用的 GPU"):
        select_azs(MY_ZONES, offerings, PROFILES)


def test_fewer_than_two_eligible_zones_raises():
    with pytest.raises(AzSelectionError, match="至少需要 2 个"):
        select_azs(MY_ZONES, MY_OFFERINGS, PROFILES, eligible_zone_ids={"apse5-az2"})
