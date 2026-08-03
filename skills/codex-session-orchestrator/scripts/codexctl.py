#!/usr/bin/env python3
"""codexctl — dispatch and control Codex CLI runs.

Architecture: `dispatch` spawns a detached supervisor; the supervisor is the parent of the
codex process, owns a Windows Job Object around the whole tree, parses the `--json` event
stream into an atomically-updated state file, samples perf counters off the job, applies
health rules, and leaves a terminal record even if the dispatching shell is long gone.

Run registry: ~/.codex-orchestrator/runs/<run_id>/
    run.json        static config (command, cwd, options)
    prompt.txt      the prompt handed to codex on stdin
    state.json      live state, atomic replace on every update
    events.jsonl    every codex event + arrival timestamp
    alerts.jsonl    health rule hits
    result.json     terminal record (exit code, reason, resume command)
    last-message.md codex's final reply (--output-last-message)
    control.json    client -> supervisor commands (cancel)
    supervisor.log  supervisor + codex stderr

Standard library only.
"""

import argparse
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SELF = Path(__file__).resolve()
IS_WIN = os.name == "nt"

# The supervisor is detached (no console), so any console child it spawns without this flag
# gets a fresh console window — a visible flash on every periodic git snapshot.
NO_WINDOW = 0x08000000 if IS_WIN else 0

TERMINAL_PHASES = ("completed", "failed", "cancelled")

# ---------------------------------------------------------------------------- paths & io


def orch_home() -> Path:
    return Path(os.environ.get("CODEX_ORCH_HOME") or Path.home() / ".codex-orchestrator")


