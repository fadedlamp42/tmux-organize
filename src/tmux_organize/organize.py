"""torganize — rename and reorder all windows in a tmux session.

two-phase approach for reliability at scale:
  1. name each window individually (small, focused LLM calls)
  2. order all windows + name session (one lightweight LLM call)

retries each phase with status bar visibility (e.g. "naming 3/10 (2/3)").
forks immediately so tmux unblocks while LLM calls run in background.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from typing import Optional, TypedDict

from .config import get_opencode_model
from tmux_organize.tmux import SessionContext, gather_session_context, run


# -- types --


class WindowPlan(TypedDict):
    id: str
    name: str
    index: int


class OrganizePlan(TypedDict):
    session: str
    windows: list[WindowPlan]


# -- constants --

MAX_RETRIES = 3
CACHE_DIR = os.path.expanduser("~/.cache/torganize")


# -- caching --


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


# -- model helpers --


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


def call_model(prompt: str) -> tuple[Optional[str], Optional[str]]:
    """run opencode with the configured model. returns (stdout, error)."""
    model = get_opencode_model()
    try:
        result = subprocess.run(
            ["opencode", "run", "-m", model, prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return None, "opencode timed out"
    return result.stdout, None


# -- phase 1: per-window naming --


def build_window_summary(window: dict) -> str:
    """one-line summary of a window for cross-window context."""
    processes = []
    for pane in window["panes"]:
        proc = pane["cmdline"] if pane["cmdline"] else pane["command"]
        processes.append(proc)
    return "%(id)s (index %(idx)d): %(procs)s in %(path)s" % {
        "id": window["id"],
        "idx": window["index"],
        "procs": ", ".join(processes) if processes else "shell",
        "path": window["panes"][0]["full_path"] if window["panes"] else "?",
    }


def build_naming_prompt(
    target_window: dict,
    all_windows: list[dict],
    already_named: dict[str, str],
    opencode_context: str,
) -> str:
    """prompt for naming a single window with cross-window context.

    includes detailed pane info for the target window, names already
    assigned to earlier windows (to avoid duplicates), and summaries
    of windows not yet named.
    """
    # detailed pane info for target window
    pane_lines = []
    for pane in target_window["panes"]:
        process_desc = pane["cmdline"] if pane["cmdline"] else pane["command"]
        line = "  - process: %(proc)s | pwd: %(path)s" % {
            "proc": process_desc,
            "path": pane["full_path"],
        }
        if pane["title"]:
            line += " | title: %(title)s" % {"title": pane["title"]}
        pane_lines.append(line)

    # previously named windows (so model avoids duplicate names)
    named_block = ""
    if already_named:
        named_lines = [
            '  %(wid)s -> "%(name)s"' % {"wid": wid, "name": name}
            for wid, name in already_named.items()
        ]
        named_block = "\nalready named windows:\n%(lines)s\n" % {
            "lines": "\n".join(named_lines)
        }

    # not-yet-named windows (excluding target)
    unnamed = [
        w
        for w in all_windows
        if w["id"] != target_window["id"] and w["id"] not in already_named
    ]
    unnamed_block = ""
    if unnamed:
        unnamed_lines = ["  " + build_window_summary(w) for w in unnamed]
        unnamed_block = "\nnot yet named:\n%(lines)s\n" % {
            "lines": "\n".join(unnamed_lines)
        }

    # opencode enrichment
    oc_block = ""
    if opencode_context:
        oc_block = "\nopencode session data:\n%(ctx)s\n" % {"ctx": opencode_context}

    return (
        "name this tmux window. context:\n\n"
        'this window (%(id)s, index=%(idx)d, current name="%(name)s"):\n'
        "%(panes)s\n"
        "%(named)s%(unnamed)s%(opencode)s\n"
        "conventions:\n"
        '- "c" if this is the ONLY primary agent/code session '
        "(opencode, claude, aider, etc.) in the session\n"
        "- if there are MULTIPLE agent sessions, use a short descriptive "
        "name based on what it's working on "
        "(use the opencode session title if available)\n"
        '- "k" if editing knowledge/docs files '
        "(readme, spec, todo, markdown, or reference file)\n"
        '- "code" if dedicated code editing '
        "(nvim/vim editing code files, NOT the agent)\n"
        "- otherwise: short lowercase-hyphenated descriptive name "
        "(2-4 words) based on activity\n"
        "- if the current name already fits conventions, keep it\n"
        "- do NOT reuse a name already assigned to another window\n\n"
        "respond with ONLY the window name slug, nothing else."
    ) % {
        "id": target_window["id"],
        "idx": target_window["index"],
        "name": target_window["name"],
        "panes": "\n".join(pane_lines),
        "named": named_block,
        "unnamed": unnamed_block,
        "opencode": oc_block,
    }


def ask_model_for_window_name(
    target_window: dict,
    all_windows: list[dict],
    already_named: dict[str, str],
    opencode_context: str,
) -> tuple[Optional[str], Optional[str]]:
    """ask LLM to name a single window. returns (slug, error)."""
    prompt = build_naming_prompt(
        target_window,
        all_windows,
        already_named,
        opencode_context,
    )
    stdout, error = call_model(prompt)
    if error:
        return None, error
    slug = stdout.strip().replace("\n", "").strip('"').strip("'").strip()
    if not slug:
        return None, "empty name from model"
    return slug, None


# -- phase 2: ordering + session naming --


def build_ordering_prompt(
    named_windows: list[dict],
    session_path: str,
) -> str:
    """lightweight prompt for session naming + window ordering.

    only sends window IDs and their already-determined names — no pane
    details. this keeps the payload tiny so the model can reliably
    track all IDs even with many windows.
    """
    window_lines = []
    for w in named_windows:
        window_lines.append('  %(id)s: "%(name)s"' % {"id": w["id"], "name": w["name"]})
    return (
        "order these tmux windows and name the session.\n\n"
        "session path: %(path)s\n\n"
        "windows (id: name):\n%(windows)s\n\n"
        "ordering conventions:\n"
        '- "c" (primary agent) -> index 1\n'
        '- "k" (knowledge/docs) -> index 2\n'
        '- "code" (code editing) -> index 3\n'
        "- remaining windows: order by relatedness/workflow\n\n"
        "session name: short lowercase project name derived from the "
        'working directory (e.g. "pocus", "enargeia", "tmux-organize")\n\n'
        "respond with ONLY valid JSON, no markdown fences:\n"
        '{"session": "name", "windows": ['
        '{"id": "@N", "index": N}, ...]}'
    ) % {
        "path": session_path,
        "windows": "\n".join(window_lines),
    }


def ask_model_for_ordering(
    named_windows: list[dict],
    session_path: str,
) -> tuple[Optional[dict], Optional[str]]:
    """ask LLM for session name + window ordering. returns (ordering, error)."""
    prompt = build_ordering_prompt(named_windows, session_path)
    stdout, error = call_model(prompt)
    if error:
        return None, error
    ordering = extract_json_from_output(stdout)
    if ordering is None:
        return None, "no json in model output"
    return ordering, None


# -- validation --


def validate_plan(plan: dict, context: SessionContext) -> Optional[str]:
    """validate a full plan (e.g. from cache) against current session state.

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


