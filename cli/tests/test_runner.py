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