def runs_root() -> Path:
    return orch_home() / "runs"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def atomic_write_json(path: Path, data) -> None:
    """Unique temp name + retried replace. On Windows, os.replace fails with WinError 5
    for as long as any reader (a status poll, an indexer) holds the destination open —
    Python readers don't pass FILE_SHARE_DELETE — so a fixed temp name and a single
    attempt took the whole supervisor down. Observed in production three times."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{secrets.token_hex(3)}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    delay = 0.01
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            if attempt == 7:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def append_jsonl(path: Path, obj) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def new_run_id() -> str:
    return "r-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def resolve_run_dir(token: str) -> Path:
    """Accepts a full run id, a unique prefix, or `latest`."""
    root = runs_root()
    if not root.exists():
        raise SystemExit("no runs recorded yet")
    if token in ("latest", "last"):
        candidates = sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            raise SystemExit("no runs recorded yet")
        return candidates[0]
    exact = root / token
    if exact.exists():
        return exact
    matches = [p for p in root.iterdir() if p.name.startswith(token)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        # A session id resolves to that session's CURRENT run via the index.
        index = read_json(orch_home() / "sessions.json") or {}
        for sid, meta in index.items():
            if sid.startswith(token) or token in sid:
                current = meta.get("current_run_id")
                if current and (root / current).exists():
                    return root / current
        raise SystemExit(f"no run matches: {token}")
    raise SystemExit(f"ambiguous run id {token}: " + ", ".join(p.name for p in matches[:5]))


def trim(text, width):
    text = (text or "").replace("\n", " ").replace("\r", "")
    return text if len(text) <= width else text[: width - 3] + "..."


def fmt_secs(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def fmt_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# ---------------------------------------------------------------------------- windows job object

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.windll.kernel32

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _BASIC_ACCOUNTING(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class _BASIC_AND_IO(ctypes.Structure):
        _fields_ = [("BasicInfo", _BASIC_ACCOUNTING), ("IoInfo", _IO_COUNTERS)]

    class _PID_LIST(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * 512),
        ]

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    def job_name_for(run_id: str) -> str:
        return f"Local\\codexctl-{run_id}"

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.OpenJobObjectW.restype = wintypes.HANDLE
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenThread.restype = wintypes.HANDLE

    class JobObject:
        """Owns the codex process tree: kill switch and perf accounting in one handle."""

        def __init__(self, run_id: str):
            self.handle = _k32.CreateJobObjectW(None, job_name_for(run_id))
            if not self.handle:
                raise OSError("CreateJobObjectW failed")
            info = _EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            _k32.SetInformationJobObject(wintypes.HANDLE(self.handle), 9,
                                         ctypes.byref(info), ctypes.sizeof(info))

        @classmethod
        def open_existing(cls, run_id: str):
            handle = _k32.OpenJobObjectW(0x1F001F, False, job_name_for(run_id))
            if not handle:
                return None
            job = cls.__new__(cls)
            job.handle = handle
            return job

        def assign(self, process_handle: int) -> bool:
            return bool(_k32.AssignProcessToJobObject(wintypes.HANDLE(self.handle),
                                                      wintypes.HANDLE(process_handle)))

        def terminate(self, code: int = 130) -> bool:
            return bool(_k32.TerminateJobObject(wintypes.HANDLE(self.handle), code))

        def sample(self) -> dict:
            out = {}
            handle = wintypes.HANDLE(self.handle)
            acct = _BASIC_AND_IO()
            if _k32.QueryInformationJobObject(handle, 8, ctypes.byref(acct), ctypes.sizeof(acct), None):
                out["cpu_seconds"] = round(
                    (acct.BasicInfo.TotalUserTime + acct.BasicInfo.TotalKernelTime) / 10_000_000, 1)
                out["read_bytes"] = acct.IoInfo.ReadTransferCount
                out["write_bytes"] = acct.IoInfo.WriteTransferCount
                out["total_processes"] = acct.BasicInfo.TotalProcesses
                out["active_processes"] = acct.BasicInfo.ActiveProcesses
            ext = _EXTENDED_LIMIT()
            if _k32.QueryInformationJobObject(handle, 9, ctypes.byref(ext), ctypes.sizeof(ext), None):
                out["peak_memory_bytes"] = ext.PeakJobMemoryUsed
            pids = _PID_LIST()
            if _k32.QueryInformationJobObject(handle, 3, ctypes.byref(pids), ctypes.sizeof(pids), None):
                out["pids"] = list(pids.ProcessIdList[: pids.NumberOfProcessIdsInList])
            return out

    def resume_process_threads(pid: int) -> None:
        snap = _k32.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if snap in (0, -1):
            return
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(_THREADENTRY32)
        ok = _k32.Thread32First(snap, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                thread = _k32.OpenThread(0x2, False, entry.th32ThreadID)  # THREAD_SUSPEND_RESUME
                if thread:
                    _k32.ResumeThread(thread)
                    _k32.CloseHandle(thread)
            ok = _k32.Thread32Next(snap, ctypes.byref(entry))
        _k32.CloseHandle(snap)

    def pid_alive(pid: int) -> bool:
        handle = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = wintypes.DWORD()
        alive = _k32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259
        _k32.CloseHandle(handle)
        return bool(alive)

else:
    def pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------- codex invocation


def locate_codex() -> str:
    cmd = shutil.which("codex")
    if not cmd:
        raise SystemExit("codex CLI was not found on PATH")
    return cmd


def codex_argv(extra: list) -> list:
    cmd = locate_codex()
    if IS_WIN and cmd.lower().endswith(".ps1"):
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", cmd] + extra
    return [cmd] + extra


def build_exec_cmd(cfg: dict, last_message_path: Path) -> list:
    # Every option belongs to `exec` and must precede the `resume` subcommand: `resume` only
    # accepts its own flags (--sandbox after it fails with "unexpected argument").
    argv = ["exec"]
    if not cfg.get("no_skip_git_repo_check"):
        argv.append("--skip-git-repo-check")
    if cfg.get("sandbox"):
        argv += ["--sandbox", cfg["sandbox"]]
    if cfg.get("model"):
        argv += ["--model", cfg["model"]]
    if cfg.get("reasoning_effort"):
        argv += ["--config", f'model_reasoning_effort="{cfg["reasoning_effort"]}"']
    if cfg.get("profile"):
        argv += ["--profile", cfg["profile"]]
    for extra in cfg.get("add_dirs") or []:
        argv += ["--add-dir", extra]
    argv += ["--json", "--output-last-message", str(last_message_path)]
    if cfg.get("resume_id"):
        argv += ["resume", cfg["resume_id"]]
    argv.append("-")
    return codex_argv(argv)


def iter_session_files():
    for root in (codex_home() / "sessions", codex_home() / "archived_sessions"):
        if root.exists():
            yield from root.rglob("rollout-*.jsonl")


def find_rollout(session_id: str):
    hits = [p for p in iter_session_files() if session_id in p.name]
    if not hits:
        return None
    return max(hits, key=lambda p: p.stat().st_mtime)


def fork_session(parent_id: str) -> str:
    """Copies a session file into a new thread carrying forked_from_id; parent stays untouched.

    The CLI has no non-interactive fork and silently drops --ephemeral on resume. Field
    convention verified on real sessions: fork gets a new id, session_id == its own new id,
    forked_from_id = parent, no parent_thread_id. Filename must carry the new uuid.
    """
    source = None
    for path in iter_session_files():
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                head = json.loads(f.readline())
        except (OSError, json.JSONDecodeError):
            continue
        if head.get("type") != "session_meta":
            continue
        own = (head.get("payload") or {}).get("id") or ""
        if own == parent_id or (parent_id and parent_id in own):
            source = path
            break
    if source is None:
        raise SystemExit(f"no session file found for parent id: {parent_id}")

    lines = source.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    head = json.loads(lines[0])
    payload = head.get("payload") or {}
    resolved_parent = payload.get("id") or parent_id

    ms = int(time.time() * 1000)
    digits = f"{ms:012x}7{secrets.token_hex(2)[1:]}{secrets.randbits(16) | 0x8000:04x}{secrets.token_hex(6)}"
    new_id = "-".join([digits[0:8], digits[8:12], digits[12:16], digits[16:20], digits[20:32]])

    payload.update({"id": new_id, "session_id": new_id, "forked_from_id": resolved_parent})
    payload.pop("parent_thread_id", None)
    payload["timestamp"] = utcnow_iso()
    head["payload"] = payload
    lines[0] = json.dumps(head, ensure_ascii=False, separators=(",", ":")) + "\n"

    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    destination = source.parent / f"rollout-{stamp}-{new_id}.jsonl"
    destination.write_text("".join(lines), encoding="utf-8", newline="")
    return new_id


# ---------------------------------------------------------------------------- supervisor


class Supervisor:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.cfg = read_json(run_dir / "run.json") or {}
        self.state_path = run_dir / "state.json"
        self.events_path = run_dir / "events.jsonl"
        self.alerts_path = run_dir / "alerts.jsonl"
        self.state = read_json(self.state_path) or {}
        self.dirty = True
        self.last_flush = 0.0
        self.last_event_mono = time.monotonic()
        self.current_item = None
        self.cancel_req = None
        self.control_seen = 0.0
        self.alerted = set()
        self.health_current = {}
        self.rollout_path = None
        self.rollout_offset = 0
        self.rollout_mtime = 0.0
        self.last_activity_mono = time.monotonic()
        self.progress_offset = 0
        self.job = None
        self.proc = None
        self.saw_turn_complete = False
        self.turn_failed_error = None

    # -- state helpers

    def set(self, **kv):
        self.state.update(kv)
        self.dirty = True

    def flush(self, force=False):
        now = time.monotonic()
        if not force and (not self.dirty or now - self.last_flush < 0.5):
            return
        self.state["updated_at"] = utcnow_iso()
        try:
            atomic_write_json(self.state_path, self.state)
            self.dirty = False
        except OSError as exc:
            # A state-write failure must never end the supervisor: its death closes the
            # Job Object handle, and KILL_ON_JOB_CLOSE takes the codex tree down with it.
            # Leave dirty set; the next tick retries.
            print(f"state flush failed, will retry: {exc}", file=sys.stderr, flush=True)
        self.last_flush = now

    # Health has two kinds of entries. Condition rules (context_high, stalled, no_diff,
    # max_items, max_runtime) are re-evaluated every tick and AUTO-RESOLVE when the
    # condition clears — a compaction that brings context back down removes the alert
    # instead of leaving a stale warning. Point rules (path_violation, repeat_command)
    # are real violations and stay visible for the rest of the run.

    def _on_alert_action(self, rule: str):
        if self.cfg.get("on_alert") == "cancel" and not self.cancel_req:
            self.request_cancel(grace=20, reason=f"auto: {rule}")

    def _publish_health(self, history_record=None):
        health = self.state.setdefault("health", {"status": "ok", "current": [], "history": []})
        if history_record:
            health["history"] = (health.get("history") or [])[-19:] + [history_record]
        health["current"] = list(self.health_current.values())
        health["status"] = "warning" if health["current"] else "ok"
        self.dirty = True

    def point_alert(self, rule: str, message: str):
        if rule in self.alerted:
            return
        self.alerted.add(rule)
        record = {"ts": utcnow_iso(), "rule": rule, "message": message, "kind": "event"}
        append_jsonl(self.alerts_path, record)
        self.health_current[rule] = {"rule": rule, "message": message, "since": record["ts"]}
        self._publish_health(record)
        self._on_alert_action(rule)

    def eval_condition(self, rule: str, active: bool, message: str):
        held = rule in self.health_current
        if active and not held:
            record = {"ts": utcnow_iso(), "rule": rule, "message": message, "kind": "condition"}
            append_jsonl(self.alerts_path, record)
            self.health_current[rule] = {"rule": rule, "message": message, "since": record["ts"]}
            self._publish_health(record)
            self._on_alert_action(rule)
        elif active and held:
            self.health_current[rule]["message"] = message
            self._publish_health()
        elif not active and held:
            self.health_current.pop(rule, None)
            record = {"ts": utcnow_iso(), "rule": rule, "resolved": True}
            append_jsonl(self.alerts_path, record)
            self._publish_health(record)

    def request_cancel(self, grace: int, reason: str):
        self.cancel_req = {"deadline": time.monotonic() + max(0, grace), "reason": reason}
        self.set(cancel_requested=reason)

    # -- git snapshots

    def git(self, *args, timeout=10):
        try:
            out = subprocess.run(
                ["git", "-C", self.cfg["cwd"], *args],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, creationflags=NO_WINDOW)
            return out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def snapshot_git(self):
        """Runs once at finalize only. Live working-tree state is deliberately not polled:
        the orchestrator checks `git status` itself when it wants ground truth."""
        if not self.cfg.get("has_git"):
            return
        stat = self.git("diff", "--shortstat")
        status = self.git("status", "--porcelain")
        self.set(diff_stat=stat or "", dirty_files=len(status.splitlines()) if status else 0)

    # -- event handling

    def on_event(self, ts: str, obj: dict):
        typ = obj.get("type")
        self.last_event_mono = time.monotonic()
        self.set(last_event_at=ts, last_event_type=typ)
        counts = self.state.setdefault("counts", {})

        if typ == "thread.started":
            sid = obj.get("thread_id")
            self.set(session_id=sid, phase="requesting_model",
                     recommended_resume=(f'python "{SELF}" resume {sid} '
                                         f'-C "{self.cfg["cwd"]}" --prompt-file <path>'))
        elif typ == "turn.started":
            counts["turns"] = counts.get("turns", 0) + 1
            self.set(phase="requesting_model")
        elif typ == "turn.completed":
            self.saw_turn_complete = True
            usage = obj.get("usage") or {}
            self.state.setdefault("tokens", {})["turn_usage"] = usage
            self.set(phase="completed")
        elif typ == "turn.failed":
            self.turn_failed_error = (obj.get("error") or {}).get("message") or "turn failed"
            self.set(phase="failed", last_error=self.turn_failed_error)
        elif typ == "error":
            self.set(last_error=obj.get("message") or "")
        elif typ in ("item.started", "item.updated", "item.completed"):
            self.on_item(ts, typ, obj.get("item") or {})
        elif typ == "unknown" or typ is None:
            self.set(schema_drift=True)
        else:
            # Forward-compat sentinel: exec_events is an internal contract, flag drift instead
            # of failing when a new CLI version introduces event types we do not know.
            if typ not in ("thread.started",):
                self.state.setdefault("unknown_events", {})
                self.state["unknown_events"][typ] = self.state["unknown_events"].get(typ, 0) + 1
                self.dirty = True

    def on_item(self, ts: str, typ: str, item: dict):
        kind = item.get("type")
        counts = self.state.setdefault("counts", {})
        if typ == "item.started":
            counts["items"] = counts.get("items", 0) + 1
            self.current_item = {"kind": kind, "started": time.monotonic(),
                                 "detail": trim(item.get("command") or "", 300)}
            if kind in ("command_execution", "mcp_tool_call", "web_search", "collab_tool_call"):
                self.set(phase="running_tool",
                         activity={"kind": kind, "detail": self.current_item["detail"],
                                   "since": ts})
        if kind == "todo_list":
            items = item.get("items") or []
            self.set(progress={
                "completed": sum(1 for i in items if i.get("completed")),
                "total": len(items),
                "items": [{"text": trim(i.get("text"), 80), "completed": bool(i.get("completed"))}
                          for i in items[:10]],
            })
        if typ == "item.completed":
            duration = None
            if self.current_item and self.current_item.get("kind") == kind:
                duration = round(time.monotonic() - self.current_item["started"], 1)
            self.current_item = None
            if kind == "command_execution":
                counts["commands"] = counts.get("commands", 0) + 1
                counts["commands_since_diff"] = counts.get("commands_since_diff", 0) + 1
                exit_code = item.get("exit_code")
                last_command = {"command": trim(item.get("command"), 300),
                                "exit_code": exit_code,
                                "duration_s": duration}
                if exit_code == 124:
                    # Shell-level timeout: the command was cut off, but its descendants
                    # may keep running detached (observed with cargo builds).
                    last_command["timed_out"] = True
                self.set(last_command=last_command)
                self.check_repeat_reads(item.get("command") or "")
            elif kind == "file_change":
                counts["file_changes"] = counts.get("file_changes", 0) + 1
                counts["commands_since_diff"] = 0
                changed = self.state.setdefault("files_changed", {})
                for change in item.get("changes") or []:
                    path = change.get("path") or ""
                    if len(changed) < 300:
                        changed[path] = change.get("kind")
                    self.check_allowed_path(path)
            elif kind == "agent_message":
                self.set(last_agent_message=trim(item.get("text"), 400))
            elif kind == "reasoning":
                self.set(phase="reasoning", last_reasoning=trim(item.get("text"), 200))
            if kind != "reasoning" and self.state.get("phase") == "running_tool":
                self.set(phase="requesting_model", activity=None)
            self.dirty = True

    def check_allowed_path(self, path: str):
        allowed = self.cfg.get("allowed_paths") or []
        if not allowed or not path:
            return
        resolved = str(Path(path).resolve()).lower()
        for base in allowed:
            if resolved.startswith(str(Path(base).resolve()).lower()):
                return
        self.point_alert("path_violation", f"file change outside allowed paths: {path}")

    def check_repeat_reads(self, command: str):
        """Counts only exact repeats with NO file change in between: re-running the same
        test three times while landing fixes between runs is progress, not a loop, and
        auto-cancelling it was observed killing a healthy repair cycle."""
        limit = self.cfg.get("max_repeat_command", 0)
        if not limit:
            return
        diff_now = (self.state.get("counts") or {}).get("file_changes", 0)
        seen = self.state.setdefault("command_repeats", {})
        key = trim(command, 400)
        entry = seen.get(key)
        if isinstance(entry, dict) and entry.get("diff_mark") == diff_now:
            entry["count"] = entry.get("count", 0) + 1
        else:
            entry = {"count": 1, "diff_mark": diff_now}
        seen[key] = entry
        if entry["count"] >= limit:
            self.point_alert("repeat_command",
                             f"same command ran {entry['count']}x with no file change "
                             f"in between: {trim(command, 120)}")
        if len(seen) > 400:
            seen.clear()

    # -- periodic work

    def tail_rollout(self):
        if self.rollout_path is None:
            sid = self.state.get("session_id")
            if not sid:
                return
            self.rollout_path = find_rollout(sid) or False
        if not self.rollout_path:
            self.rollout_path = None if self.rollout_path is False else self.rollout_path
            return
        try:
            mtime = Path(self.rollout_path).stat().st_mtime
        except OSError:
            return
        if mtime > self.rollout_mtime:
            # The session file advancing is remote-stream activity even when the --json
            # channel is quiet (long reasoning between items) — count it against the
            # stall timer. Partial signal only: a single long model request can keep
            # both channels silent until its first item completes.
            self.rollout_mtime = mtime
            self.last_activity_mono = time.monotonic()
            self.set(remote_stream={"last_write_epoch": round(mtime, 1)})
        try:
            with open(self.rollout_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self.rollout_offset)
                chunk = f.read()
                self.rollout_offset = f.tell()
        except OSError:
            return
        tokens = self.state.setdefault("tokens", {})
        for raw in chunk.splitlines():
            if '"token_count"' not in raw and '"context_compacted"' not in raw:
                continue
            try:
                payload = json.loads(raw).get("payload") or {}
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "context_compacted":
                tokens["compactions"] = tokens.get("compactions", 0) + 1
                self.dirty = True
                continue
            info = payload.get("info") or {}
            last = info.get("last_token_usage") or {}
            total = info.get("total_token_usage") or {}
            window = info.get("model_context_window")
            tokens["requests"] = tokens.get("requests", 0) + 1
            tokens["total"] = total
            tokens["last"] = last
            if window:
                tokens["context_window"] = window
                used = last.get("input_tokens") or 0
                tokens["context_used_pct"] = round(used * 100 / window, 1)
            self.dirty = True

    def progress_path(self) -> Path:
        return Path(self.cfg["cwd"]) / (self.cfg.get("progress_file") or ".codex-progress.jsonl")

    def init_progress(self):
        """A resumed run inherits the workspace progress file. Pre-existing lines are
        from PREVIOUS runs: surface the last one as historical context, never as the
        current run's progress — a stale 'completed' milestone must not mask a rework."""
        path = self.progress_path()
        try:
            size = path.stat().st_size
        except OSError:
            return
        if not size:
            return
        self.progress_offset = size
        try:
            lines = [l for l in path.read_text(encoding="utf-8", errors="replace").splitlines()
                     if l.strip()]
        except OSError:
            return
        if lines:
            try:
                obj = json.loads(lines[-1])
            except json.JSONDecodeError:
                obj = {"note": trim(lines[-1], 200)}
            self.set(agent_report={"inherited": obj, "historical": True, "count": 0})

    def tail_progress(self):
        """Agent-side reporting: the task brief may ask codex to append JSON lines to a
        progress file in its workspace. Append-only; a report resets the stall timer so a
        long quiet command with live reports is not misread as a hang. Semantic garnish
        only — never the sole liveness signal."""
        path = self.progress_path()
        try:
            if path.stat().st_size <= self.progress_offset:
                return
            with path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(self.progress_offset)
                chunk = f.read()
                self.progress_offset = f.tell()
        except OSError:
            return
        for raw in chunk.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                obj = {"note": trim(raw, 200)}
            report = self.state.setdefault("agent_report", {})
            report.pop("inherited", None)
            report.pop("historical", None)
            report["last"] = obj
            report["origin_run_id"] = self.cfg["run_id"]
            report["count"] = report.get("count", 0) + 1
            report["at"] = utcnow_iso()
            self.last_activity_mono = time.monotonic()
            self.dirty = True

    def health_tick(self):
        silence = time.monotonic() - max(self.last_event_mono, self.last_activity_mono)
        self.set(silence_seconds=round(silence))
        max_silence = self.cfg.get("max_silence", 600)
        if max_silence:
            self.eval_condition("stalled", silence > max_silence,
                                f"no events/activity for {fmt_secs(silence)}")
        counts = self.state.get("counts") or {}
        limit = self.cfg.get("max_commands_no_diff", 0)
        if limit:
            n = counts.get("commands_since_diff", 0)
            self.eval_condition("no_diff", n > limit, f"{n} commands without a file change")
        max_items = self.cfg.get("max_items", 0)
        if max_items:
            self.eval_condition("max_items", counts.get("items", 0) > max_items,
                                f"item count exceeded {max_items}")
        max_runtime = self.cfg.get("max_runtime", 0)
        if max_runtime:
            self.eval_condition("max_runtime", time.monotonic() - self.t0 > max_runtime,
                                f"runtime exceeded {fmt_secs(max_runtime)}")
        pct = (self.state.get("tokens") or {}).get("context_used_pct")
        if pct is not None:
            threshold = self.cfg.get("context_alert_pct", 90)
            self.eval_condition("context_high", pct >= threshold,
                                f"context {pct}% (threshold {threshold}%)")

    def perf_tick(self):
        if self.job is None:
            return
        sample = self.job.sample()
        if sample:
            sample["sampled_at"] = utcnow_iso()
            self.set(perf=sample)

    def control_tick(self):
        path = self.run_dir / "control.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if mtime <= self.control_seen:
            return
        self.control_seen = mtime
        ctl = read_json(path) or {}
        if ctl.get("action") == "cancel":
            self.request_cancel(grace=int(ctl.get("grace", 20)),
                                reason=ctl.get("reason") or "user cancel")

    def maybe_terminate(self):
        """Soft cancel: prefer killing between items so no tool call is cut in half."""
        if not self.cancel_req:
            return
        at_boundary = self.current_item is None
        expired = time.monotonic() > self.cancel_req["deadline"]
        if at_boundary or expired:
            self.set(phase="cancelled", cancel_boundary="item" if at_boundary else "forced")
            self.flush(force=True)
            self.terminate_tree()

    def terminate_tree(self):
        if self.job is not None:
            self.job.terminate()
        elif self.proc is not None:
            try:
                if IS_WIN:
                    self.proc.kill()
                else:
                    os.killpg(os.getpgid(self.proc.pid), 9)
            except OSError:
                pass

    # -- main

    def run(self) -> int:
        cfg = self.cfg
        run_id = cfg["run_id"]
        prompt = (self.run_dir / "prompt.txt").read_text(encoding="utf-8")
        last_message = self.run_dir / "last-message.md"

        if cfg.get("fork_from"):
            cfg["resume_id"], fork_method = fork_any(cfg["fork_from"])
            self.set(forked_from=cfg["fork_from"], session_id=cfg["resume_id"],
                     fork_method=fork_method)
        if cfg.get("resume_id"):
            self.set(session_id=cfg["resume_id"])

        cfg["has_git"] = (Path(cfg["cwd"]) / ".git").exists()
        baseline = None
        if cfg["has_git"]:
            baseline = self.git("rev-parse", "--short", "HEAD")
        cmd = build_exec_cmd(cfg, last_message)

        self.t0 = time.monotonic()
        self.set(run_id=run_id, phase="starting", cwd=cfg["cwd"], baseline_commit=baseline,
                 started_at=utcnow_iso(), supervisor_pid=os.getpid(), cmd=cmd,
                 engine="exec", health={"status": "ok", "current": [], "history": []},
                 cli_entrypoint=f'python "{SELF}"')
        if cfg.get("allowed_paths"):
            # Normalized at dispatch; shown here so a misconfigured guard is visible
            # instead of silently firing.
            self.set(allowed_paths=cfg["allowed_paths"])
        self.set(progress_path=str(self.progress_path()))
        self.init_progress()
        self.flush(force=True)

        if IS_WIN:
            self.job = JobObject(run_id)
            creationflags = 0x4 | 0x200 | 0x08000000  # SUSPENDED | NEW_GROUP | NO_WINDOW
        else:
            creationflags = 0

        stderr_log = (self.run_dir / "codex-stderr.log").open("a", encoding="utf-8")
        popen_kw = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_log,
                        text=True, encoding="utf-8", errors="replace", cwd=cfg["cwd"], bufsize=1)
        if IS_WIN:
            popen_kw["creationflags"] = creationflags
        else:
            popen_kw["start_new_session"] = True
        self.proc = subprocess.Popen(cmd, **popen_kw)
        self.set(codex_pid=self.proc.pid)

        if IS_WIN:
            # Assign while suspended so no child can escape the job, then let it run.
            self.job.assign(int(self.proc._handle))
            resume_process_threads(self.proc.pid)

        def feed_stdin():
            try:
                self.proc.stdin.write(prompt)
                self.proc.stdin.close()
            except OSError:
                pass

        events = queue.Queue()

        def read_stdout():
            for line in self.proc.stdout:
                events.put((utcnow_iso(), line.rstrip("\n")))
            events.put(None)

        threading.Thread(target=feed_stdin, daemon=True).start()
        threading.Thread(target=read_stdout, daemon=True).start()

        self.set(phase="requesting_model")
        stream_done = False
        last_ctl_tick = 0.0
        last_slow_tick = 0.0
        # Nothing inside this loop may raise: an unexpected supervisor death closes the
        # Job Object handle and KILL_ON_JOB_CLOSE kills the working codex tree with it.
        while not stream_done:
            try:
                entry = events.get(timeout=0.5)
                if entry is None:
                    stream_done = True
                else:
                    ts, line = entry
                    stripped = line.strip()
                    if stripped.startswith("{"):
                        try:
                            obj = json.loads(stripped)
                            append_jsonl(self.events_path, {"ts": ts, "event": obj})
                            self.on_event(ts, obj)
                        except json.JSONDecodeError:
                            append_jsonl(self.events_path, {"ts": ts, "raw": line})
                    elif stripped:
                        append_jsonl(self.events_path, {"ts": ts, "raw": line})
            except queue.Empty:
                pass
            except Exception as exc:
                print(f"event handling error, skipped: {exc!r}", file=sys.stderr, flush=True)

            try:
                now = time.monotonic()
                if now - last_ctl_tick >= 1:
                    last_ctl_tick = now
                    self.control_tick()
                if now - last_slow_tick >= 5:
                    last_slow_tick = now
                    self.tail_rollout()
                    self.tail_progress()
                    self.health_tick()
                    self.perf_tick()
                self.maybe_terminate()
                self.flush()
            except Exception as exc:
                print(f"tick error, skipped: {exc!r}", file=sys.stderr, flush=True)

        exit_code = self.proc.wait()
        stderr_log.close()
        try:
            self.finalize(exit_code, last_message)
        except Exception as exc:
            print(f"finalize error: {exc!r}", file=sys.stderr, flush=True)
            try:
                atomic_write_json(self.run_dir / "result.json", {
                    "run_id": self.cfg.get("run_id"), "phase": "failed",
                    "reason": f"finalize error: {exc}", "exit_code": exit_code,
                    "finished_at": utcnow_iso(),
                    "session_id": self.state.get("session_id"),
                })
            except OSError:
                pass
        return 0

    def finalize(self, exit_code: int, last_message: Path):
        self.tail_rollout()
        self.snapshot_git()
        # The run is over: whatever conditions were active are no longer "current".
        for rule in list(self.health_current):
            self.eval_condition(rule, False, "")
        if self.state.get("phase") == "cancelled":
            phase, reason = "cancelled", self.state.get("cancel_requested") or "cancelled"
        elif self.turn_failed_error:
            phase, reason = "failed", self.turn_failed_error
        elif exit_code == 0 and self.saw_turn_complete:
            phase, reason = "completed", "turn completed"
        elif exit_code == 0:
            phase, reason = "completed", "process exited cleanly"
        else:
            phase, reason = "failed", f"codex exited with code {exit_code}"
        sid = self.state.get("session_id")
        result = {
            "run_id": self.cfg["run_id"],
            "phase": phase,
            "reason": reason,
            "exit_code": exit_code,
            "finished_at": utcnow_iso(),
            "session_id": sid,
            "last_event_at": self.state.get("last_event_at"),
            "dirty_files": self.state.get("dirty_files"),
            "diff_stat": self.state.get("diff_stat"),
            "final_message_file": str(last_message) if last_message.exists() else None,
            "resume_cmd": (f'python "{SELF}" resume {sid} -C "{self.cfg["cwd"]}" '
                           f'--prompt-file <path>') if sid else None,
        }
        atomic_write_json(self.run_dir / "result.json", result)
        self.set(phase=phase, finished_at=result["finished_at"], exit_code=exit_code)
        self.flush(force=True)