def validate_ordering(
    ordering: dict,
    expected_ids: set[str],
) -> Optional[str]:
    """validate an ordering response has all expected IDs with unique indices.

    separate from validate_plan because the ordering response only has
    {id, index} pairs (no name field — names are merged in afterward).
    """
    if "session" not in ordering or "windows" not in ordering:
        return "missing session/windows keys"
    plan_ids = {w["id"] for w in ordering["windows"]}
    if expected_ids != plan_ids:
        missing = expected_ids - plan_ids
        extra = plan_ids - expected_ids
        parts = []
        if missing:
            parts.append("missing %s" % ",".join(sorted(missing)))
        if extra:
            parts.append("extra %s" % ",".join(sorted(extra)))
        return "window id mismatch: %s" % "; ".join(parts)
    indices = [w["index"] for w in ordering["windows"]]
    if len(indices) != len(set(indices)):
        return "duplicate indices"
    return None


# -- fallback ordering --

CONVENTION_PRIORITY = {"c": 1, "k": 2, "code": 3}


def compute_fallback_ordering(
    named_windows: list[dict],
    session_path: str,
) -> dict:
    """deterministic ordering when the LLM ordering call fails.

    applies convention priorities (c=1, k=2, code=3) then sorts
    remaining windows alphabetically by name.
    """
    prioritized = []
    remaining = []
    for w in named_windows:
        if w["name"] in CONVENTION_PRIORITY:
            prioritized.append(w)
        else:
            remaining.append(w)

    prioritized.sort(key=lambda w: CONVENTION_PRIORITY[w["name"]])
    remaining.sort(key=lambda w: w["name"])

    ordered = prioritized + remaining
    session_name = os.path.basename(session_path).lower().replace(" ", "-")

    return {
        "session": session_name,
        "windows": [
            {"id": w["id"], "name": w["name"], "index": i + 1}
            for i, w in enumerate(ordered)
        ],
    }


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


