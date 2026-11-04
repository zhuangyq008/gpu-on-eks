import os
import stat
import sys

import pytest

from qwenctl.runner import CommandError, SubprocessRunner


def test_capture_and_env(tmp_path):
    runner = SubprocessRunner({"QWEN_TEST": "hello"}, extra_path=[str(tmp_path)])
    result = runner.run([sys.executable, "-c", "import os; print(os.environ['QWEN_TEST'])"], capture=True)
    assert result.stdout.strip() == "hello"
    path = runner.run([sys.executable, "-c", "import os; print(os.environ['PATH'])"], capture=True).stdout
    assert path.startswith(str(tmp_path))


def test_stdin_is_passed():
    runner = SubprocessRunner({})
    result = runner.run(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], input_text="abc", capture=True
    )
    assert result.stdout.strip() == "ABC"


def test_failure_raises_with_output():
    runner = SubprocessRunner({})
    with pytest.raises(CommandError) as info:
        runner.run([sys.executable, "-c", "print('boom'); raise SystemExit(3)"], capture=True)
    assert info.value.returncode == 3 and "boom" in info.value.output


def test_missing_command():
    with pytest.raises(CommandError) as info:
        SubprocessRunner({}).run(["definitely-not-a-real-command-qwenctl"])
    assert info.value.returncode == 127


def test_per_command_env_overrides_default():
    runner = SubprocessRunner({"QWEN_TEST": "default"})
    code = "import os; print(os.environ['QWEN_TEST'])"
    assert (
        runner.run([sys.executable, "-c", code], capture=True, env={"QWEN_TEST": "override"}).stdout.strip()
        == "override"
    )
    assert runner.run([sys.executable, "-c", code], capture=True).stdout.strip() == "default"


def test_run_to_file_writes_binary_stdout_owner_only(tmp_path):
    path = tmp_path / "out.bin"
    path.write_bytes(b"stale")
    os.chmod(path, 0o644)
    runner = SubprocessRunner({})
    script = "import sys; sys.stdout.buffer.write(bytes(range(256)) * 8192)"  # 2 MiB，跨多个分块
    assert runner.run_to_file([sys.executable, "-c", script], str(path), max_bytes=10**7) == 256 * 8192
    assert path.read_bytes() == bytes(range(256)) * 8192
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_run_to_file_does_not_follow_symlinks(tmp_path):
    target = tmp_path / "kubeconfig"
    target.write_text("keep me")
    link = tmp_path / "out.bin"
    link.symlink_to(target)
    SubprocessRunner({}).run_to_file([sys.executable, "-c", "print('x')"], str(link), max_bytes=100)
    assert target.read_text() == "keep me" and not link.is_symlink()


def test_run_to_file_stops_at_the_limit_and_removes_the_file(tmp_path):
    path = tmp_path / "out.bin"
    script = "import sys\nwhile True: sys.stdout.buffer.write(b'x' * 65536)"
    with pytest.raises(CommandError) as err:
        SubprocessRunner({}).run_to_file([sys.executable, "-c", script], str(path), max_bytes=1_000_000)
    assert "超过 1000000 字节" in err.value.output and not path.exists()


def test_run_to_file_failure_carries_stderr(tmp_path):
    runner = SubprocessRunner({})
    script = "import sys; sys.stderr.write('nope' * 100000); sys.exit(3)"  # 大量 stderr 也不会卡住
    with pytest.raises(CommandError) as err:
        runner.run_to_file([sys.executable, "-c", script], str(tmp_path / "x"), max_bytes=100)
    assert err.value.returncode == 3 and "nope" in err.value.output
    assert not (tmp_path / "x").exists()


def test_run_to_file_missing_command(tmp_path):
    with pytest.raises(CommandError) as err:
        SubprocessRunner({}).run_to_file(["definitely-not-a-command-xyz"], str(tmp_path / "x"), max_bytes=1)
    assert err.value.returncode == 127 and not (tmp_path / "x").exists()


class _FailingOut:
    """写盘失败（磁盘满、配额）。"""

    def __init__(self, error):
        self.error = error

    def write(self, _data):
        raise self.error


@pytest.mark.parametrize("error", [OSError(28, "No space left on device"), KeyboardInterrupt()])
def test_stream_reaps_the_child_when_writing_fails(error):
    from qwenctl import runner as runner_module

    # 子进程一直写：如果出错时没有 kill，wait() 会一直等下去（测试会超时）
    forever = [sys.executable, "-c", "import sys\nwhile True: sys.stdout.buffer.write(b'x' * 65536)"]
    with pytest.raises(type(error)):
        runner_module._stream(forever, dict(os.environ), _FailingOut(error), max_bytes=10**9)


def test_run_to_file_removes_the_file_when_writing_fails(tmp_path, monkeypatch):
    from qwenctl import runner as runner_module

    def disk_full(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runner_module, "_stream", disk_full)
    path = tmp_path / "out.bin"
    with pytest.raises(OSError):
        SubprocessRunner({}).run_to_file([sys.executable, "-c", "pass"], str(path), max_bytes=100)
    assert not path.exists()


def test_run_to_file_accepts_listed_returncodes(tmp_path):
    path = tmp_path / "out.bin"
    script = "import sys; sys.stdout.write('partial'); sys.exit(1)"
    size = SubprocessRunner({}).run_to_file(
        [sys.executable, "-c", script], str(path), 100, ok_returncodes=(0, 1)
    )
    assert size == 7 and path.read_bytes() == b"partial"