# ---------------------------------------------------------------------------- client commands


def shared_dispatch_flags(p: argparse.ArgumentParser):
    p.add_argument("-C", "--project-dir", default=None,
                   help="Directory codex runs in. Resumes inherit the session's previous "
                        "directory when omitted; new dispatches use the current directory.")
    p.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"])
    p.add_argument("--model")
    p.add_argument("--reasoning-effort",
                   choices=["low", "medium", "high", "xhigh", "max", "ultra"])
    p.add_argument("--profile")
    p.add_argument("--add-dir", action="append", default=[], metavar="DIR")
    p.add_argument("--no-skip-git-repo-check", action="store_true")
    p.add_argument("--max-silence", type=int, default=600,
                   help="Alert after N seconds without codex events (0 = off, default 600).")
    p.add_argument("--max-commands-no-diff", type=int, default=0,
                   help="Alert after N commands with no file change (0 = off).")
    p.add_argument("--max-items", type=int, default=0, help="Alert past N items (0 = off).")
    p.add_argument("--max-runtime", type=int, default=0, help="Alert past N seconds (0 = off).")
    p.add_argument("--max-repeat-command", type=int, default=0,
                   help="Alert when the same command runs N times (0 = off).")
    p.add_argument("--allowed-path", action="append", default=[], metavar="DIR",
                   help="Alert on file changes outside these roots (repeatable).")
    p.add_argument("--context-alert-pct", type=int, default=90)
    p.add_argument("--on-alert", choices=["warn", "cancel"], default="warn",
                   help="cancel = auto soft-cancel when any health rule fires.")
    p.add_argument("--progress-file", default=".codex-progress.jsonl", metavar="NAME",
                   help="Workspace-relative file the agent may append progress reports to.")
    p.add_argument("--prompt-file", metavar="PATH",
                   help="Read the prompt from a file (wins over the positional prompt).")
    p.add_argument("--wait", action="store_true", help="Block until the run finishes.")


