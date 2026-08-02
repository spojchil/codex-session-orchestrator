#!/usr/bin/env python3
"""Inspect local Codex session JSONL files.

Field structure comes from `session_meta` on the first line of each rollout file, not from any
formatted output:

  id                 own uuid, matches the filename, never repeats
  session_id         thread id — see the table below; it repeats across files only for subagents
  forked_from_id     direct parent, set by both forks and subagents
  parent_thread_id   set by subagents only
  source             "cli" / "vscode" / "exec", or {"subagent": ...} for a Codex-spawned subagent

Verified against the meta of five related sessions:

  kind      id    session_id        forked_from_id  parent_thread_id
  root      own   == own            —               —
  fork      new   == own (new)      parent          —
  subagent  new   == parent's       parent          parent

So a fork starts a new thread while a subagent stays inside the parent's. Listing by `session_id`
collapses a parent and its subagents into one apparent row, which reads as duplicates. `id` is the
identity, `forked_from_id` is the lineage, and `parent_thread_id` separates the two kinds.

Lineage has a second producer: `state_5.sqlite`'s `thread_spawn_edges` records subagent
parent/child edges (but not fork edges), so `--tree` reads both.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# A session writes on every event, so a recent mtime means "probably still running". Only a hint:
# a stalled process and a finished one look the same once the window passes.
LIVE_WINDOW_S = 90

# Substrings used to skip json parsing for lines that cannot matter to a summary. Sessions reach
# tens of megabytes, and `-l` touches every one of them.
SUMMARY_HINTS = ('"user_message"', '"session_meta"')


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def sessions_root() -> Path:
    return codex_home() / "sessions"


def archived_root() -> Path:
    return codex_home() / "archived_sessions"


def iter_session_files(include_archived=False):
    roots = [sessions_root()]
    if include_archived:
        roots.append(archived_root())
    for root in roots:
        if root.exists():
            yield from root.rglob("rollout-*.jsonl")


def load_jsonl(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue


def read_meta(path: Path) -> dict:
    """Reads only the leading session_meta. Cheap enough to run over every session file."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    return {}
                if obj.get("type") == "session_meta":
                    return obj.get("payload", {}) or {}
                return {}
    except OSError:
        return {}
    return {}


def scan_summary(path: Path) -> tuple[int, str]:
    """Counts rounds and keeps the last prompt, parsing only lines that can contain one."""
    rounds = 0
    last_prompt = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                if not any(hint in raw for hint in SUMMARY_HINTS):
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                payload = obj.get("payload", {})
                if obj.get("type") == "event_msg" and payload.get("type") == "user_message":
                    rounds += 1
                    last_prompt = payload.get("message") or ""
    except OSError:
        pass
    return rounds, last_prompt