# -- orchestration --


def set_status(session_id: str, message: str) -> None:
    """update the @torganize status bar indicator."""
    run("set-option", "-t", session_id, "@torganize", message)


def main() -> None:
    session_id = run("display-message", "-p", "#{session_id}")
    if not session_id:
        print("not in a tmux session")
        sys.exit(1)

    # gather context eagerly before forking
    context = gather_session_context(session_id)
    opencode_sessions = query_opencode_sessions()
    opencode_context = build_opencode_context(opencode_sessions, context)

    set_status(session_id, "organizing...")

    # fork: parent exits immediately so tmux unblocks
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()

    # check cache first — skip all LLM calls on hit
    cache_key = build_cache_key(context, opencode_context)
    cached_plan = read_cached_plan(cache_key)
    if cached_plan and validate_plan(cached_plan, context) is None:
        apply_organization_plan(session_id, cached_plan)
        run("set-option", "-t", session_id, "-u", "@torganize")
        return

    # -- phase 1: name each window individually --
    # each call is small (one window's pane context + summaries of others)
    # and the output is just a slug, so ID mismatch is impossible.
    windows = context["windows"]
    already_named: dict[str, str] = {}

    for i, window in enumerate(windows):
        slug = None
        for attempt in range(1, MAX_RETRIES + 1):
            set_status(
                session_id,
                "naming %(cur)d/%(total)d (%(att)d/%(max)d)"
                % {
                    "cur": i + 1,
                    "total": len(windows),
                    "att": attempt,
                    "max": MAX_RETRIES,
                },
            )
            slug, _error = ask_model_for_window_name(
                window,
                windows,
                already_named,
                opencode_context,
            )
            if slug:
                break

        # fallback: keep current name if all retries exhausted
        already_named[window["id"]] = slug if slug else window["name"]

    named_windows = [{"id": wid, "name": name} for wid, name in already_named.items()]

    # -- phase 2: ordering + session naming --
    # lightweight call with just {id, name} pairs — no pane details.
    # falls back to deterministic ordering if the model still chokes.
    expected_ids = {w["id"] for w in named_windows}
    plan = None

    for attempt in range(1, MAX_RETRIES + 1):
        set_status(
            session_id,
            "ordering (%(att)d/%(max)d)" % {"att": attempt, "max": MAX_RETRIES},
        )
        ordering, error = ask_model_for_ordering(
            named_windows,
            context["session_path"],
        )
        if error:
            continue
        validation_error = validate_ordering(ordering, expected_ids)
        if validation_error:
            continue

        # merge names into the ordering response
        name_lookup = {w["id"]: w["name"] for w in named_windows}
        plan = {
            "session": ordering["session"],
            "windows": [
                {"id": w["id"], "name": name_lookup[w["id"]], "index": w["index"]}
                for w in ordering["windows"]
            ],
        }
        break

    # deterministic fallback: convention priorities then alphabetical
    if not plan:
        plan = compute_fallback_ordering(named_windows, context["session_path"])

    write_cached_plan(cache_key, plan)
    apply_organization_plan(session_id, plan)
    run("set-option", "-t", session_id, "-u", "@torganize")


def cli() -> None:
    main()


if __name__ == "__main__":
    cli()
