#!/usr/bin/env python3
"""Run or resume a Codex exec session and print a clean result."""

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def iter_session_files():
    root = codex_home() / "sessions"
    if not root.exists():
        return
    yield from root.rglob("rollout-*.jsonl")


def load_jsonl(path: Path):
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def find_new_session_id(project_dir: Path, started_at: float) -> str | None:
    matches = []
    project_resolved = str(project_dir.resolve())
    for path in iter_session_files() or []:
        try:
            if path.stat().st_mtime + 2 < started_at:
                continue
        except OSError:
            continue
        for obj in load_jsonl(path):
            if obj.get("type") != "session_meta":
                continue
            payload = obj.get("payload", {})
            cwd = payload.get("cwd")
            if cwd and str(Path(cwd).resolve()) != project_resolved:
                break
            # `id` first: it is the per-file identity and never repeats, while `session_id` is the
            # thread and is shared by a session and every subagent it spawns. Preferring the thread
            # would hand back an id that resumes somebody else's file.
            sid = payload.get("id") or payload.get("session_id")
            if sid:
                matches.append((path.stat().st_mtime, sid))
            break
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


def session_id_from_json_log(path: Path) -> str | None:
    for obj in load_jsonl(path):
        for key in ("id", "thread_id", "session_id"):
            if obj.get(key):
                return obj[key]
        payload = obj.get("payload") or {}
        for key in ("id", "thread_id", "session_id"):
            if payload.get(key):
                return payload[key]
    return None


def fork_session(parent_id: str) -> str:
    """Copies a session into a new one that carries `forked_from_id`, and returns the new id.

    There is no non-interactive fork in the CLI: `codex fork` only drives the TUI, and
    `--ephemeral` is silently dropped on the `resume` path — a resumed turn lands in the parent
    file even with the flag set, so it cannot be used to keep a parent clean.

    The field convention below was read off five related sessions' `session_meta`:

      kind      id    session_id      forked_from_id  parent_thread_id
      fork      new   == own (new)    parent          absent
      subagent  new   == parent's     parent          parent

    Verified end to end: the copy resumes, inherits the parent's context, and the parent file's
    md5 is unchanged afterwards. Two caveats. The CLI locates a file that is not yet in its index
    by scanning filenames for the uuid, so the filename must carry the new id; on finding it the
    CLI read-repairs, inserting a row into `threads` in `state_5.sqlite`. And `session_meta` is an
    internal structure rather than a published contract — the meta carries `history_mode`, so
    re-verify this after a CLI upgrade.
    """
    source = None
    for path in iter_session_files() or []:
        for obj in load_jsonl(path):
            if obj.get("type") != "session_meta":
                break
            own = (obj.get("payload") or {}).get("id") or ""
            if own == parent_id or (parent_id and parent_id in own):
                source = path
            break
        if source:
            break
    if source is None:
        raise RuntimeError(f"no session file found for parent id: {parent_id}")

    lines = source.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    if not lines:
        raise RuntimeError(f"parent session file is empty: {source}")
    head = json.loads(lines[0])
    payload = head.get("payload") or {}
    resolved_parent = payload.get("id") or parent_id

    ms = int(time.time() * 1000)
    digits = f"{ms:012x}7{secrets.token_hex(2)[1:]}{secrets.randbits(16) | 0x8000:04x}{secrets.token_hex(6)}"
    new_id = "-".join([digits[0:8], digits[8:12], digits[12:16], digits[16:20], digits[20:32]])

    payload["id"] = new_id
    payload["session_id"] = new_id
    payload["forked_from_id"] = resolved_parent
    payload.pop("parent_thread_id", None)
    payload["timestamp"] = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )
    head["payload"] = payload
    lines[0] = json.dumps(head, ensure_ascii=False, separators=(",", ":")) + "\n"

    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    destination = source.parent / f"rollout-{stamp}-{new_id}.jsonl"
    destination.write_text("".join(lines), encoding="utf-8", newline="")
    return new_id