def last_token_info(path: Path) -> dict:
    """Last token_count of the file: context window occupancy and cumulative usage.

    Reads only the trailing chunk — token_count events are frequent, so the last one is close
    to the end even in multi-megabyte sessions.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - 300_000))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return {}
    info = {}
    for raw in tail.splitlines():
        if '"token_count"' not in raw:
            continue
        try:
            payload = json.loads(raw).get("payload") or {}
        except json.JSONDecodeError:
            continue
        candidate = payload.get("info") or {}
        if candidate:
            info = candidate
    return info


def spawn_edges() -> dict:
    """child_thread_id -> parent_thread_id from state_5.sqlite (subagent edges only)."""
    db_path = codex_home() / "state_5.sqlite"
    if not db_path.exists():
        return {}
    try:
        db = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        edges = dict(db.execute("select child_thread_id, parent_thread_id from thread_spawn_edges"))
        db.close()
        return edges
    except sqlite3.Error:
        return {}


def text_from_content(content):
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict):
            if block.get("type") in ("input_text", "output_text", "text"):
                parts.append(block.get("text", ""))
    return "\n".join(p for p in parts if p)


def activity_label(typ: str, payload: dict) -> str | None:
    """Names one unit of work. Codex records tool use several different ways.

    `exec_command_begin` / `exec_command_end` are handled but were not observed in any local
    session: on this machine shell work shows up as `function_call` / `custom_tool_call` instead.
    """
    if typ == "response_item":
        ptype = payload.get("type")
        if ptype == "function_call":
            return payload.get("name") or "function_call"
        if ptype == "custom_tool_call":
            return payload.get("name") or "custom_tool_call"
        return None
    if typ == "event_msg":
        ptype = payload.get("type")
        if ptype == "exec_command_begin":
            return "exec"
        if ptype == "web_search_end":
            return "web_search"
        if ptype == "mcp_tool_call_end":
            return "mcp"
        if ptype in ("patch_apply_begin", "patch_apply_end"):
            return "patch"
        if ptype in ("compacted", "context_compacted"):
            return "compact"
    return None


def activity_detail(typ: str, payload: dict) -> str:
    if typ == "response_item":
        if payload.get("type") == "function_call":
            return str(payload.get("arguments") or "")
        if payload.get("type") == "custom_tool_call":
            return str(payload.get("input") or "")
    if typ == "event_msg" and payload.get("type") == "web_search_end":
        return str(payload.get("query") or "")
    return ""


def find_session_path(session_or_path: str) -> Path:
    """Resolves by own `id` first, because that is the unique one.

    Falling back to `session_id` would silently pick whichever file in a lineage was written last,
    which is how a fork gets mistaken for its parent. Archived sessions are searched too.
    """
    candidate = Path(session_or_path)
    if candidate.exists():
        return candidate

    by_id, by_thread = [], []
    for path in iter_session_files(include_archived=True) or []:
        meta = read_meta(path)
        own = meta.get("id") or ""
        thread = meta.get("session_id") or ""
        if session_or_path in path.name or (own and session_or_path in own):
            by_id.append(path)
        elif thread and session_or_path in thread:
            by_thread.append(path)

    matches = by_id or by_thread
    if not matches:
        raise FileNotFoundError(f"No Codex session matched: {session_or_path}")
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if by_thread and not by_id and len(matches) > 1:
        print(
            f"note: {session_or_path} matched {len(matches)} files by session_id (a lineage, not an "
            f"identity); showing the most recent. Use the file's own `id` to be exact.",
            file=sys.stderr,
        )
    return matches[0]


def parse_session(path: Path):
    meta = {}
    rounds = []
    current = None

    for obj in load_jsonl(path):
        typ = obj.get("type")
        payload = obj.get("payload", {})

        if typ == "session_meta":
            meta = payload
            continue

        if typ == "event_msg" and payload.get("type") == "user_message":
            if current:
                rounds.append(current)
            current = {
                "prompt": payload.get("message") or "",
                "assistant": [],
                "activity": [],
                "errors": [],
                "last_agent_message": None,
            }
            continue

        if current is None:
            continue

        if typ == "event_msg":
            ptype = payload.get("type")
            if ptype == "agent_message":
                current["assistant"].append(payload.get("message") or "")
            elif ptype == "task_complete":
                current["last_agent_message"] = payload.get("last_agent_message")
            elif ptype in ("error", "turn_aborted", "task_failed"):
                current["errors"].append(payload)
        elif typ == "response_item":
            if payload.get("type") == "message" and payload.get("role") == "assistant":
                text = text_from_content(payload.get("content"))
                if text:
                    current["assistant"].append(text)

        label = activity_label(typ, payload)
        if label:
            current["activity"].append((label, activity_detail(typ, payload)))

    if current:
        rounds.append(current)

    return meta, rounds


def is_live(mtime: float) -> bool:
    return (time.time() - mtime) <= LIVE_WINDOW_S


def short(value: str, width: int = 8) -> str:
    return (value or "")[:width] if value else "—"


def trim(text, width):
    text = (text or "").replace("\n", " ")
    return text if len(text) <= width else text[: width - 3] + "..."


def collect_rows(project=None, include_archived=False):
    rows = []
    project_norm = str(Path(project).resolve()) if project else None
    for path in iter_session_files(include_archived) or []:
        meta = read_meta(path)
        cwd = meta.get("cwd", "")
        if project_norm:
            try:
                if str(Path(cwd).resolve()) != project_norm:
                    continue
            except OSError:
                continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        count, last_prompt = scan_summary(path)
        source = meta.get("source")
        rows.append({
            "mtime": mtime,
            "id": meta.get("id") or path.stem[-36:],
            "thread": meta.get("session_id") or "",
            "forked_from": meta.get("forked_from_id") or meta.get("parent_thread_id") or "",
            "subagent": isinstance(source, dict) and "subagent" in source,
            "archived": "archived_sessions" in str(path),
            "rounds": count,
            "cwd": cwd,
            "last_prompt": last_prompt,
            "path": path,
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


def list_sessions(project=None, limit=30, include_archived=False):
    rows = collect_rows(project, include_archived)
    show_cwd = project is None
    header = f"{'':2s} {'time':11s} {'rnd':>4s} {'id':36s} {'forked':8s}"
    if show_cwd:
        header += f" {'cwd':28s}"
    print(header + " last prompt")
    print("-" * (len(header) + 12))
    for row in rows[:limit]:
        mark = "*" if is_live(row["mtime"]) else ("^" if row["subagent"] else " ")
        if row["archived"]:
            mark = "a"
        stamp = datetime.fromtimestamp(row["mtime"]).strftime("%m-%d %H:%M")
        line = f"{mark:2s} {stamp:11s} {row['rounds']:4d} {row['id']:36s} {short(row['forked_from']):8s}"
        if show_cwd:
            line += f" {trim(row['cwd'], 28):28s}"
        print(line + " " + trim(row["last_prompt"], 60))
    if rows:
        print()
        print("* = written within the last 90s (probably running)   "
              "^ = Codex-spawned subagent   a = archived")


def print_tree(project=None, limit=60, include_archived=False):
    """Groups by lineage from both producers: forked_from_id (rollout meta, forks and
    subagents) and thread_spawn_edges (sqlite, subagents only)."""
    rows = collect_rows(project, include_archived)[:limit]
    edges = spawn_edges()
    by_id = {row["id"]: row for row in rows}
    children = {}
    roots = []
    for row in rows:
        parent = row["forked_from"] or edges.get(row["id"]) or ""
        if parent and parent in by_id and parent != row["id"]:
            children.setdefault(parent, []).append(row)
        else:
            roots.append(row)

    def emit(row, depth):
        mark = "*" if is_live(row["mtime"]) else ("^" if row["subagent"] else " ")
        stamp = datetime.fromtimestamp(row["mtime"]).strftime("%m-%d %H:%M")
        indent = "  " * depth + ("└ " if depth else "")
        print(f"{mark} {stamp}  {row['rounds']:4d}  {indent}{row['id']}  {trim(row['last_prompt'], 40)}")
        for child in sorted(children.get(row["id"], []), key=lambda r: r["mtime"]):
            emit(child, depth + 1)

    print(f"{'':1s} {'time':11s}  {'rnd':>4s}  lineage")
    print("-" * 78)
    for root in roots:
        emit(root, 0)
    orphans = [r for r in rows
               if (r["forked_from"] or edges.get(r["id"])) and
               (r["forked_from"] or edges.get(r["id"])) not in by_id]
    if orphans:
        print()
        print("forked from a session outside this listing (widen --limit or drop -p):")
        for row in orphans:
            print(f"   {row['id']}  <- {row['forked_from'] or edges.get(row['id'])}")


GREP_HINTS = ('"user_message"', '"agent_message"')


def grep_sessions(pattern: str, project=None, limit=50, include_archived=False):
    """Content search across sessions: user prompts and agent replies."""
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error:
        rx = re.compile(re.escape(pattern), re.IGNORECASE)
    project_norm = str(Path(project).resolve()) if project else None
    files = sorted(iter_session_files(include_archived) or [],
                   key=lambda p: p.stat().st_mtime, reverse=True)
    printed = 0
    for path in files:
        if printed >= limit:
            print(f"... stopped at {limit} matches (raise --limit for more)")
            return
        meta = None
        session_hits = 0
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    if session_hits >= 3:
                        break
                    if not any(h in raw for h in GREP_HINTS):
                        continue
                    if not rx.search(raw):
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    payload = obj.get("payload", {})
                    if obj.get("type") != "event_msg" or \
                            payload.get("type") not in ("user_message", "agent_message"):
                        continue
                    text = payload.get("message") or ""
                    match = rx.search(text)
                    if not match:
                        continue
                    if meta is None:
                        meta = read_meta(path)
                        cwd = meta.get("cwd", "")
                        if project_norm:
                            try:
                                if str(Path(cwd).resolve()) != project_norm:
                                    break
                            except OSError:
                                break
                        stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%m-%d %H:%M")
                        print(f"{stamp}  {meta.get('id', path.stem[-36:])}  {trim(cwd, 40)}")
                    kind = "user " if payload["type"] == "user_message" else "agent"
                    start = max(0, match.start() - 40)
                    print(f"    {kind}: ...{trim(text[start:start + 160], 120)}")
                    session_hits += 1
                    printed += 1
        except OSError:
            continue
    if printed == 0:
        print("no matches")


def print_rounds(path: Path, tail=0, show_activity=0):
    meta, rounds = parse_session(path)
    print(f"id:             {meta.get('id', '')}")
    print(f"session_id:     {meta.get('session_id', '')}")
    if meta.get("forked_from_id") or meta.get("parent_thread_id"):
        print(f"forked_from_id: {meta.get('forked_from_id') or meta.get('parent_thread_id')}")
    source = meta.get("source")
    if isinstance(source, dict) and "subagent" in source:
        print("source:         subagent")
    print(f"cwd:            {meta.get('cwd', '')}")
    print(f"file:           {path}")
    tokens = last_token_info(path)
    if tokens:
        last = tokens.get("last_token_usage") or {}
        total = tokens.get("total_token_usage") or {}
        window = tokens.get("model_context_window")
        line = f"tokens:         total {total.get('total_tokens', 0):,}"
        if window and last.get("input_tokens") is not None:
            pct = round(last["input_tokens"] * 100 / window, 1)
            line += f"   context {last['input_tokens']:,}/{window:,} ({pct}%)"
        print(line)
    try:
        age = int(time.time() - path.stat().st_mtime)
        print(f"last write:     {age}s ago" + ("  (probably running)" if age <= LIVE_WINDOW_S else ""))
    except OSError:
        pass
    print()
    items = rounds[-tail:] if tail else rounds
    offset = len(rounds) - len(items)
    for i, rnd in enumerate(items, offset + 1):
        reply = rnd.get("last_agent_message") or (rnd["assistant"][-1] if rnd["assistant"] else "")
        print(f"[{i}/{len(rounds)}] user: {trim(rnd['prompt'], 160)}")
        print(f"        answer: {trim(reply, 220)}")
        if rnd["activity"]:
            kinds = {}
            for label, _detail in rnd["activity"]:
                kinds[label] = kinds.get(label, 0) + 1
            summary = ", ".join(f"{k}x{v}" if v > 1 else k for k, v in sorted(kinds.items()))
            print(f"        activity: {len(rnd['activity'])} ({summary})")
            for label, detail in rnd["activity"][-show_activity:] if show_activity else []:
                print(f"          - {label}: {trim(detail, 140)}")
        if rnd["errors"]:
            print(f"        errors: {len(rnd['errors'])}")
        print()


def print_last_message(path: Path):
    _meta, rounds = parse_session(path)
    for rnd in reversed(rounds):
        reply = rnd.get("last_agent_message") or (rnd["assistant"][-1] if rnd["assistant"] else "")
        if reply:
            print(reply)
            return
    raise RuntimeError("No final assistant message found.")


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="Inspect Codex session JSONL files.")
    parser.add_argument("session", nargs="?", help="Own id, partial id, or rollout JSONL path")
    parser.add_argument("-l", "--list-sessions", action="store_true", help="List recent sessions")
    parser.add_argument("--tree", action="store_true", help="List sessions grouped by fork lineage")
    parser.add_argument("-g", "--grep", metavar="PATTERN",
                        help="Search prompts and replies across sessions (regex, case-insensitive)")
    parser.add_argument("-p", "--project", help="Filter by project directory")
    parser.add_argument("--archived", action="store_true",
                        help="Include archived_sessions in -l / --tree / --grep")
    parser.add_argument("-n", "--tail", type=int, default=0, help="Show only recent N rounds")
    parser.add_argument(
        "--activity",
        type=int,
        nargs="?",
        const=10,
        default=0,
        metavar="N",
        help="Also print the last N tool calls / searches per round (default 10)",
    )
    parser.add_argument("--last-message", action="store_true", help="Print only the latest final answer")
    parser.add_argument("--limit", type=int, default=30, help="Maximum sessions/matches to list")
    args = parser.parse_args()

    if args.grep:
        grep_sessions(args.grep, args.project, max(args.limit, 50), args.archived)
        return

    if args.tree:
        print_tree(args.project, max(args.limit, 60), args.archived)
        return

    if args.list_sessions:
        list_sessions(args.project, args.limit, args.archived)
        return

    if not args.session:
        parser.error("provide a session id/path, or use -l / --tree / --grep")

    path = find_session_path(args.session)
    if args.last_message:
        print_last_message(path)
    else:
        print_rounds(path, args.tail, args.activity)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
