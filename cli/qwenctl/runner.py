"""执行外部命令（cdk、aws、helm、kubectl）。

- 只用参数列表，不经过 shell，避免注入。
- 默认把输出直接打到终端，客户能实时看到 CloudFormation / helm 的进度。
"""

from __future__ import annotations

import contextlib
import functools
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import BinaryIO, Mapping, Optional, Protocol, Sequence, Tuple


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

    def run_to_file(
        self, args: Sequence[str], path: str, max_bytes: int, ok_returncodes: Sequence[int] = (0,)
    ) -> int:
        """把二进制 stdout 写到 path（只有本人可读写），超过 max_bytes 就停掉命令并报错。返回字节数。"""
        ...


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

    def run_to_file(
        self, args: Sequence[str], path: str, max_bytes: int, ok_returncodes: Sequence[int] = (0,)
    ) -> int:
        """边读边写，超过 max_bytes 立刻停掉命令（CloudShell 的 home 只有 1 GB）。返回字节数。

        任何失败（命令出错、超限、写盘失败、Ctrl-C）都删掉文件、回收子进程。"""
        if os.path.lexists(path):
            os.unlink(path)  # 是符号链接也只删链接本身
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        done = False
        try:
            with os.fdopen(fd, "wb") as out:
                written, returncode, stderr = _stream(args, self._env, out, max_bytes)
            if written > max_bytes:
                raise CommandError(args, 1, f"输出超过 {max_bytes} 字节，已停止")
            if returncode not in ok_returncodes:
                raise CommandError(args, returncode, stderr)
            done = True
            return written
        except FileNotFoundError as exc:
            raise CommandError(args, 127, f"找不到命令 {args[0]}") from exc
        finally:
            if not done:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(path)


CHUNK_BYTES = 1024 * 1024


def _stream(
    args: Sequence[str], env: Mapping[str, str], out: BinaryIO, max_bytes: int
) -> Tuple[int, int, str]:
    """把 stdout 写到 out，超过 max_bytes 就停。无论怎么结束（包括写盘失败）都回收子进程。"""
    # stderr 写临时文件：只读 stdout 时，stderr 管道写满会让两边互相等待
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(list(args), env=dict(env), stdout=subprocess.PIPE, stderr=errors)
        stdout = process.stdout
        if stdout is None:  # 不会发生：stdout=PIPE
            raise RuntimeError("子进程没有 stdout")
        written = 0
        try:
            with stdout:
                for chunk in iter(functools.partial(stdout.read, CHUNK_BYTES), b""):
                    written += len(chunk)
                    if written > max_bytes:
                        break
                    out.write(chunk)
        except BaseException:  # 写盘失败、Ctrl-C：先停掉子进程再往上抛
            process.kill()
            process.wait()
            raise
        if written > max_bytes:
            process.kill()
        returncode = process.wait()
        errors.seek(0)
        return written, returncode, errors.read().decode(errors="replace")
