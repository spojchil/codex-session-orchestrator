# Codex Session Orchestrator

A Claude Code skill that drives the **local OpenAI Codex CLI** as a persistent, observable, controllable background agent.

让 Claude Code 把本机 **Codex CLI** 当作可派发、可观测、可控制的后台执行代理。

## Why / 为什么

Running `codex exec` from an agent normally means blocking on a black box: no session id until it finishes, no idea what it is doing, and the only way to change course is to kill it blind. This skill fixes all of that:

- **Instant dispatch** — returns `run_id` + `session_id` within seconds (parsed from the event stream), while the task keeps running in the background under a detached supervisor.
- **Live structured state** — one `state.json` per run: phase (requesting_model / reasoning / running_tool / …), current command, Codex's own todo-list progress, token usage with **context-window occupancy %**, changed files, health alerts, and whole-process-tree CPU/IO/memory (Windows Job Object accounting).
- **Steer mid-run** — `steer` stops the task at an item boundary and resumes the *same session* with a correction prompt. No wasted context, no half-written files.
- **Reliable cancel** — Job Object–based kill of the entire process tree (npm shim → node → codex → sandbox children), with a soft mode that waits for a tool-call boundary.
- **Proactive context compaction** — `compact` triggers Codex's real context compaction on any session via a temporary app-server, verified against the session file.
- **Native fork** — branch a session (optionally without running anything) to compare two approaches from an identical starting point; parent stays untouched.
- **Health guards** — stall detection, N-commands-without-diff, path violations, context-growth alerts; optionally auto-cancel.
- **Transcript archaeology** — `codex-trace.py`: list/tree (fork *and* subagent lineage), **cross-session full-text search**, archived sessions included.

## Requirements / 前置条件

- [Codex CLI](https://github.com/openai/codex) installed and logged in (`codex --version` works). Developed and tested against codex-cli 0.146.
- Python 3 (standard library only — no pip installs).
- **Windows-first**: process-tree control and perf accounting use Windows Job Objects and are battle-tested on Windows 11. POSIX fallbacks (process groups) exist but are not yet field-tested.
- Dispatching consumes your Codex quota. This skill never talks to the network itself; it only launches local Codex processes and reads local session files.

## Install / 安装

As a plugin (recommended):

```
/plugin marketplace add spojchil/codex-session-orchestrator
/plugin install codex-session-orchestrator@spojchil-skills
```

Or manually: copy `skills/codex-session-orchestrator/` into `~/.claude/skills/`.

## Quick start / 快速上手

Ask Claude to use the skill, or run the scripts directly:

```bash
# dispatch in the background; returns run_id + session_id in seconds
python scripts/codexctl.py dispatch -C /path/to/project --sandbox workspace-write "task description"

# watch it work
python scripts/codexctl.py status          # one-shot snapshot of the latest run
python scripts/codexctl.py watch <run>     # live view until it finishes

# change course mid-run — same session, context preserved
python scripts/codexctl.py steer <run> "stop doing X, do Y instead"

# other controls
python scripts/codexctl.py cancel <run> [--hard]
python scripts/codexctl.py resume <session-id> "follow-up task"
python scripts/codexctl.py compact <session-id>
python scripts/codexctl.py fork <session-id>
python scripts/codexctl.py list

# transcript search
python scripts/codex-trace.py --grep "that topic we discussed"
python scripts/codex-trace.py --tree
```

Run registry lives in `~/.codex-orchestrator/runs/<run_id>/` (`state.json`, `events.jsonl`, `result.json`, `last-message.md`, `alerts.jsonl`).

See [`skills/codex-session-orchestrator/SKILL.md`](skills/codex-session-orchestrator/SKILL.md) for the full command reference and field-tested operational guidance (task-brief writing, worktree parallelism, fork semantics). The skill documentation is currently in Chinese; the scripts' `--help` output is in English.

## Notes / 说明

- Codex's `--json` event schema is an internal contract; after a Codex CLI upgrade, `status` flags unknown event types instead of breaking.
- `compact` and native `fork` use the Codex app-server surface (officially experimental); both fall back gracefully.
- Sessions dispatched via `codex exec` are hidden from the TUI resume picker by default — use `codex resume --include-non-interactive`, or resume by id.

## License

MIT