def session_cwd(session_id: str):
    """The workspace the session last ran in, via the session index."""
    if not session_id:
        return None
    index = read_json(sessions_index_path()) or {}
    current = (index.get(session_id) or {}).get("current_run_id")
    if not current:
        return None
    run_cfg = read_json(runs_root() / current / "run.json") or {}
    return run_cfg.get("cwd")


def cfg_from_args(args, resume_id=None, fork_from=None) -> dict:
    # A resume that omits -C must land in the session's own worktree, not wherever the
    # orchestrator happens to be standing — with a wide sandbox that difference is how
    # half-finished work ends up in the main repo.
    inherited = session_cwd(resume_id or fork_from)
    if args.project_dir and inherited and \
            str(Path(args.project_dir).resolve()) != str(Path(inherited).resolve()):
        print(f"warning: -C differs from the session's previous cwd ({inherited}); "
              f"proceeding with the explicit -C", file=sys.stderr)
    project_dir = Path(args.project_dir or inherited or os.getcwd()).resolve()
    if not project_dir.exists():
        raise SystemExit(f"project directory does not exist: {project_dir}")
    if project_dir == Path.home():
        raise SystemExit(
            "refusing to run with the home directory as -C: the managed sandbox cannot spawn "
            "child processes there. Point -C at a project and use --add-dir for extras.")
    return {
        "run_id": new_run_id(),
        "cwd": str(project_dir),
        "sandbox": args.sandbox,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "profile": args.profile,
        "add_dirs": args.add_dir,
        "no_skip_git_repo_check": args.no_skip_git_repo_check,
        "resume_id": resume_id,
        "fork_from": fork_from,
        "max_silence": args.max_silence,
        "max_commands_no_diff": args.max_commands_no_diff,
        "max_items": args.max_items,
        "max_runtime": args.max_runtime,
        "max_repeat_command": args.max_repeat_command,
        # Anchor relative allowed roots to the project dir at dispatch time. The
        # supervisor runs with the run directory as cwd, so resolving there made every
        # relative root a guaranteed (false) path_violation.
        "allowed_paths": [
            str((Path(p) if Path(p).is_absolute() else project_dir / p).resolve())
            for p in args.allowed_path],
        "context_alert_pct": args.context_alert_pct,
        "on_alert": args.on_alert,
        "progress_file": args.progress_file,
        "created_at": utcnow_iso(),
    }


