"""Exercise shell transitions with real tmux and a local SSH stand-in."""

import os
import shlex
import shutil
import subprocess
import time
import uuid

import pytest

from arsenal.tmux_utils import ShellStep, create_session


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    tmux = shutil.which("tmux")
    if not tmux:
        pytest.skip("tmux is required")
    socket = "arsenal-test-" + uuid.uuid4().hex
    config = tmp_path / "tmux.conf"
    config.write_text('set -g default-command "/bin/bash --noprofile --norc -i"\n')
    bindir = tmp_path / "bin"
    bindir.mkdir()

    def executable(name, body):
        path = bindir / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
        return path

    executable("tmux", f"exec {shlex.quote(tmux)} -L {socket} -f {shlex.quote(str(config))} \"$@\"")
    executable("ssh", '\n'.join([
        'case "$1" in',
        '  fail) echo "ssh: connection refused" >&2; exit 255;;',
        '  stall) echo "ssh: waiting for authentication"; exec sleep 30;;',
        'esac',
        'sleep 0.2',
        "export PS1='remote> '",
        'exec /bin/bash --noprofile --norc -i',
    ]))
    executable("enter-context", "sleep 0.2\nexport PS1='context> '\nexec /bin/bash --noprofile --norc -i")
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("TMUX", raising=False)

    def call(*args):
        return subprocess.check_output(["tmux", *args], text=True).strip()

    try:
        yield call
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


def wait_for_file(path):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text()
        time.sleep(0.05)
    pytest.fail(f"Command did not create {path}")


@pytest.mark.parametrize("execute", [False, True])
def test_remote_setup_and_final_command(terminal, tmp_path, execute):
    remote_dir = tmp_path / "remote directory"
    remote_dir.mkdir()
    result = tmp_path / "result"
    create_session("remote", {"main": {"remote": [
        "ssh example", f"cd {shlex.quote(str(remote_dir))}", "export REMOTE_VALUE=works",
        'echo "Setup output (with parentheses); must stay visible."',
        f'printf "%s\\n%s\\n" "$PWD" "$REMOTE_VALUE" > {shlex.quote(str(result))}',
    ]}}, execute=execute, setup_timeout=5)
    output = terminal("capture-pane", "-p", "-J", "-S", "-", "-t", "remote:0.0")
    assert "ready-" not in output
    assert "pane-setup-" not in output
    assert "Setup output (with parentheses); must stay visible." in output.splitlines()
    if not execute:
        assert not result.exists(), "Final command must remain pending"
        terminal("send-keys", "-t", "remote:0.0", "Enter")
    assert wait_for_file(result) == str(remote_dir) + "\nworks\n"


@pytest.mark.parametrize("host,error,diagnostic", [
    ("fail", TimeoutError, "connection refused"),
    ("stall", TimeoutError, "waiting for authentication"),
])
def test_ssh_failure_cleans_up_session(terminal, tmp_path, host, error, diagnostic):
    result_file = tmp_path / "must-not-run"
    with pytest.raises(error, match=diagnostic):
        create_session("failed", {"main": {"remote": [
            ShellStep(f"ssh {host}", prompt=r"remote> $"),
            f"echo wrong-shell > {shlex.quote(str(result_file))}",
        ]}}, execute=True, setup_timeout=0.8)
    assert not result_file.exists()
    result = subprocess.run(["tmux", "has-session", "-t", "failed"], capture_output=True)
    assert result.returncode != 0


def test_local_setup_still_preserves_shell_state(terminal, tmp_path):
    result = tmp_path / "result"
    create_session("local", {"main": {"local": [
        f"cd {shlex.quote(str(tmp_path))}", "export LOCAL_VALUE=works",
        f'printf "%s\\n%s\\n" "$PWD" "$LOCAL_VALUE" > {shlex.quote(str(result))}',
    ]}}, execute=True, setup_timeout=5)
    assert wait_for_file(result) == str(tmp_path) + "\nworks\n"


@pytest.mark.parametrize("entry", ["enter-context", "/bin/bash --noprofile --norc -i"])
def test_arbitrary_nested_shells(terminal, tmp_path, entry):
    result = tmp_path / "result"
    create_session("nested", {"main": {"nested": [
        entry,
        f"cd {shlex.quote(str(tmp_path))}",
        'export CONTEXT_VALUE=outer',
        ShellStep("PS1='inner READY: ' /bin/bash --noprofile --norc -i", prompt=r"inner READY: $"),
        'export CONTEXT_VALUE=inner',
        f'printf "%s\\n%s\\n" "$PWD" "$CONTEXT_VALUE" > {shlex.quote(str(result))}',
    ]}}, execute=True, setup_timeout=5)
    assert wait_for_file(result) == str(tmp_path) + "\ninner\n"


def test_slow_command_finishes_before_next_input(terminal, tmp_path):
    result = tmp_path / "result"
    create_session("slow", {"main": {"slow": [
        "enter-context",
        "sleep 0.4; export SETUP_DONE=yes",
        f'printf "%s" "$SETUP_DONE" > {shlex.quote(str(result))}',
    ]}}, execute=True, setup_timeout=5)
    assert wait_for_file(result) == "yes"


def test_return_to_parent_shell(terminal, tmp_path):
    result = tmp_path / "result"
    create_session("return", {"main": {"nested": [
        "export CONTEXT_VALUE=parent",
        ShellStep("enter-context", prompt=r"context> $"),
        "export CONTEXT_VALUE=child",
        ShellStep("exit", prompt=r"bash-[0-9.]+[$#] ?$"),
        f'printf "%s" "$CONTEXT_VALUE" > {shlex.quote(str(result))}',
    ]}}, execute=True, setup_timeout=5)
    assert wait_for_file(result) == "parent"
