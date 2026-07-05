#!/usr/bin/env python3
"""Guarded Codex CLI runner with conservative mechanical recovery.

The guard owns only the Codex process it starts. It never clicks the screen,
never changes proxy/network settings, and never approves permission prompts.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RECOVERABLE_MARKERS = [
    "ResponseStreamDisconnected",
    "ResponseStreamConnectionFailed",
    "HttpConnectionFailed",
    "ResponseTooManyFailedAttempts",
    "stream disconnected",
    "connection failed",
    "connection reset",
    "connection closed",
    "timeout",
    "timed out",
    "econnreset",
    "network",
]

HUMAN_MARKERS = [
    "Unauthorized",
    "UsageLimitExceeded",
    "SessionBudgetExceeded",
    "ContextWindowExceeded",
    "approval",
    "permission",
    "quota",
    "rate limit",
    "login",
    "auth",
    "forbidden",
    "requires human",
    "dangerously",
]


@dataclass
class GuardState:
    run_id: str
    prompt: str
    cwd: str
    state: str = "working"
    label: str = "Starting guarded Codex run"
    detail: str = ""
    session_id: str | None = None
    turn_started: bool = False
    last_event_type: str | None = None
    last_event_at: str | None = None
    updated_at: str = field(default_factory=lambda: now_iso())
    attempt: int = 0
    recovery_attempts: int = 0
    needs_human: bool = False
    recommended_action: str | None = None
    error_info: str | None = None
    exit_code: int | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_state_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "CodexStatusbar"
    return Path.cwd() / ".codex-statusbar"


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def contains_marker(text: str, markers: list[str]) -> bool:
    lower = text.lower()
    return any(marker.lower() in lower for marker in markers)


class GuardedCodexRunner:
    def __init__(
        self,
        *,
        prompt: str,
        cwd: Path,
        state_dir: Path,
        codex_bin: str,
        max_recoveries: int,
        continue_prompt: str,
        codex_args: list[str],
        skip_git_repo_check: bool,
    ) -> None:
        self.state = GuardState(
            run_id=str(uuid.uuid4()),
            prompt=prompt,
            cwd=str(cwd),
        )
        self.cwd = cwd
        self.state_dir = state_dir
        self.status_path = state_dir / "guard_status.json"
        self.events_path = state_dir / "guard_events.jsonl"
        self.actions_path = state_dir / "actions.jsonl"
        self.codex_bin = codex_bin
        self.max_recoveries = max(0, max_recoveries)
        self.continue_prompt = continue_prompt
        self.codex_args = codex_args
        self.skip_git_repo_check = skip_git_repo_check

    def run(self) -> int:
        final_code = 1
        next_mode = "initial"
        while True:
            self.state.attempt += 1
            self._set_status(
                "working",
                "Starting Codex",
                f"Attempt {self.state.attempt}: {next_mode}",
            )

            command = self._build_command(next_mode)
            self._record_action(
                decision="start_process",
                reason=next_mode,
                command=command,
            )
            result = self._run_once(command)
            final_code = result.returncode
            self.state.exit_code = result.returncode
            self._write_status()

            decision = self._decide_after_exit(result)
            self._record_action(
                decision=decision["decision"],
                reason=decision["reason"],
                command=decision.get("command"),
            )

            if decision["decision"] == "complete":
                self._set_status("completed", "Completed", "Guarded Codex run finished.")
                return 0

            if decision["decision"] == "blocked":
                self._set_status(
                    "failed",
                    "Needs human attention",
                    decision["reason"],
                    needs_human=True,
                    recommended_action=decision.get("recommended_action"),
                )
                return final_code or 1

            if decision["decision"] == "retry_original":
                self.state.recovery_attempts += 1
                next_mode = "initial"
                continue

            if decision["decision"] == "resume_continue":
                self.state.recovery_attempts += 1
                next_mode = "resume"
                continue

            self._set_status("failed", "Recovery stopped", decision["reason"], needs_human=True)
            return final_code or 1

    def _build_command(self, mode: str) -> list[str]:
        base = [self.codex_bin, "exec"]
        if mode == "resume":
            base.append("resume")
        base.append("--json")
        if self.skip_git_repo_check:
            base.append("--skip-git-repo-check")
        base.extend(self.codex_args)
        if mode == "resume":
            if self.state.session_id:
                base.append(self.state.session_id)
            else:
                base.append("--last")
            base.append(self.continue_prompt)
        else:
            base.append(self.state.prompt)
        return base

    def _run_once(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        stderr_lines: list[str] = []
        process = subprocess.Popen(
            command,
            cwd=str(self.cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        stderr_queue: queue.Queue[str] = queue.Queue()

        def read_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                stderr_queue.put(line.rstrip("\n"))

        threading.Thread(target=read_stderr, daemon=True).start()

        assert process.stdout is not None
        for line in process.stdout:
            self._drain_stderr(stderr_queue, stderr_lines)
            stripped = line.strip()
            if not stripped:
                continue
            self._handle_stdout_line(stripped)

        returncode = process.wait()
        self._drain_stderr(stderr_queue, stderr_lines)
        if stderr_lines:
            append_jsonl(
                self.events_path,
                {
                    "timestamp": now_iso(),
                    "run_id": self.state.run_id,
                    "type": "stderr",
                    "lines": stderr_lines[-20:],
                },
            )
            if not self.state.error_info:
                joined = "\n".join(stderr_lines[-5:])
                if joined.strip():
                    self.state.error_info = joined.strip()
        return subprocess.CompletedProcess(command, returncode, "", "\n".join(stderr_lines))

    def _drain_stderr(self, stderr_queue: queue.Queue[str], stderr_lines: list[str]) -> None:
        while True:
            try:
                line = stderr_queue.get_nowait()
            except queue.Empty:
                break
            stderr_lines.append(line)

    def _handle_stdout_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            append_jsonl(
                self.events_path,
                {
                    "timestamp": now_iso(),
                    "run_id": self.state.run_id,
                    "type": "parse_error",
                    "line": line[:1000],
                },
            )
            return
        if not isinstance(event, dict):
            return

        event_type = str(event.get("type") or "")
        self.state.last_event_type = event_type
        self.state.last_event_at = now_iso()
        self.state.updated_at = self.state.last_event_at

        append_jsonl(
            self.events_path,
            {
                "timestamp": self.state.updated_at,
                "run_id": self.state.run_id,
                "event": event,
            },
        )

        if event_type == "thread.started":
            session_id = event.get("thread_id")
            if isinstance(session_id, str) and session_id:
                self.state.session_id = session_id
            self._set_status("working", "Thread started", self.state.session_id or "")
            return

        if event_type == "turn.started":
            self.state.turn_started = True
            self._set_status("working", "Thinking", "Turn started.")
            return

        if event_type in {"item.started", "item.updated", "item.completed"}:
            self._handle_item_event(event_type, event)
            return

        if event_type == "turn.completed":
            self._set_status("completed", "Completed", "Turn completed.")
            return

        if event_type == "turn.failed":
            message = self._extract_error(event) or "Turn failed."
            self.state.error_info = message
            self._set_status("failed", "Turn failed", message)
            return

        if event_type == "error":
            message = self._extract_error(event) or json.dumps(event, ensure_ascii=False)[:300]
            self.state.error_info = message
            label = "Recoverable stream error" if contains_marker(message, RECOVERABLE_MARKERS) else "Error"
            state = "reconnecting" if contains_marker(message, RECOVERABLE_MARKERS) else "failed"
            self._set_status(state, label, message)

    def _handle_item_event(self, event_type: str, event: dict[str, Any]) -> None:
        item = event.get("item")
        if not isinstance(item, dict):
            return
        item_type = str(item.get("type") or "")

        if item_type == "agent_message":
            text = str(item.get("text") or "")
            self._set_status("outputting", "Outputting", text[:160])
        elif item_type == "reasoning":
            self._set_status("working", "Reasoning", "Codex is thinking.")
        elif item_type == "command_execution":
            command = str(item.get("command") or "")
            status = str(item.get("status") or "")
            self._set_status("executing", "Executing command", f"{status}: {command}"[:180])
            if status in {"declined"}:
                self.state.needs_human = True
        elif item_type == "file_change":
            status = str(item.get("status") or "")
            self._set_status("executing", "Editing files", f"File change {status}")
            if status in {"declined", "failed"}:
                self.state.needs_human = True
        elif item_type in {"mcp_tool_call", "collab_tool_call"}:
            status = str(item.get("status") or "")
            self._set_status("executing", "Calling tool", f"{item_type} {status}")
        elif item_type == "error":
            message = str(item.get("message") or item.get("text") or "Item error.")
            self.state.error_info = message
            self._set_status("failed", "Item error", message)
        else:
            self._set_status("working", event_type, item_type)

    def _extract_error(self, event: dict[str, Any]) -> str | None:
        error = event.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            details = error.get("additional_details") or error.get("additionalDetails")
            info = error.get("codex_error_info") or error.get("codexErrorInfo")
            parts = [part for part in [message, info, details] if isinstance(part, str) and part]
            if parts:
                return " | ".join(parts)
        if isinstance(error, str):
            return error
        message = event.get("message")
        if isinstance(message, str):
            return message
        return None

    def _decide_after_exit(self, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        error_text = "\n".join(
            part for part in [self.state.error_info or "", result.stderr or ""] if part
        )

        if self.state.state == "completed" and result.returncode == 0:
            return {"decision": "complete", "reason": "turn completed with exit code 0"}

        if contains_marker(error_text, HUMAN_MARKERS):
            return {
                "decision": "blocked",
                "reason": error_text[:500] or "Human attention is required.",
                "recommended_action": "Open Codex and handle login, quota, approval, or permission state.",
            }

        if self.state.recovery_attempts >= self.max_recoveries:
            return {
                "decision": "blocked",
                "reason": f"Recovery limit reached ({self.max_recoveries}).",
                "recommended_action": "Inspect guard_events.jsonl and decide whether to continue manually.",
            }

        recoverable = result.returncode != 0 or contains_marker(error_text, RECOVERABLE_MARKERS)
        if not recoverable:
            return {
                "decision": "blocked",
                "reason": f"Process exited with code {result.returncode}; no safe recovery rule matched.",
                "recommended_action": "Inspect guard_events.jsonl before retrying.",
            }

        if not self.state.turn_started:
            command = self._build_command("initial")
            self._set_status(
                "recovering",
                "Retrying original prompt",
                "Codex exited before the turn began.",
            )
            return {
                "decision": "retry_original",
                "reason": "failure before turn.started",
                "command": command,
            }

        if self.state.session_id:
            command = self._build_command("resume")
            self._set_status(
                "recovering",
                "Resuming with continue",
                f"Session {self.state.session_id}",
            )
            return {
                "decision": "resume_continue",
                "reason": "failure after turn.started",
                "command": command,
            }

        return {
            "decision": "blocked",
            "reason": "Turn started but no session id was captured; refusing --last fallback.",
            "recommended_action": "Resume manually and send 继续.",
        }

    def _set_status(
        self,
        state: str,
        label: str,
        detail: str,
        *,
        needs_human: bool | None = None,
        recommended_action: str | None = None,
    ) -> None:
        self.state.state = state
        self.state.label = label
        self.state.detail = detail
        self.state.updated_at = now_iso()
        if needs_human is not None:
            self.state.needs_human = needs_human
        if recommended_action is not None:
            self.state.recommended_action = recommended_action
        self._write_status()

    def _write_status(self) -> None:
        atomic_write_json(self.status_path, asdict(self.state))

    def _record_action(
        self,
        *,
        decision: str,
        reason: str,
        command: list[str] | None = None,
    ) -> None:
        append_jsonl(
            self.actions_path,
            {
                "timestamp": now_iso(),
                "run_id": self.state.run_id,
                "session_id": self.state.session_id,
                "decision": decision,
                "reason": reason,
                "attempt": self.state.attempt,
                "recovery_attempts": self.state.recovery_attempts,
                "command": command,
            },
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Codex with conservative auto recovery.")
    parser.add_argument("prompt", nargs="*", help="Prompt to pass to codex exec.")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--max-recoveries", type=int, default=2)
    parser.add_argument("--continue-prompt", default="继续")
    parser.add_argument(
        "--codex-arg",
        action="append",
        default=[],
        help="Extra argument passed to codex exec before the prompt. Repeatable.",
    )
    parser.add_argument(
        "--require-git-repo",
        action="store_true",
        help="Do not add --skip-git-repo-check.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        if not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
    if not prompt:
        print("codex_guard: prompt is required", file=sys.stderr)
        return 2

    runner = GuardedCodexRunner(
        prompt=prompt,
        cwd=args.cwd.expanduser().resolve(),
        state_dir=args.state_dir.expanduser(),
        codex_bin=args.codex_bin,
        max_recoveries=args.max_recoveries,
        continue_prompt=args.continue_prompt,
        codex_args=args.codex_arg,
        skip_git_repo_check=not args.require_git_repo,
    )
    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
