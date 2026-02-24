"""thin wrappers around tmux commands.

shared by both torganize (session-level) and tname (window-level) entrypoints.
"""

from __future__ import annotations

import os
import socket
import subprocess
from typing import TypedDict


# hostname variants for filtering non-descriptive pane titles
_full_hostname = socket.gethostname()
_short_hostname = _full_hostname.split(".")[0]
HOSTNAME_TITLES = {_full_hostname, _short_hostname}


class PaneContext(TypedDict):
    command: str
    cmdline: str  # full process command with args (e.g. "nvim README.md")
    directory: str
    full_path: str  # absolute working directory
    title: str


class WindowContext(TypedDict):
    id: str
    index: int
    name: str
    panes: list[PaneContext]


class SessionContext(TypedDict):
    session_id: str
    session_path: str
    windows: list[WindowContext]


def run(*args: str) -> str:
    """run a tmux command and return stdout with trailing newlines removed.

    NOTE: must use rstrip('\\n') not strip() — strip() eats leading tabs,
    which breaks tab-delimited format strings when a field (like pane_title)
    is empty.
    """
    result = subprocess.run(["tmux", *args], capture_output=True, text=True)
    return result.stdout.rstrip("\n")


def get_child_cmdline(shell_pid: str) -> str:
    """get the full command line of the first child process of a shell pid.

    returns the args string (e.g. 'nvim README.md', 'opencode -s ses_abc123')
    or empty string if no child found.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-lP", shell_pid],
            capture_output=True,
            text=True,
        )
        first_child_line = (
            result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        )
        if not first_child_line:
            return ""
        child_pid = first_child_line.split()[0]
        args_result = subprocess.run(
            ["ps", "-p", child_pid, "-o", "args="],
            capture_output=True,
            text=True,
        )
        return args_result.stdout.strip()
    except (IndexError, subprocess.SubprocessError):
        return ""


def gather_session_context(session_id: str) -> SessionContext:
    """collect all window and pane info for a tmux session.

    uses process cmdlines (via child pid lookup) and full paths as primary
    signals, since pane titles are inconsistent.
    """
    windows: list[WindowContext] = []

    raw_windows = run(
        "list-windows",
        "-t",
        session_id,
        "-F",
        "#{window_id}\t#{window_index}\t#{window_name}",
    )

    for line in raw_windows.splitlines():
        if not line.strip():
            continue
        window_id, window_index, window_name = line.split("\t")

        panes: list[PaneContext] = []
        raw_panes = run(
            "list-panes",
            "-t",
            window_id,
            "-F",
            "#{pane_title}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_pid}",
        )
        for pane_line in raw_panes.splitlines():
            if not pane_line.strip():
                continue
            pane_title, command, full_path, pane_pid = pane_line.split("\t")
            title = "" if pane_title in HOSTNAME_TITLES else pane_title
            cmdline = get_child_cmdline(pane_pid)
            directory = os.path.basename(full_path)
            panes.append(
                PaneContext(
                    command=command,
                    cmdline=cmdline,
                    directory=directory,
                    full_path=full_path,
                    title=title,
                )
            )

        windows.append(
            WindowContext(
                id=window_id,
                index=int(window_index),
                name=window_name,
                panes=panes,
            )
        )

    session_path = run(
        "display-message",
        "-t",
        session_id,
        "-p",
        "#{pane_current_path}",
    )

    return SessionContext(
        session_id=session_id,
        session_path=session_path,
        windows=windows,
    )
