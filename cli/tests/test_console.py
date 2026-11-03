import pytest

from qwenctl.console import Aborted

from .conftest import make_console


def test_ask_text_uses_default_on_enter():
    con, _ = make_console([""])
    assert con.ask_text("名字", "qwen-vl") == "qwen-vl"


def test_ask_text_retries_until_valid():
    con, out = make_console(["bad", "good"])
    value = con.ask_text("值", "", lambda v: None if v == "good" else "不对")
    assert value == "good"
    assert "✗ 不对" in out.getvalue()


def test_ask_int_enforces_bounds():
    con, out = make_console(["abc", "9", "3"])
    assert con.ask_int("副本", 2, 1, 8) == 3
    assert "请输入整数" in out.getvalue()
    assert "1~8" in out.getvalue()


def test_ask_bool():
    con, _ = make_console(["", "n", "maybe", "YES"])
    assert con.ask_bool("继续", True) is True
    assert con.ask_bool("继续", True) is False
    assert con.ask_bool("继续", False) is True


def test_choose_returns_key_and_shows_options():
    con, out = make_console(["", "2"])
    options = [("a", "选项 A"), ("b", "选项 B")]
    assert con.choose("标题", options, "a") == "a"
    assert con.choose("标题", options, "a") == "b"
    assert "[2] 选项 B" in out.getvalue()


def test_choose_rejects_unknown_default():
    con, _ = make_console()
    with pytest.raises(ValueError):
        con.choose("标题", [("a", "A")], "z")


def test_eof_aborts():
    con, _ = make_console([])
    with pytest.raises(Aborted):
        con.ask_text("x")


def test_assume_defaults_never_reads_input():
    con, out = make_console([], assume_defaults=True)
    assert con.ask_int("副本", 2, 1, 8) == 2
    assert "(默认)" in out.getvalue()


def test_assume_defaults_with_invalid_default_aborts():
    con, _ = make_console([], assume_defaults=True)
    with pytest.raises(Aborted, match="默认值不合法"):
        con.ask_text("x", "", lambda v: "必须填写" if not v else None)


def test_status_prints_detail():
    con, out = make_console()
    con.status("warn", "标题", "细节")
    assert "⚠ 标题" in out.getvalue()
    assert "细节" in out.getvalue()


def test_ask_bool_shows_hint_only_once():
    con, out = make_console([], assume_defaults=True)
    con.ask_bool("继续？", True)
    assert "继续？ [Y/n]: (默认)" in out.getvalue()


def test_non_ascii_digits_are_rejected_instead_of_crashing():
    con, out = make_console(["²", "３", "2"])
    assert con.ask_int("副本", 1, 1, 8) == 2
    assert out.getvalue().count("请输入整数") == 2
    con, _ = make_console(["²", "1"])
    assert con.choose("标题", [("a", "A")], "a") == "a"


def test_interactive_flag():
    assert make_console()[0].interactive is True
    assert make_console(assume_defaults=True)[0].interactive is False
