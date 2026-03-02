"""torganize — rename and reorder all windows in a tmux session.

gathers tmux pane context + opencode session data (via `otop sessions`),
sends both to an LLM to generate a naming/ordering plan, then applies it.

forks immediately so tmux unblocks while the LLM call runs in background.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from typing import Optional, TypedDict

from tmux_organize.tmux import SessionContext, gather_session_context, run


# -- types --


class WindowPlan(TypedDict):
    id: str
    name: str
    index: int


class OrganizePlan(TypedDict):
    session: str
    windows: list[WindowPlan]


# -- caching --

CACHE_DIR = os.path.expanduser("~/.cache/torganize")


def build_cache_key(context: SessionContext, opencode_context: str) -> str:
    """stable hash from window structure + opencode session titles.

    includes opencode context so a title change (same processes, different
    session state) busts the cache.
    """
    stable_parts = []
    for window in context["windows"]:
        pane_fingerprints = sorted(
            "%(cmdline)s:%(path)s"
            % {
                "cmdline": p["cmdline"] or p["command"],
                "path": p["full_path"],
            }
            for p in window["panes"]
        )
        stable_parts.append(
            "%(name)s|%(panes)s"
            % {
                "name": window["name"],
                "panes": "|".join(pane_fingerprints),
            }
        )
    raw_key = "%(path)s;%(windows)s;%(oc)s" % {
        "path": context["session_path"],
        "windows": ";".join(stable_parts),
        "oc": opencode_context,
    }
    return hashlib.sha256(raw_key.encode()).hexdigest()[:16]


def read_cached_plan(key: str) -> Optional[dict]:
    cache_path = os.path.join(CACHE_DIR, "%(key)s.json" % {"key": key})
    if not os.path.exists(cache_path):
        return None
    with open(cache_path) as f:
        return json.load(f)


def write_cached_plan(key: str, plan: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(os.path.join(CACHE_DIR, "%(key)s.json" % {"key": key}), "w") as f:
        json.dump(plan, f)


# -- opencode session enrichment --


def query_opencode_sessions() -> list[dict]:
    """call `otop sessions` to get correlated opencode session data.

    returns a list of dicts with pid, tty, tmux_pane, and session info
    (title, status, directory, etc.). returns empty list if otop isn't
    installed or no sessions are running.
    """
    try:
        result = subprocess.run(
            ["otop", "sessions"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return []


def build_opencode_context(
    opencode_sessions: list[dict],
    context: SessionContext,
) -> str:
    """match opencode sessions to tmux windows by pane target.

    otop sessions returns tmux_pane as "session:window.pane" (e.g.
    "personal:3.1"). we match these against the window IDs in our
    session context to annotate which windows have opencode sessions
    and what those sessions are about.
    """
    if not opencode_sessions:
        return ""

    # build a lookup: tmux window index -> opencode session info
    # tmux_pane format is "session_name:window_index.pane_index"
    session_name = run("display-message", "-t", context["session_id"], "-p", "#S")
    window_sessions: dict[int, dict] = {}
    for oc_session in opencode_sessions:
        pane_target = oc_session.get("tmux_pane")
        if not pane_target:
            continue
        # parse "session_name:window_index.pane_index"
        if ":" not in pane_target:
            continue
        target_session, rest = pane_target.split(":", 1)
        if target_session != session_name:
            continue
        if "." in rest:
            window_idx_str = rest.split(".")[0]
        else:
            window_idx_str = rest
        try:
            window_idx = int(window_idx_str)
        except ValueError:
            continue
        session_data = oc_session.get("session", {})
        if session_data:
            window_sessions[window_idx] = session_data

    if not window_sessions:
        return ""

    # format as context lines for the prompt
    lines = []
    for window_idx, session_data in sorted(window_sessions.items()):
        lines.append(
            "window %(idx)d has opencode session: "
            '"%(title)s" (%(status)s, %(msgs)d messages, model: %(model)s)'
            % {
                "idx": window_idx,
                "title": session_data.get("title", "untitled"),
                "status": session_data.get("status", "unknown"),
                "msgs": session_data.get("message_count", 0),
                "model": session_data.get("model", "?"),
            }
        )
    return "\n".join(lines)


# -- model interaction --


def extract_json_from_output(text: str) -> Optional[dict]:
    """find the first complete JSON object in model output."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        if text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def build_prompt(context: SessionContext, opencode_context: str) -> str:
    """build the model prompt with tmux state + opencode session enrichment."""
    window_descriptions = []
    for window in context["windows"]:
        pane_parts = []
        for pane in window["panes"]:
            process_desc = pane["cmdline"] if pane["cmdline"] else pane["command"]
            desc = "process: %(proc)s | pwd: %(path)s" % {
                "proc": process_desc,
                "path": pane["full_path"],
            }
            if pane["title"]:
                desc += " | title: %(title)s" % {"title": pane["title"]}
            pane_parts.append(desc)
        window_descriptions.append(
            '  %(id)s index=%(idx)d name="%(name)s":\n%(panes)s'
            % {
                "id": window["id"],
                "idx": window["index"],
                "name": window["name"],
                "panes": "\n".join("    - %(p)s" % {"p": p} for p in pane_parts),
            }
        )

    windows_block = "\n".join(window_descriptions)

    # opencode enrichment block — injected when otop data is available
    opencode_block = ""
    if opencode_context:
        opencode_block = (
            "\nopencode session data (from otop, use these titles for "
            "descriptive naming):\n%(ctx)s\n" % {"ctx": opencode_context}
        )

    return (
        "organize this tmux session. current state:\n\n"
        "session path: %(path)s\n"
        "windows:\n%(windows)s\n"
        "%(opencode)s\n"
        "the process cmdline and pwd are the most reliable signals. "
        "pane titles are supplementary.%(opencode_hint)s\n\n"
        "conventions:\n"
        "- session name: short lowercase project name derived from the "
        "working directory or dominant codebase "
        '(e.g. "pocus", "enargeia", "sttts", "tmux-organize").\n'
        '- window 1 = "c" if there\'s exactly ONE primary agent/code session '
        "(opencode, claude, aider, etc.)\n"
        '- if there are MULTIPLE agent sessions, do NOT use "c" or "c1"/"c2". '
        "instead, give each a short descriptive name based on what it's working on "
        "(use the opencode session title if available).\n"
        '- window 2 = "k" if there\'s a knowledge/docs window '
        "(editing a readme, spec, todo, markdown, or reference file -- "
        "look at the process args to see what file is open)\n"
        '- window 3 = "code" if there\'s a dedicated code editing window '
        "(nvim/vim editing code files, NOT the agent)\n"
        "- only assign c/k/code if a matching window actually exists; don't force them\n"
        "- remaining windows: short lowercase-hyphenated descriptive names "
        "(2-4 words) based on activity\n"
        "- describe activities and projects, not tools or hostnames\n"
        "- if a window already has a correct conventional name, keep it\n\n"
        "respond with ONLY valid JSON, no markdown fences or explanation:\n"
        '{"session": "name", "windows": ['
        '{"id": "@N", "name": "name", "index": N}, ...]}'
    ) % {
        "path": context["session_path"],
        "windows": windows_block,
        "opencode": opencode_block,
        "opencode_hint": (
            " when opencode session titles are available, prefer deriving "
            "window names from them."
            if opencode_context
            else ""
        ),
    }