def pick_run(args, default=None) -> str:
    """Positional run and --run-id are interchangeable on every run-taking command."""
    token = getattr(args, "run_id_opt", None) or getattr(args, "run", None) or default
    if not token:
        raise SystemExit("give a run id (positional or --run-id)")
    return token


def resolve_prompt(args) -> str:
    if getattr(args, "prompt_file", None):
        return Path(args.prompt_file).read_text(encoding="utf-8")
    if args.prompt is not None:
        return args.prompt
    return sys.stdin.read()


def sessions_index_path() -> Path:
    return orch_home() / "sessions.json"


def update_session_index(session_id: str, run_id: str):
    """Keeps session -> current_run_id, and stamps the superseded run so stale monitors
    can see they are watching an old state file."""
    if not session_id:
        return
    index = read_json(sessions_index_path()) or {}
    prev = (index.get(session_id) or {}).get("current_run_id")
    if prev and prev != run_id:
        prev_dir = runs_root() / prev
        prev_state = read_json(prev_dir / "state.json")
        if prev_state and prev_state.get("phase") in TERMINAL_PHASES \
                and prev_state.get("superseded_by") != run_id:
            prev_state["superseded_by"] = run_id
            try:
                atomic_write_json(prev_dir / "state.json", prev_state)
            except OSError:
                pass
    index[session_id] = {"current_run_id": run_id, "updated_at": utcnow_iso()}
    try:
        atomic_write_json(sessions_index_path(), index)
    except OSError:
        pass


def spawn_supervisor(run_dir: Path):
    log = (run_dir / "supervisor.log").open("a", encoding="utf-8")
    argv = [sys.executable, str(SELF), "supervise", "--run-dir", str(run_dir)]
    kw = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=str(run_dir))
    if IS_WIN:
        base = 0x8 | 0x200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            # Break out of any job the calling shell lives in, so closing that shell
            # cannot take the supervisor (and codex) down with it.
            proc = subprocess.Popen(argv, creationflags=base | 0x01000000, **kw)
        except OSError:
            proc = subprocess.Popen(argv, creationflags=base, **kw)
    else:
        proc = subprocess.Popen(argv, start_new_session=True, **kw)
    log.close()
    return proc


