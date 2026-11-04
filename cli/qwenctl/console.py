"""终端交互：提问、校验、默认值。输入输出可注入，方便测试。"""

from __future__ import annotations

import sys
from typing import Callable, Optional, Sequence, TextIO, Tuple

Validator = Callable[[str], Optional[str]]  # 返回错误信息；合法时返回 None

SYMBOLS = {"ok": "✓", "warn": "⚠", "fail": "✗", "info": "ℹ"}


class Aborted(Exception):
    """用户取消，或非交互模式下默认值不合法。"""


def _is_ascii_int(raw: str) -> bool:
    # "²".isdigit() 为 True 但 int("²") 会报错，所以只接受 ASCII 数字
    return raw.isascii() and raw.isdecimal()


class Console:
    def __init__(
        self,
        input_fn: Callable[[str], str] = input,
        out: Optional[TextIO] = None,
        assume_defaults: bool = False,
    ) -> None:
        self._input = input_fn
        self._out = out or sys.stdout
        self._assume_defaults = assume_defaults

    @property
    def interactive(self) -> bool:
        return not self._assume_defaults

    # ---- 输出 ----
    def line(self, text: str = "") -> None:
        self._out.write(text + "\n")
        # 输出重定向到文件时 Python 会缓冲，和子进程（cdk/helm）的输出顺序会乱
        self._out.flush()

    def status(self, status: str, text: str, detail: str = "") -> None:
        self.line(f"  {SYMBOLS.get(status, '-')} {text}")
        if detail:
            self.line(f"      {detail}")

    def heading(self, text: str) -> None:
        self.line()
        self.line(text)

    # ---- 输入 ----
    def _read(self, prompt: str) -> str:
        if self._assume_defaults:
            self.line(prompt + "(默认)")
            return ""
        try:
            return self._input(prompt)
        except (EOFError, KeyboardInterrupt) as exc:
            raise Aborted("已取消") from exc

    def ask_text(
        self,
        label: str,
        default: str = "",
        validator: Optional[Validator] = None,
        show_default: bool = True,
    ) -> str:
        suffix = f" [{default}]" if default and show_default else ""
        while True:
            value = self._read(f"  {label}{suffix}: ").strip() or default
            error = validator(value) if validator else None
            if error is None:
                return value
            self.status("fail", error)
            if self._assume_defaults:
                raise Aborted(f"默认值不合法：{error}")

    def ask_int(self, label: str, default: int, low: int, high: int) -> int:
        def check(raw: str) -> Optional[str]:
            if not _is_ascii_int(raw):
                return "请输入整数"
            if not low <= int(raw) <= high:
                return f"请输入 {low}~{high} 之间的整数"
            return None

        return int(self.ask_text(label, str(default), check))

    def ask_bool(self, label: str, default: bool) -> bool:
        hint = "Y/n" if default else "y/N"

        def check(raw: str) -> Optional[str]:
            return None if raw.lower() in ("y", "yes", "n", "no") else "请输入 y 或 n"

        answer = self.ask_text(f"{label} [{hint}]", "y" if default else "n", check, show_default=False)
        return answer.lower() in ("y", "yes")

    def choose(self, label: str, options: Sequence[Tuple[str, str]], default: str) -> str:
        """options 是 (key, 显示文字) 列表，返回选中的 key。"""
        keys = [k for k, _ in options]
        if default not in keys:
            raise ValueError(f"默认值 {default} 不在选项里")
        self.line(f"  {label}")
        for index, (_, text) in enumerate(options, start=1):
            self.line(f"    [{index}] {text}")
        default_index = str(keys.index(default) + 1)

        def check(raw: str) -> Optional[str]:
            if _is_ascii_int(raw) and 1 <= int(raw) <= len(options):
                return None
            return f"请输入 1~{len(options)} 之间的编号"

        return keys[int(self.ask_text("请选择", default_index, check)) - 1]