def ask_model_for_plan(
    context: SessionContext,
    opencode_context: str,
) -> tuple[Optional[dict], Optional[str]]:
    """call opencode with the default model to generate an organization plan.

    returns (plan, error) — exactly one will be None.
    opencode session: ses_353e6f162ffeKnVCh3sjFq09ZJ
    """
    prompt = build_prompt(context, opencode_context)
    try:
        result = subprocess.run(
            ["opencode", "run", "-m", "anthropic/claude-sonnet-4-5", prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return None, "opencode timed out"
    plan = extract_json_from_output(result.stdout)
    if plan is None:
        return None, "no json in model output"
    return plan, None


def validate_plan(plan: dict, context: SessionContext) -> Optional[str]:
    """check the plan accounts for all windows with unique indices.

    returns None if valid, or an error string describing the problem.
    """
    if "session" not in plan or "windows" not in plan:
        return "missing session/windows keys"
    context_ids = {w["id"] for w in context["windows"]}
    plan_ids = {w["id"] for w in plan["windows"]}
    plan_indices = [w["index"] for w in plan["windows"]]
    if context_ids != plan_ids:
        missing = context_ids - plan_ids
        extra = plan_ids - context_ids
        parts = []
        if missing:
            parts.append("missing %s" % ",".join(sorted(missing)))
        if extra:
            parts.append("extra %s" % ",".join(sorted(extra)))
        return "window id mismatch: %s" % "; ".join(parts)
    if len(plan_indices) != len(set(plan_indices)):
        return "duplicate indices"
    return None


# -- plan application --


def apply_organization_plan(session_id: str, plan: dict) -> None:
    """rename session, rename all windows, and reorder to target indices."""
    # rename all windows
    for window in plan["windows"]:
        run("rename-window", "-t", window["id"], window["name"])

    # reorder: move all to temp high indices to clear conflicts
    for i, window in enumerate(plan["windows"]):
        run(
            "move-window",
            "-s",
            window["id"],
            "-t",
            "%(sid)s:%(idx)d" % {"sid": session_id, "idx": 900 + i},
        )

    # move each to its target index
    for i, window in enumerate(plan["windows"]):
        run(
            "move-window",
            "-s",
            "%(sid)s:%(src)d" % {"sid": session_id, "src": 900 + i},
            "-t",
            "%(sid)s:%(dst)d" % {"sid": session_id, "dst": window["index"]},
        )

    # rename session last (window operations use session_id which is stable)
    run("rename-session", "-t", session_id, plan["session"])


def main() -> None:
    session_id = run("display-message", "-p", "#{session_id}")
    if not session_id:
        print("not in a tmux session")
        sys.exit(1)

    # gather context eagerly before forking
    context = gather_session_context(session_id)
    opencode_sessions = query_opencode_sessions()
    opencode_context = build_opencode_context(opencode_sessions, context)

    # show status in the status bar via @torganize session option
    # (requires #{?@torganize,...,} in status-right; see README)
    run("set-option", "-t", session_id, "@torganize", "organizing...")

    # fork: parent exits immediately so tmux unblocks
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()

    # child: check cache or call model
    cache_key = build_cache_key(context, opencode_context)
    plan = read_cached_plan(cache_key)
    is_cached = plan is not None

    if not plan:
        plan, error = ask_model_for_plan(context, opencode_context)
        if error:
            run("set-option", "-t", session_id, "@torganize", error)
            sys.exit(1)

    # narrowing: one of cache hit or model call succeeded
    assert plan is not None

    validation_error = validate_plan(plan, context)
    if validation_error:
        run("set-option", "-t", session_id, "@torganize", validation_error)
        sys.exit(1)

    if not is_cached:
        write_cached_plan(cache_key, plan)

    apply_organization_plan(session_id, plan)

    # clear status indicator — the renamed windows are the feedback
    run("set-option", "-t", session_id, "-u", "@torganize")


def cli() -> None:
    main()


if __name__ == "__main__":
    cli()