def launch(cfg: dict, prompt: str, wait: bool, quiet: bool = False) -> dict:
    run_dir = runs_root() / cfg["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    atomic_write_json(run_dir / "run.json", cfg)
    spawn_supervisor(run_dir)

    state_path = run_dir / "state.json"
    deadline = time.time() + 120
    state = {}
    while time.time() < deadline:
        state = read_json(state_path) or {}
        if state.get("session_id") or state.get("phase") in TERMINAL_PHASES:
            break
        time.sleep(0.3)

    update_session_index(state.get("session_id") or cfg.get("resume_id"), cfg["run_id"])

    if not quiet:
        print(f"run_id: {cfg['run_id']}")
        print(f"session_id: {state.get('session_id') or ''}")
        print(f"state: {state_path}")
        print(f'watch: python "{SELF}" watch {cfg["run_id"]}')
    if wait:
        # `phase` turns terminal on the turn.completed event, slightly before the supervisor
        # writes result.json — wait for the result, with a grace window in case the
        # supervisor died mid-finalize.
        terminal_seen = None
        result = None
        while True:
            state = read_json(state_path) or {}
            result = read_json(run_dir / "result.json")
            if result:
                break
            if state.get("phase") in TERMINAL_PHASES:
                terminal_seen = terminal_seen or time.time()
                if time.time() - terminal_seen > 15:
                    break
            time.sleep(1)
        final = run_dir / "last-message.md"
        if not quiet:
            print()
        if final.exists():
            print(final.read_text(encoding="utf-8", errors="replace").rstrip())
        # Reuse the read that broke the loop: re-reading can hit the atomic-replace
        # window and momentarily see nothing.
        result = result or read_json(run_dir / "result.json") or {}
        if result.get("phase") != "completed":
            print(f"[{result.get('phase')}] {result.get('reason')}", file=sys.stderr)
    return state


def cmd_dispatch(args):
    prompt = resolve_prompt(args)
    if not prompt.strip():
        raise SystemExit("prompt is empty")
    if args.session_id and args.fork_from:
        raise SystemExit("use only one of --session-id / --fork-from")
    cfg = cfg_from_args(args, resume_id=args.session_id, fork_from=args.fork_from)
    launch(cfg, prompt, wait=args.wait)


def cmd_resume(args):
    prompt = resolve_prompt(args)
    if not prompt.strip():
        raise SystemExit("prompt is empty")
    cfg = cfg_from_args(args, resume_id=args.session_id)
    launch(cfg, prompt, wait=args.wait)


def render_status(run_dir: Path, as_json=False):
    state = read_json(run_dir / "state.json") or {}
    result = read_json(run_dir / "result.json")
    if as_json:
        print(json.dumps({"state": state, "result": result}, ensure_ascii=False, indent=1))
        return
    phase = state.get("phase", "?")
    silence = state.get("silence_seconds")
    live = phase not in TERMINAL_PHASES
    stalled = f"  (last event {fmt_secs(silence)} ago)" if live and silence and silence > 60 else ""
    print(f"run:      {state.get('run_id')}   phase: {phase}{stalled}")
    print(f"session:  {state.get('session_id') or '—'}")
    print(f"cwd:      {state.get('cwd')}   baseline: {state.get('baseline_commit') or '—'}")
    counts = state.get("counts") or {}
    print(f"work:     turns {counts.get('turns', 0)}  items {counts.get('items', 0)}"
          f"  commands {counts.get('commands', 0)}  patches {counts.get('file_changes', 0)}")
    activity = state.get("activity")
    if live and activity:
        print(f"activity: {activity.get('kind')}  {trim(activity.get('detail'), 90)}")
    if live and state.get("cancel_requested") and phase not in TERMINAL_PHASES:
        print(f"pending:  cancel/steer requested ({state['cancel_requested']}) — "
              f"waiting for an item boundary")
    lc = state.get("last_command") or {}
    if live and lc.get("timed_out"):
        print(f"warning:  last command hit its timeout (exit 124): {trim(lc.get('command'), 70)}")
        print("          its descendants may still be running detached — check perf pids/CPU")
    progress = state.get("progress")
    if progress:
        print(f"plan:     {progress['completed']}/{progress['total']} done")
        for item in progress.get("items", [])[:6]:
            mark = "x" if item["completed"] else " "
            print(f"            [{mark}] {item['text']}")
    report = state.get("agent_report")
    if report and report.get("last"):
        last = report["last"]
        parts = [f"{k}={trim(str(v), 60)}" for k, v in list(last.items())[:4]]
        print(f"report:   {'  '.join(parts)}  ({report.get('count', 0)} reports)")
    elif report and report.get("inherited"):
        last = report["inherited"]
        parts = [f"{k}={trim(str(v), 50)}" for k, v in list(last.items())[:3]]
        print(f"report:   [historical, previous run] {'  '.join(parts)}")
    tokens = state.get("tokens") or {}
    total = tokens.get("total") or {}
    if total:
        line = (f"tokens:   in {total.get('input_tokens', 0):,}"
                f" (cached {total.get('cached_input_tokens', 0):,})"
                f"  out {total.get('output_tokens', 0):,}"
                f"  requests {tokens.get('requests', 0)}")
        if tokens.get("context_window"):
            line += f"  context {tokens.get('context_used_pct')}% of {tokens['context_window']:,}"
        if tokens.get("compactions"):
            line += f"  compactions {tokens['compactions']}"
        print(line)
    files = state.get("files_changed") or {}
    if files:
        print(f"files:    {len(files)} changed  {state.get('diff_stat') or ''}")
    rs = state.get("remote_stream") or {}
    if live and rs.get("last_write_epoch"):
        print(f"stream:   session file written {fmt_secs(max(0, time.time() - rs['last_write_epoch']))} ago")
    health = state.get("health") or {}
    current = health.get("current")
    if current:
        print(f"health:   warning ({len(current)} active)")
        for alert in current[:4]:
            print(f"            ! {alert['rule']}: {alert['message']}"
                  f"  (since {(alert.get('since') or '')[11:19]})")
    else:
        history = health.get("history") or health.get("alerts") or []
        print("health:   ok" + (f"  ({len(history)} past alerts, resolved)" if history else ""))
    if state.get("superseded_by"):
        print(f"note:     superseded by {state['superseded_by']} (this is an old run of the session)")
    perf = state.get("perf") or {}
    if perf:
        print(f"perf:     cpu {perf.get('cpu_seconds', 0)}s"
              f"  io r {fmt_bytes(perf.get('read_bytes'))} w {fmt_bytes(perf.get('write_bytes'))}"
              f"  mem peak {fmt_bytes(perf.get('peak_memory_bytes'))}"
              f"  procs {perf.get('active_processes', 0)}")
    if state.get("last_agent_message"):
        print(f"message:  {trim(state['last_agent_message'], 160)}")
    if result:
        print(f"result:   [{result.get('phase')}] {result.get('reason')}"
              f"  exit={result.get('exit_code')}")
    if state.get("last_error") and phase != "completed":
        print(f"error:    {trim(state.get('last_error'), 160)}")
    if state.get("unknown_events"):
        print(f"note:     unknown event types seen (CLI schema drift?): "
              f"{', '.join(state['unknown_events'])}")


def git_snapshot(cwd: str, baseline: str):
    """On-demand only — the supervisor never polls git; this runs once when asked."""
    def g(*argv):
        try:
            out = subprocess.run(["git", "-C", cwd, *argv], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=15,
                                 creationflags=NO_WINDOW)
            return out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    ref = baseline or "HEAD"
    print("--- git (on demand) ---")
    stat = g("diff", "--shortstat", ref)
    print(f"vs {ref}: {stat or 'no diff (or not a git repo)'}")
    names = g("diff", "--name-status", ref)
    if names:
        lines = names.splitlines()
        for line in lines[:12]:
            print(f"  {line}")
        if len(lines) > 12:
            print(f"  ... {len(lines) - 12} more files")
    porcelain = g("status", "--porcelain")
    if porcelain is not None:
        print(f"uncommitted entries: {len(porcelain.splitlines()) if porcelain else 0}")


def cmd_status(args):
    run_dir = resolve_run_dir(pick_run(args, default="latest"))
    render_status(run_dir, as_json=args.json)
    if args.diff and not args.json:
        state = read_json(run_dir / "state.json") or {}
        if state.get("cwd"):
            git_snapshot(state["cwd"], state.get("baseline_commit"))


def cmd_watch(args):
    run_dir = resolve_run_dir(pick_run(args, default="latest"))
    while True:
        os.system("cls" if IS_WIN else "clear")
        render_status(run_dir)
        state = read_json(run_dir / "state.json") or {}
        if state.get("phase") in TERMINAL_PHASES:
            final = run_dir / "last-message.md"
            if final.exists():
                print("\n--- final message ---")
                print(final.read_text(encoding="utf-8", errors="replace").rstrip())
            return
        time.sleep(args.interval)


def verify_run(run_dir: Path, state: dict) -> tuple:
    """Ground truth for one run: is anything actually alive? Heals orphaned records.

    A non-terminal phase only means the supervisor was alive at the last state write.
    If the supervisor is gone, KILL_ON_JOB_CLOSE took the codex tree with it — the
    record is a corpse claiming to be running, so mark it failed and write the
    missing terminal record."""
    phase = state.get("phase", "?")
    if phase in TERMINAL_PHASES:
        return phase, ""
    sup = state.get("supervisor_pid")
    sup_alive = bool(sup and pid_alive(sup))
    codex_alive = bool(state.get("codex_pid") and pid_alive(state["codex_pid"]))
    if sup_alive:
        procs = ""
        if IS_WIN:
            job = JobObject.open_existing(run_dir.name)
            if job:
                procs = str((job.sample() or {}).get("active_processes", "?"))
        return phase, f"LIVE {procs}p" if procs else "LIVE"
    if codex_alive:
        # Should not happen on Windows (job close kills the tree); flag it loudly.
        return phase, f"ORPHAN pid={state.get('codex_pid')}"
    reason = "supervisor died mid-run; tree killed by job close (record healed by list)"
    state.update({"phase": "failed", "last_error": reason})
    try:
        atomic_write_json(run_dir / "state.json", state)
        if not read_json(run_dir / "result.json"):
            sid = state.get("session_id")
            atomic_write_json(run_dir / "result.json", {
                "run_id": run_dir.name, "phase": "failed", "reason": reason,
                "exit_code": None, "finished_at": utcnow_iso(), "session_id": sid,
                "resume_cmd": f'python "{SELF}" resume {sid} "<prompt>"' if sid else None,
            })
    except OSError:
        pass
    return "failed", "healed"


def cmd_list(args):
    root = runs_root()
    if not root.exists():
        print("no runs recorded yet")
        return
    project = None
    if args.project_dir:
        project = str(Path(args.project_dir).resolve())
    rows = sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    shown = 0
    print(f"{'run_id':26s} {'phase':11s} {'alive':12s} {'age':>7s} {'session':10s} "
          f"{'cwd':28s} prompt")
    print("-" * 120)
    for run_dir in rows:
        if shown >= args.limit:
            break
        state = read_json(run_dir / "state.json") or {}
        if project:
            try:
                if str(Path(state.get("cwd") or "").resolve()) != project:
                    continue
            except OSError:
                continue
        phase, alive = verify_run(run_dir, state)
        age = fmt_secs(time.time() - run_dir.stat().st_mtime)
        prompt = ""
        try:
            prompt = (run_dir / "prompt.txt").read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        sid = (state.get("session_id") or "")[:8]
        print(f"{run_dir.name:26s} {phase:11s} {alive:12s} {age:>7s} {sid:10s} "
              f"{trim(state.get('cwd') or '', 28):28s} {trim(prompt, 36)}")
        shown += 1
    if not shown:
        print(f"no runs match {project or ''}")


def request_supervisor_cancel(run_dir: Path, grace: int, reason: str) -> bool:
    """Returns True if a live supervisor picked up the request path."""
    state = read_json(run_dir / "state.json") or {}
    sup = state.get("supervisor_pid")
    if sup and pid_alive(sup):
        atomic_write_json(run_dir / "control.json",
                          {"action": "cancel", "grace": grace, "reason": reason,
                           "ts": utcnow_iso()})
        return True
    return False


def hard_kill(run_dir: Path) -> None:
    state = read_json(run_dir / "state.json") or {}
    run_id = run_dir.name
    if IS_WIN:
        job = JobObject.open_existing(run_id)
        if job:
            job.terminate()
            return
    pid = state.get("codex_pid")
    if pid and pid_alive(pid):
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, creationflags=NO_WINDOW)
        else:
            try:
                os.killpg(os.getpgid(pid), 9)
            except OSError:
                pass


def wait_terminal(run_dir: Path, timeout: float) -> dict:
    deadline = time.time() + timeout
    state = {}
    while time.time() < deadline:
        state = read_json(run_dir / "state.json") or {}
        if state.get("phase") in TERMINAL_PHASES:
            return state
        time.sleep(0.5)
    return state


