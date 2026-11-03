"""执行外部命令（cdk、aws、helm、kubectl）。

- 只用参数列表，不经过 shell，避免注入。
- 默认把输出直接打到终端，客户能实时看到 CloudFormation / helm 的进度。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, Sequence


class CommandError(Exception):
    def __init__(self, args: Sequence[str], returncode: int, output: str = "") -> None:
        self.args_list = list(args)
        self.returncode = returncode
        self.output = output
        super().__init__(f"命令失败（退出码 {returncode}）：{' '.join(args)}")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str


class Runner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Optional[str] = None,
        input_text: Optional[str] = None,
        capture: bool = False,
        env: Optional[Mapping[str, str]] = None,
    ) -> CommandResult: ...


class SubprocessRunner:
    def __init__(self, env: Mapping[str, str], extra_path: Sequence[str] = ()) -> None:
        merged = {**os.environ, **env}
        path = os.pathsep.join([*extra_path, merged.get("PATH", "")])
        self._env = {**merged, "PATH": path}

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Optional[str] = None,
        input_text: Optional[str] = None,
        capture: bool = False,
        env: Optional[Mapping[str, str]] = None,
    ) -> CommandResult:
        """env 只对这一条命令生效（在默认环境上覆盖）。"""
        try:
            completed = subprocess.run(
                list(args),
                cwd=cwd,
                env={**self._env, **(env or {})},
                input=input_text,
                text=True,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.STDOUT if capture else None,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CommandError(args, 127, f"找不到命令 {args[0]}") from exc
        if completed.returncode != 0:
            raise CommandError(args, completed.returncode, completed.stdout or "")
        return CommandResult(completed.returncode, completed.stdout or "")
