"""Create tmux sessions from {window_name: {pane_name: command}} mappings."""

import math
import os
import re
import subprocess
import termios
import time
from collections.abc import Mapping
from dataclasses import dataclass


DEFAULT_PROMPT = r"[#$>%❯] ?$"


@dataclass(frozen=True)
class ShellStep:
    """A command with an optional expected prompt after execution.

    Set prompt when entering a shell to distinguish its prompt from the parent
    shell's prompt if the command fails. The regex matches the current cursor
    line, without terminal color escapes. Commands are never rewritten.
    """

    command: str
    prompt: str | None = None


def create_session(
    session_name: str,
    session: Mapping[str, Mapping[str, str | list[str | ShellStep]]],
    *,
    workdir_override: Mapping[str, str | os.PathLike[str]] | None = None,
    execute: bool = False,
    attach: bool = False,
    setup_timeout: float = 300,
    prompt_pattern: str = DEFAULT_PROMPT,
) -> str:
    """Create a session, leaving commands at shell prompts unless execute=True.

    Windows and panes follow mapping order. Existing sessions are never replaced.
    When already inside tmux, attaching switches the current client.
    workdir_override maps pane names to starting directories, before command input.
    Pane values may be strings or lists of strings/ShellStep objects. Each
    earlier entry is sent verbatim, then we wait for a stable shell prompt
    before continuing. This also works when a command enters a nested shell.
    The final entry stays pending unless execute=True; it is never awaited.

    prompt_pattern matches common shell prompts by default. Override it for
    custom prompts, or use ShellStep.prompt for a particular shell transition.
    Subsequent steps inherit that prompt until another step changes it.
    Readiness requires a fresh prompt, a terminal accepting interactive input,
    and 150 ms of stable output. This is terminal automation: programs that
    imitate shell prompts need a more specific regex. A prompt confirms input
    readiness, not command success. Authentication requiring input must be
    completed separately. Setup timeout applies to each command/prompt wait.
    """
    if not session_name or any(char in session_name for char in ":.\n"):
        raise ValueError("session_name must be nonempty and contain no colon, dot, or newline")
    if not session:
        raise ValueError("session must contain at least one window")
    re.compile(prompt_pattern)
    if not math.isfinite(setup_timeout) or setup_timeout <= 0:
        raise ValueError("setup_timeout must be positive")
    for window_name, panes in session.items():
        if not window_name or not panes:
            raise ValueError("Each window needs a name and at least one pane")
        for pane_name, commands in panes.items():
            if not pane_name or not isinstance(commands, (str, list)):
                raise ValueError("Each pane needs a name and a command string or list of strings")
            for step in [commands] if isinstance(commands, str) else commands:
                command = step.command if isinstance(step, ShellStep) else step
                if isinstance(step, ShellStep) and step.prompt is not None:
                    re.compile(step.prompt)
                if not isinstance(command, str):
                    raise ValueError("Pane commands must be strings")
                if any(char in command for char in "\n\r"):
                    raise ValueError("Pane commands must be single-line to avoid accidental execution")

    workdirs = {
        name: os.path.abspath(os.path.expanduser(path))
        for name, path in (workdir_override or {}).items()
    }
    for path in workdirs.values():
        if not os.path.isdir(path):
            raise NotADirectoryError(path)

    def workdir_args(pane_name: str) -> tuple[str, ...]:
        return ("-c", workdirs[pane_name]) if pane_name in workdirs else ()

    def tmux(*args: str) -> str:
        return subprocess.check_output(["tmux", *args], text=True).rstrip("\n")

    def snapshot(pane_id: str) -> tuple[str, str, str]:
        output = tmux("capture-pane", "-p", "-t", pane_id)
        position = tmux("display-message", "-p", "-t", pane_id,
                        "#{cursor_y} #{cursor_x} #{history_size}")
        row, column, _ = map(int, position.split())
        lines = output.split("\n")
        # capture-pane omits trailing blank cells, including prompt spaces.
        current_line = lines[row].ljust(column)[:column] if row < len(lines) else ""
        return output, position, current_line

    def wait_for_prompt(pane_id: str, pattern: str, *, previous=None, command=None) -> None:
        expected = re.compile(pattern)
        tty = tmux("display-message", "-p", "-t", pane_id, "#{pane_tty}")
        deadline = time.monotonic() + setup_timeout
        stable_since = None
        last_state = None
        with open(tty, "rb", buffering=0) as terminal:
            while time.monotonic() < deadline:
                state = snapshot(pane_id)
                flags = termios.tcgetattr(terminal)[3]
                ready = (
                    state != previous
                    and not flags & (termios.ICANON | termios.ECHO)
                    and expected.search(state[2]) is not None
                )
                if ready:
                    if state != last_state or stable_since is None:
                        stable_since = time.monotonic()
                    elif stable_since is not None and time.monotonic() - stable_since >= 0.15:
                        return
                else:
                    stable_since = None
                last_state = state
                time.sleep(0.025)
        context = f" after {command!r}" if command is not None else ""
        output = tmux("capture-pane", "-p", "-J", "-S", "-", "-t", pane_id)
        raise TimeoutError(
            f"Pane {pane_id} did not reach prompt {pattern!r}{context} "
            f"within {setup_timeout} seconds. Pane output:\n{output}"
        )

    # new-session fails safely if the name is already in use.
    first_window = next(iter(session))
    result = tmux(
        "new-session", "-d", "-P", "-F", "#{session_id} #{window_id} #{pane_id}",
        "-s", session_name, "-n", first_window,
        *workdir_args(next(iter(session[first_window]))),
    )
    session_id, first_window_id, first_pane_id = result.split()
    try:
        pending_commands = []
        for option, value in (("mouse", "on"), ("history-limit", "50000")):
            tmux("set-option", "-t", session_id, option, value)
        for index, (window_name, panes) in enumerate(session.items()):
            if index == 0:
                window_id, pane_id = first_window_id, first_pane_id
            else:
                window_id, pane_id = tmux(
                    "new-window", "-d", "-P", "-F", "#{window_id} #{pane_id}",
                    "-t", session_id + ":", "-n", window_name,
                    *workdir_args(next(iter(panes))),
                ).split()
            tmux("set-window-option", "-t", window_id, "automatic-rename", "off")
            tmux("set-window-option", "-t", window_id, "allow-rename", "off")
            tmux("set-window-option", "-t", window_id, "pane-border-status", "top")
            tmux("set-window-option", "-t", window_id, "pane-border-format", " #{pane_index}: #{pane_title} ")
            initial_pane_id = pane_id
            for pane_index, (pane_name, command) in enumerate(panes.items()):
                if pane_index:
                    pane_id = tmux(
                        "split-window", "-d", "-h", "-P", "-F", "#{pane_id}",
                        "-t", window_id,
                        *workdir_args(pane_name),
                    )
                    tmux("select-layout", "-t", window_id, "tiled")
                tmux("select-pane", "-t", pane_id, "-T", pane_name)
                if command:
                    pending_commands.append((pane_id, command))
            tmux("select-pane", "-t", initial_pane_id)
        for pane_id, commands in pending_commands:
            commands = [commands] if isinstance(commands, str) else commands
            pattern = prompt_pattern
            wait_for_prompt(pane_id, pattern)
            for step in commands[:-1]:
                command = step.command if isinstance(step, ShellStep) else step
                if isinstance(step, ShellStep) and step.prompt is not None:
                    pattern = step.prompt
                if not command:
                    continue
                previous = snapshot(pane_id)
                tmux("send-keys", "-t", pane_id, "-l", "--", command)
                tmux("send-keys", "-t", pane_id, "Enter")
                wait_for_prompt(pane_id, pattern, previous=previous, command=command)
            final = commands[-1]
            command = final.command if isinstance(final, ShellStep) else final
            if not command:
                continue
            tmux("send-keys", "-t", pane_id, "-l", "--", command)
            if execute:
                tmux("send-keys", "-t", pane_id, "Enter")
        tmux("select-window", "-t", first_window_id)
    except BaseException:
        # Only remove the session created by this call if setup fails.
        subprocess.run(["tmux", "kill-session", "-t", session_id], check=False)
        raise
    if attach:
        action = "switch-client" if os.environ.get("TMUX") else "attach-session"
        subprocess.run(["tmux", action, "-t", session_id], check=True)
    return session_name