def cmd_cancel(args):
    run_dir = resolve_run_dir(pick_run(args))
    state = read_json(run_dir / "state.json") or {}
    if state.get("phase") in TERMINAL_PHASES:
        print(f"already {state['phase']}")
        return
    # A live supervisor gets the request even for --hard (grace 0), so the terminal record
    # says "cancelled" instead of a bogus "failed". Direct kill is only for a dead supervisor.
    grace = 0 if args.hard else args.grace
    if not request_supervisor_cancel(run_dir, grace, "user cancel"):
        hard_kill(run_dir)
        state = read_json(run_dir / "state.json") or {}
        state.update({"phase": "cancelled", "cancel_requested": "hard kill"})
        atomic_write_json(run_dir / "state.json", state)
        print("cancelled (hard, supervisor was gone)")
        return
    state = wait_terminal(run_dir, grace + 30)
    if state.get("phase") in TERMINAL_PHASES:
        print(f"cancelled at {state.get('cancel_boundary') or '?'} boundary")
    else:
        print("supervisor did not confirm in time; forcing")
        hard_kill(run_dir)


def stop_run(run_dir: Path, grace: int, reason: str) -> dict:
    """Stops a live run at an item boundary (grace, then force) and returns its final state."""
    state = read_json(run_dir / "state.json") or {}
    if state.get("phase") in TERMINAL_PHASES:
        return state
    if request_supervisor_cancel(run_dir, grace, reason):
        state = wait_terminal(run_dir, grace + 30)
        if state.get("phase") not in TERMINAL_PHASES:
            hard_kill(run_dir)
            state = wait_terminal(run_dir, 10)
    else:
        # Dead supervisor: record the terminal state ourselves so the old run never
        # reads as still running.
        hard_kill(run_dir)
        state = read_json(run_dir / "state.json") or {}
        state.update({"phase": "cancelled",
                      "cancel_requested": f"{reason} (supervisor was gone)"})
        atomic_write_json(run_dir / "state.json", state)
    return state


def cmd_steer(args):
    """Interrupt-and-resume, by design: `codex exec` has no input channel to a running
    process, so steering means stopping at an item boundary and resuming the SAME
    session with the correction. Context up to the stop is fully preserved."""
    # `steer --run-id X "msg"` puts the message in the run positional; shuffle it back.
    if getattr(args, "run_id_opt", None) and args.prompt is None and args.run:
        args.prompt, args.run = args.run, None
    run_dir = resolve_run_dir(pick_run(args))
    state = read_json(run_dir / "state.json") or {}
    old_cfg = read_json(run_dir / "run.json") or {}
    session_id = state.get("session_id")
    if not session_id:
        raise SystemExit("run has no session_id yet; try again in a few seconds or cancel it")
    prompt = resolve_prompt(args)
    if not prompt.strip():
        raise SystemExit("prompt is empty")

    # boundary semantics: "completed" = the old run had already finished (lossless),
    # "item" = stopped between items (cheap), "forced" = grace expired mid-item and
    # in-flight remote reasoning was discarded (lossy — treat as a real interruption).
    entry_phase = state.get("phase")
    if entry_phase in TERMINAL_PHASES:
        boundary = "completed"
    else:
        state = stop_run(run_dir, args.grace, "steer")
        boundary = state.get("cancel_boundary") or "unknown"
        print(f"stopped {run_dir.name} (boundary={boundary})")

    cfg = dict(old_cfg)
    cfg.update({"run_id": new_run_id(), "resume_id": session_id, "fork_from": None,
                "created_at": utcnow_iso(), "steered_from": run_dir.name})
    launch(cfg, prompt, wait=args.wait)
    print(json.dumps({"steer": {
        "old_run": run_dir.name, "new_run": cfg["run_id"], "session_id": session_id,
        "boundary": boundary,
        "mode": "interrupt-and-resume",
    }}, ensure_ascii=False))


def cmd_events(args):
    run_dir = resolve_run_dir(pick_run(args, default="latest"))
    lines = []
    try:
        with (run_dir / "events.jsonl").open(encoding="utf-8") as f:
            lines = f.readlines()[-args.tail:]
    except OSError:
        raise SystemExit("no events recorded")
    for raw in lines:
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ts = (entry.get("ts") or "")[11:19]
        event = entry.get("event")
        if event is None:
            print(f"{ts}  raw: {trim(entry.get('raw'), 120)}")
            continue
        typ = event.get("type")
        item = event.get("item") or {}
        detail = item.get("type") or ""
        if item.get("command"):
            detail += f"  {trim(item['command'], 90)}"
        elif item.get("text"):
            detail += f"  {trim(item['text'], 90)}"
        elif event.get("usage"):
            u = event["usage"]
            detail = f"in {u.get('input_tokens', 0):,} out {u.get('output_tokens', 0):,}"
        elif event.get("message"):
            detail = trim(event["message"], 90)
        print(f"{ts}  {typ:16s} {detail}")


# ---------------------------------------------------------------------------- app-server compact