def build_args(args, last_message_path: Path):
    codex_cmd = shutil.which("codex")
    if not codex_cmd:
        raise RuntimeError("codex CLI was not found on PATH")
    if os.name == "nt" and codex_cmd.lower().endswith(".ps1"):
        cmd = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            codex_cmd,
            "exec",
        ]
    else:
        cmd = [codex_cmd, "exec"]

    # Every option below belongs to `exec`, so it has to precede the `resume` subcommand.
    # `resume` accepts only its own flags: putting `--sandbox` after it fails with
    # "unexpected argument '--sandbox' found". `--skip-git-repo-check` happens to be accepted in
    # both positions, which is why the wrong order stayed hidden on the non-resume path.
    if not args.no_skip_git_repo_check:
        cmd.append("--skip-git-repo-check")
    if args.sandbox:
        cmd.extend(["--sandbox", args.sandbox])
    if args.model:
        cmd.extend(["--model", args.model])
    if args.reasoning_effort:
        # Quoted so the value parses as a TOML string, matching the CLI's own `-c model="o3"` example.
        cmd.extend(["--config", f'model_reasoning_effort="{args.reasoning_effort}"'])
    if args.profile:
        cmd.extend(["--profile", args.profile])
    for extra in args.add_dir:
        cmd.extend(["--add-dir", extra])
    if args.ignore_user_config:
        cmd.append("--ignore-user-config")

    cmd.extend(["--json", "--output-last-message", str(last_message_path)])

    if args.session_id or args.last:
        cmd.append("resume")
        cmd.append(args.session_id if args.session_id else "--last")

    cmd.append("-")
    return cmd


def main():
    parser = argparse.ArgumentParser(description="Run Codex exec/resume with clean output.")
    parser.add_argument("prompt", nargs="?", help="Prompt to send. If omitted, read stdin.")
    parser.add_argument("--session-id", help="Resume this Codex session id.")
    parser.add_argument(
        "--fork-from",
        metavar="PARENT_ID",
        help="Copy that session into a new one (forked_from_id set) and run against the copy, "
             "so the parent stays untouched.",
    )
    parser.add_argument("--last", action="store_true", help="Resume the most recent session.")
    parser.add_argument(
        "--add-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="Extra directory the sandbox may access. Repeatable. Prefer this over widening -C.",
    )
    parser.add_argument("-C", "--project-dir", default=os.getcwd(), help="Directory to run Codex in.")
    parser.add_argument("--no-skip-git-repo-check", action="store_true", help="Do not add --skip-git-repo-check.")
    parser.add_argument("--quiet-final-only", action="store_true", help="Print only the final answer.")
    parser.add_argument("--show-log", action="store_true", help="Print metadata and captured JSONL log path.")
    parser.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"])
    parser.add_argument("--model")
    parser.add_argument(
        "--reasoning-effort",
        choices=["low", "medium", "high", "xhigh", "max", "ultra"],
        help="Maps to --config model_reasoning_effort. max/ultra need a GPT-5.6 model.",
    )
    parser.add_argument("--profile")
    parser.add_argument("--ignore-user-config", action="store_true")
    parsed = parser.parse_args()

    chosen = [name for name, value in
              (("--session-id", parsed.session_id), ("--last", parsed.last), ("--fork-from", parsed.fork_from))
              if value]
    if len(chosen) > 1:
        parser.error(f"use only one of {', '.join(chosen)}")

    prompt = parsed.prompt if parsed.prompt is not None else sys.stdin.read()
    if not prompt.strip():
        parser.error("prompt is empty")

    project_dir = Path(parsed.project_dir).resolve()
    if not project_dir.exists():
        parser.error(f"project directory does not exist: {project_dir}")
    if project_dir == Path.home():
        # The managed Windows sandbox cannot build a restricted token when the write root is the
        # user profile: every child process dies with SetTokenInformation(TokenDefaultDacl) 1344.
        parser.error(
            "refusing to run with the home directory as -C: the managed sandbox cannot spawn any "
            "child process there. Point -C at a project or worktree, and use --add-dir for paths "
            "outside it."
        )

    forked_from = None
    if parsed.fork_from:
        forked_from = parsed.fork_from
        parsed.session_id = fork_session(parsed.fork_from)

    temp_dir = Path(tempfile.gettempdir()) / "codex-session-orchestrator"
    temp_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{int(time.time() * 1000)}"
    json_log_path = temp_dir / f"events-{stamp}.jsonl"
    last_message_path = temp_dir / f"last-message-{stamp}.md"

    cmd = build_args(parsed, last_message_path)
    started_at = time.time()

    with json_log_path.open("w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(project_dir),
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )

    session_id = (
        parsed.session_id
        or session_id_from_json_log(json_log_path)
        or find_new_session_id(project_dir, started_at)
    )

    if not parsed.quiet_final_only:
        print(f"session_id: {session_id or ''}")
        if forked_from:
            print(f"forked_from: {forked_from}")
    if parsed.show_log:
        print(f"json_log: {json_log_path}")
        print(f"last_message: {last_message_path}")
        print(f"exit_code: {proc.returncode}")
    if (not parsed.quiet_final_only) or parsed.show_log:
        print()

    if last_message_path.exists():
        print(last_message_path.read_text(encoding="utf-8", errors="replace").rstrip())
    else:
        print(json_log_path.read_text(encoding="utf-8", errors="replace").rstrip())

    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
