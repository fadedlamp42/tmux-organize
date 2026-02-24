"""tname — rename a single tmux window based on pane context.

gathers process cmdlines, paths, and titles from the current window,
enriches with opencode session data if available, then asks a fast model
(haiku) for a short descriptive slug.

forks immediately so tmux unblocks while the LLM call runs in background.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from tmux_organize.tmux import HOSTNAME_TITLES, get_child_cmdline, run


def query_opencode_for_window(window_index: int, session_name: str) -> str:
    """check if any opencode session is running in this window via otop.

    returns the session title if found, empty string otherwise.
    """
    try:
        result = subprocess.run(
            ["otop", "sessions"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return ""
        sessions = json.loads(result.stdout)
        for entry in sessions:
            pane_target = entry.get("tmux_pane", "")
            if not pane_target or ":" not in pane_target:
                continue
            target_session, rest = pane_target.split(":", 1)
            if target_session != session_name:
                continue
            idx_str = rest.split(".")[0] if "." in rest else rest
            try:
                if int(idx_str) == window_index:
                    session_data = entry.get("session", {})
                    return session_data.get("title", "")
            except ValueError:
                continue
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass
    return ""


def gather_window_context(window_id: str) -> str:
    """collect pane context for a single window. returns a context string
    with process cmdlines, paths, and filtered titles."""
    # process cmdlines
    raw_pids = run(
        "list-panes",
        "-t",
        window_id,
        "-F",
        "#{pane_pid}",
    )
    cmdlines = []
    for pid in raw_pids.splitlines():
        pid = pid.strip()
        if not pid:
            continue
        cmdline = get_child_cmdline(pid)
        if cmdline:
            cmdlines.append(cmdline)

    # unique paths
    raw_paths = run(
        "list-panes",
        "-t",
        window_id,
        "-F",
        "#{pane_current_path}",
    )
    paths = sorted(set(p.strip() for p in raw_paths.splitlines() if p.strip()))

    # filtered pane titles (exclude hostname)
    raw_titles = run(
        "list-panes",
        "-t",
        window_id,
        "-F",
        "#{pane_title}",
    )
    titles = sorted(
        set(
            t.strip()
            for t in raw_titles.splitlines()
            if t.strip() and t.strip() not in HOSTNAME_TITLES
        )
    )

    window_name = run("display-message", "-t", window_id, "-p", "#W")

    parts = []
    if cmdlines:
        parts.append("processes: %(procs)s" % {"procs": "; ".join(cmdlines)})
    if paths:
        parts.append("paths: %(paths)s" % {"paths": "; ".join(paths)})
    if titles:
        parts.append("titles: %(titles)s" % {"titles": "; ".join(titles)})
    if window_name:
        parts.append("current name: %(name)s" % {"name": window_name})

    return "; ".join(parts)


def main() -> None:
    window_id = run("display-message", "-p", "#{window_id}")
    if not window_id:
        print("not in a tmux session")
        sys.exit(1)

    # gather context eagerly before forking
    context = gather_window_context(window_id)
    if not context:
        run("display-message", "no pane context found")
        sys.exit(1)

    # try to get opencode session context for this window
    session_name = run("display-message", "-p", "#S")
    window_index_str = run("display-message", "-p", "#{window_index}")
    opencode_title = ""
    try:
        window_index = int(window_index_str)
        opencode_title = query_opencode_for_window(window_index, session_name)
    except ValueError:
        pass

    if opencode_title:
        context += "; opencode session title: %(title)s" % {"title": opencode_title}

    run("display-message", "naming...")

    # fork: parent exits immediately so tmux unblocks
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()

    # child: call model for slug
    prompt = (
        "here is context about a tmux window: %(ctx)s -- "
        "generate a short lowercase-hyphenated name for this window "
        "(2-4 words max). describe what the user is working on, not "
        "the tools or hostname. output ONLY the slug, nothing else."
    ) % {"ctx": context}

    try:
        result = subprocess.run(
            ["opencode", "run", "-m", "anthropic/claude-haiku-4-5", prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
        slug = result.stdout.strip().replace(" ", "").replace("\n", "")
    except subprocess.TimeoutExpired:
        slug = ""

    if not slug:
        run(
            "display-message",
            "tname: failed for %(wid)s" % {"wid": window_id},
        )
        sys.exit(1)

    run("rename-window", "-t", window_id, slug)
    run(
        "display-message",
        "renamed: %(slug)s" % {"slug": slug},
    )


def cli() -> None:
    main()


if __name__ == "__main__":
    cli()