class AppServerClient:
    """Minimal newline-delimited JSON-RPC client over a temporary `codex app-server`."""

    def __init__(self):
        self.proc = subprocess.Popen(
            codex_argv(["app-server"]),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=NO_WINDOW)
        self.lock = threading.Lock()
        self.responses = {}
        self.notifications = []
        threading.Thread(target=self._reader, daemon=True).start()
        self.next_id = 0

    def _reader(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self.lock:
                if "id" in msg and ("result" in msg or "error" in msg):
                    self.responses[msg["id"]] = msg
                else:
                    self.notifications.append(msg)

    def notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def call(self, method, params=None, timeout=60):
        self.next_id += 1
        rid = self.next_id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if rid in self.responses:
                    resp = self.responses.pop(rid)
                    if "error" in resp:
                        raise RuntimeError(f"{method}: {resp['error']}")
                    return resp.get("result")
            if self.proc.poll() is not None:
                raise RuntimeError("app-server exited unexpectedly")
            time.sleep(0.2)
        raise TimeoutError(f"{method} timed out after {timeout}s")

    def take_notifications(self):
        with self.lock:
            out, self.notifications = self.notifications, []
        return out

    def close(self):
        try:
            self.proc.terminate()
        except OSError:
            pass


def appserver_session(client: "AppServerClient"):
    client.call("initialize", {
        "clientInfo": {"name": "codexctl", "title": "codexctl", "version": "2.0"},
        "capabilities": {"experimentalApi": True},
    }, timeout=30)
    client.notify("initialized")


def native_fork(session_id: str, last_turn_id=None) -> str:
    """Fork through the CLI's own machinery (app-server thread/fork): the fork is indexed
    properly and can be cut at a turn boundary, neither of which the manual copy can do."""
    client = AppServerClient()
    try:
        appserver_session(client)
        params = {"threadId": session_id}
        if last_turn_id:
            params["lastTurnId"] = last_turn_id
        result = client.call("thread/fork", params, timeout=60)
        new_id = ((result or {}).get("thread") or {}).get("id")
        if not new_id:
            raise RuntimeError(f"thread/fork returned no thread id: {result}")
        return new_id
    finally:
        client.close()


def fork_any(session_id: str) -> tuple:
    """Native fork first; the manual JSONL copy is the fallback when the experimental
    app-server surface is unavailable. Returns (new_id, method)."""
    try:
        return native_fork(session_id), "app-server"
    except (RuntimeError, TimeoutError, OSError) as exc:
        print(f"native fork unavailable ({exc}); falling back to manual copy", file=sys.stderr)
        return fork_session(session_id), "manual-copy"


def cmd_fork(args):
    new_id, method = fork_any(args.session_id)
    print(f"forked_id: {new_id}")
    print(f"method: {method}")
    print(f'resume: python "{SELF}" resume {new_id} "<prompt>"')
    print(f"cleanup when done: codex delete --force {new_id}")


def rollout_compaction_count(session_id: str) -> int:
    path = find_rollout(session_id)
    if not path:
        return 0
    count = 0
    for raw in path.open(encoding="utf-8", errors="replace"):
        if '"context_compacted"' in raw:
            count += 1
    return count


def run_compaction(session_id: str, timeout: int) -> bool:
    """Compacts a session via app-server; True only when the compaction is confirmed
    (contextCompaction item completed, or the rollout gained a context_compacted event)."""
    before = rollout_compaction_count(session_id)
    client = AppServerClient()
    try:
        appserver_session(client)
        client.call("thread/resume", {"threadId": session_id}, timeout=60)
        client.call("thread/compact/start", {"threadId": session_id}, timeout=60)
        deadline = time.time() + timeout
        while time.time() < deadline:
            for note in client.take_notifications():
                params = note.get("params") or {}
                item = params.get("item") or {}
                if note.get("method") == "item/completed" \
                        and item.get("type") == "contextCompaction":
                    return True
                if note.get("method") == "error":
                    err = (params.get("error") or {}).get("message")
                    print(f"  transient: {err}", flush=True)
            if rollout_compaction_count(session_id) > before:
                return True
            time.sleep(3)
        return False
    finally:
        client.close()


def cmd_compact(args):
    print("compaction started; waiting...")
    if run_compaction(args.session_id, args.timeout):
        print(f"compacted OK  (rollout context_compacted events: "
              f"{rollout_compaction_count(args.session_id)})")
    else:
        raise SystemExit("timed out waiting for compaction; check the session manually")


def make_checkpoint_prompt(cwd: str, progress_file: str) -> str:
    """Absolute paths only: agents resolve relative paths against surprising anchors
    when several worktrees share a main repo (observed: checkpoint written to the main
    workspace instead of the run's worktree)."""
    checkpoint = Path(cwd) / "CHECKPOINT.md"
    progress = Path(cwd) / progress_file
    return (
        f"把当前进度写成 checkpoint，只做这一件事：写入或更新 {checkpoint}（必须用这个绝对路径），"
        "内容包括（1）已完成的事项，（2）改到一半的文件与所处状态，（3）恢复工作的第一条命令，"
        f"（4）剩余待办清单，（5）不许越出的范围。同时向 {progress}（绝对路径）追加一行 "
        '{"phase":"checkpoint"}。不要做任何其他修改，写完立即回复 checkpoint-done。'
    )


def cmd_ccr(args):
    """Checkpoint -> compact -> resume as one transaction. Aborts before the resume step
    if compaction cannot be confirmed, leaving the session unchanged."""
    run_dir = resolve_run_dir(pick_run(args))
    state = read_json(run_dir / "state.json") or {}
    session_id = state.get("session_id")
    if not session_id:
        raise SystemExit("run has no session_id")
    continuation = Path(args.prompt_file).read_text(encoding="utf-8")
    old_cfg = read_json(run_dir / "run.json") or {}

    # Lightweight transaction record: the client may outlive its caller (an observed
    # 51-minute ccr finished after the calling shell timed out), so every phase is
    # written to ccr.json in the old run's directory for later inspection.
    txn_path = run_dir / "ccr.json"
    txn = {"transaction": "checkpoint-compact-resume", "old_run": run_dir.name,
           "session_id": session_id, "client_pid": os.getpid(), "phases": []}

    def txn_phase(name, **extra):
        txn["phases"].append({"phase": name, "ts": utcnow_iso(), **extra})
        try:
            atomic_write_json(txn_path, txn)
        except OSError:
            pass

    if state.get("phase") not in TERMINAL_PHASES:
        print("1/4 stopping at item boundary...")
        txn_phase("stopping_source")
        stop_run(run_dir, args.grace, "checkpoint-compact-resume")
        txn_phase("source_stopped")
    else:
        print("1/4 run already terminal")
        txn_phase("source_already_terminal")

    if args.checkpoint_prompt:
        # A custom instruction chooses its own target — printing our default path here
        # was observed misleading the operator into thinking the file was missing.
        print("2/4 writing checkpoint (custom instruction; target defined by your prompt)...")
        checkpoint_target = None
    else:
        checkpoint_target = str(Path(old_cfg.get("cwd", "")) / "CHECKPOINT.md")
        print(f"2/4 writing checkpoint (short resumed turn) -> {checkpoint_target}")
    cfg = dict(old_cfg)
    cfg.update({"run_id": new_run_id(), "resume_id": session_id, "fork_from": None,
                "created_at": utcnow_iso()})
    txn_phase("writing_checkpoint", run_id=cfg["run_id"], target=checkpoint_target,
              custom_prompt=bool(args.checkpoint_prompt))
    launch(cfg, args.checkpoint_prompt or make_checkpoint_prompt(
        old_cfg.get("cwd", ""), old_cfg.get("progress_file") or ".codex-progress.jsonl"),
        wait=True, quiet=True)
    txn_phase("checkpoint_done", run_id=cfg["run_id"])

    print("3/4 compacting...")
    txn_phase("compacting")
    if not run_compaction(session_id, args.timeout):
        txn_phase("compact_failed")
        raise SystemExit(
            "compaction not confirmed; ABORTING before resume — session and checkpoint "
            "are intact, retry with a longer --timeout (transaction log: ccr.json)")
    txn_phase("compact_confirmed", events=rollout_compaction_count(session_id))
    print(f"    compacted OK (events: {rollout_compaction_count(session_id)})")

    print("4/4 resuming with continuation prompt...")
    cfg = dict(old_cfg)
    cfg.update({"run_id": new_run_id(), "resume_id": session_id, "fork_from": None,
                "created_at": utcnow_iso(), "steered_from": run_dir.name})
    txn_phase("resuming", run_id=cfg["run_id"])
    launch(cfg, continuation, wait=args.wait)
    txn_phase("resumed", run_id=cfg["run_id"])
    print(json.dumps({"checkpoint_compact_resume": {
        "old_run": run_dir.name, "new_run": cfg["run_id"], "session_id": session_id,
        "transaction_log": str(txn_path),
    }}, ensure_ascii=False))


# ---------------------------------------------------------------------------- entry point


def main():
    # GBK/legacy consoles cannot encode every character a codex plan may contain.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(
        prog="codexctl", description="Dispatch and control Codex CLI runs.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("dispatch", help="Start a codex run in the background.")
    p.add_argument("prompt", nargs="?", help="Prompt; reads stdin if omitted.")
    p.add_argument("--session-id", help="Resume this session id.")
    p.add_argument("--fork-from", metavar="PARENT_ID",
                   help="Copy that session into a new thread and run against the copy.")
    shared_dispatch_flags(p)
    p.set_defaults(func=cmd_dispatch)

    p = sub.add_parser("resume", help="Resume a session with a new prompt (background).")
    p.add_argument("session_id")
    p.add_argument("prompt", nargs="?")
    shared_dispatch_flags(p)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("status", help="Show a run's current state (run id, prefix, or session id).")
    p.add_argument("run", nargs="?", default=None)
    p.add_argument("--run-id", dest="run_id_opt", metavar="RUN")
    p.add_argument("--json", action="store_true")
    p.add_argument("--diff", action="store_true",
                   help="Also take a one-shot git snapshot of the workspace (on demand only).")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("watch", help="Live-refresh a run's state until it finishes.")
    p.add_argument("run", nargs="?", default=None)
    p.add_argument("--run-id", dest="run_id_opt", metavar="RUN")
    p.add_argument("--interval", type=float, default=5)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("events", help="Tail a run's event log.")
    p.add_argument("run", nargs="?", default=None)
    p.add_argument("--run-id", dest="run_id_opt", metavar="RUN")
    p.add_argument("-n", "--tail", type=int, default=25)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("cancel", help="Stop a run (soft: waits for an item boundary).")
    p.add_argument("run", nargs="?", default=None)
    p.add_argument("--run-id", dest="run_id_opt", metavar="RUN")
    p.add_argument("--grace", type=int, default=20,
                   help="Seconds to wait for an item boundary before force kill.")
    p.add_argument("--hard", action="store_true", help="Kill the tree immediately.")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("steer",
                       help="Interrupt-and-resume: stop at an item boundary, resume the "
                            "same session with a correction prompt.")
    p.add_argument("run", nargs="?", default=None)
    p.add_argument("prompt", nargs="?")
    p.add_argument("--run-id", dest="run_id_opt", metavar="RUN")
    p.add_argument("--prompt-file", metavar="PATH")
    p.add_argument("--grace", type=int, default=20)
    p.add_argument("--wait", action="store_true")
    p.set_defaults(func=cmd_steer)

    p = sub.add_parser("checkpoint-compact-resume", aliases=["ccr"],
                       help="One transaction: checkpoint turn, compact, resume from a prompt file.")
    p.add_argument("run", nargs="?", default=None)
    p.add_argument("--run-id", dest="run_id_opt", metavar="RUN")
    p.add_argument("--prompt-file", required=True, metavar="PATH",
                   help="Continuation prompt used for the final resume.")
    p.add_argument("--checkpoint-prompt", metavar="TEXT",
                   help="Override the default checkpoint-writing instruction.")
    p.add_argument("--grace", type=int, default=20)
    p.add_argument("--timeout", type=int, default=300, help="Compaction confirm timeout.")
    p.add_argument("--wait", action="store_true")
    p.set_defaults(func=cmd_ccr)

    p = sub.add_parser("compact", help="Force context compaction on a session (app-server).")
    p.add_argument("session_id")
    p.add_argument("--timeout", type=int, default=300)
    p.set_defaults(func=cmd_compact)

    p = sub.add_parser("fork", help="Fork a session without running anything on it yet.")
    p.add_argument("session_id")
    p.set_defaults(func=cmd_fork)

    p = sub.add_parser("list", help="List runs; verifies liveness and heals orphaned records.")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.add_argument("-C", "--project-dir", nargs="?", const=".", default=None, metavar="DIR",
                   help="Only runs for this workspace (bare -C = current directory).")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("supervise", help=argparse.SUPPRESS)
    p.add_argument("--run-dir", required=True)
    p.set_defaults(func=lambda a: Supervisor(Path(a.run_dir)).run())

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
