#!/usr/bin/env python3
"""Codex app-server runner that writes statusbar-compatible state files.

This runner is intentionally conservative. It can start and monitor app-server
threads, but it does not approve permission prompts, click the screen, or change
network/proxy settings.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
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
    "responseStreamDisconnected",
    "responseStreamConnectionFailed",
    "httpConnectionFailed",
    "responseTooManyFailedAttempts",
    "ResponseStreamDisconnected",
    "ResponseStreamConnectionFailed",
    "HttpConnectionFailed",
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
    "unauthorized",
    "usageLimitExceeded",
    "contextWindowExceeded",
    "SessionBudgetExceeded",
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

APPROVAL_REQUEST_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
    "applyPatchApproval",
    "execCommandApproval",
}


@dataclass
class AppServerState:
    run_id: str
    prompt: str
    cwd: str
    source: str = "app-server"
    state: str = "working"
    label: str = "Starting Codex app-server run"
    detail: str = ""
    thread_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
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
    recoverable_error: bool = False
    terminal: bool = False


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


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def user_input(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text, "text_elements": []}]


def resolve_codex_bin(configured: str) -> str:
    if configured != "codex":
        return configured
    return (
        shutil.which("codex.cmd")
        or shutil.which("codex.exe")
        or shutil.which("codex")
        or configured
    )


class CodexAppServerRunner:
    def __init__(
        self,
        *,
        prompt: str,
        cwd: Path,
        state_dir: Path,
        codex_bin: str,
        max_recoveries: int,
        continue_prompt: str,
        appserver_args: list[str],
        model: str | None,
        approval_policy: str | None,
        sandbox: str | None,
        ephemeral: bool,
        stale_seconds: int,
    ) -> None:
        self.state = AppServerState(
            run_id=str(uuid.uuid4()),
            prompt=prompt,
            cwd=str(cwd),
        )
        self.cwd = cwd
        self.state_dir = state_dir
        self.status_path = state_dir / "appserver_status.json"
        self.events_path = state_dir / "appserver_events.jsonl"
        self.actions_path = state_dir / "actions.jsonl"
        self.codex_bin = resolve_codex_bin(codex_bin)
        self.max_recoveries = max(0, max_recoveries)
        self.continue_prompt = continue_prompt
        self.appserver_args = appserver_args
        self.model = model
        self.approval_policy = approval_policy
        self.sandbox = sandbox
        self.ephemeral = ephemeral
        self.stale_seconds = stale_seconds
        self._process: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._next_id = 1
        self._last_message_at = time.monotonic()

    def run(self) -> int:
        prompt = self.state.prompt
        mode = "initial"
        final_code = 1
        try:
            self._ensure_server()
            self._ensure_thread()
            while True:
                self.state.attempt += 1
                self._reset_turn_for_attempt()
                self._record_action("turn_start", mode, prompt=prompt)
                turn_response = self._request(
                    "turn/start",
                    {
                        "threadId": self.state.thread_id,
                        "input": user_input(prompt),
                    },
                    timeout_seconds=60,
                )
                if turn_response is None:
                    self.state.terminal = True
                    if not self.state.needs_human:
                        self._set_status(
                            "failed",
                            "Failed to start turn",
                            self.state.error_info or "No turn/start response from app-server.",
                            error_info=self.state.error_info or "turn/start failed",
                        )
                else:
                    self._wait_for_terminal()
                decision = self._decide_after_terminal()
                self._record_action(
                    decision["decision"],
                    decision["reason"],
                    prompt=decision.get("prompt"),
                )

                if decision["decision"] == "complete":
                    final_code = 0
                    break
                if decision["decision"] == "blocked":
                    final_code = 1
                    break

                self.state.recovery_attempts += 1
                prompt = str(decision["prompt"])
                mode = decision["decision"]
                self._set_status("recovering", "Recovering", decision["reason"])
                self._ensure_server()
                self._ensure_thread()
        finally:
            self._write_status()
            self._stop_server()
        return final_code

    def _ensure_server(self) -> None:
        if self._process and self._process.poll() is None:
            return
        command = [self.codex_bin, "app-server", "--stdio", *self.appserver_args]
        self._record_action("start_app_server", "launch stdio app-server", command=command)
        self._process = subprocess.Popen(
            command,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        threading.Thread(
            target=self._read_stream,
            args=(self._process.stdout, "stdout"),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stream,
            args=(self._process.stderr, "stderr"),
            daemon=True,
        ).start()
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-statusbar",
                    "title": "Codex Statusbar",
                    "version": "0.1.0",
                },
                "capabilities": None,
            },
            timeout_seconds=30,
        )
        self._send_notification("initialized")

    def _ensure_thread(self) -> None:
        if self.state.thread_id:
            response = self._request(
                "thread/resume",
                {"threadId": self.state.thread_id, **self._thread_overrides()},
                timeout_seconds=60,
            )
            if response is not None:
                self._absorb_thread_response(response)
                return
            self.state.thread_id = None
        response = self._request(
            "thread/start",
            {
                **self._thread_overrides(),
                "cwd": str(self.cwd),
                "ephemeral": self.ephemeral,
            },
            timeout_seconds=60,
        )
        if response is None:
            raise RuntimeError("app-server did not start a thread")
        self._absorb_thread_response(response)

    def _thread_overrides(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.model:
            params["model"] = self.model
        if self.approval_policy:
            params["approvalPolicy"] = self.approval_policy
        if self.sandbox:
            params["sandbox"] = self.sandbox
        return params

    def _reset_turn_for_attempt(self) -> None:
        self.state.turn_id = None
        self.state.turn_started = False
        self.state.needs_human = False
        self.state.recommended_action = None
        self.state.error_info = None
        self.state.recoverable_error = False
        self.state.terminal = False
        self._set_status("working", "Starting turn", f"Attempt {self.state.attempt}")

    def _wait_for_terminal(self) -> None:
        while not self.state.terminal and not self.state.needs_human:
            processed = self._process_one_message(timeout_seconds=1.0)
            if processed:
                continue
            if self._process and self._process.poll() is not None:
                self.state.exit_code = self._process.returncode
                self.state.terminal = True
                self._set_status(
                    "failed",
                    "app-server exited",
                    f"Process exited with code {self._process.returncode}.",
                    error_info=self.state.error_info,
                )
                return
            idle = time.monotonic() - self._last_message_at
            if (
                self.stale_seconds > 0
                and idle > self.stale_seconds
                and self.state.state not in {"stale", "completed", "failed", "waiting"}
            ):
                self._set_status(
                    "stale",
                    "Possibly stuck",
                    f"No app-server event for {int(idle)}s.",
                    recommended_action="Check Codex before forcing recovery.",
                )

    def _decide_after_terminal(self) -> dict[str, Any]:
        if self.state.state == "completed":
            return {"decision": "complete", "reason": "turn completed"}
        if self.state.needs_human or contains_marker(self.state.error_info or "", HUMAN_MARKERS):
            self.state.needs_human = True
            self._set_status(
                "failed",
                "Needs human attention",
                self.state.error_info or self.state.detail,
                needs_human=True,
                recommended_action="Open Codex and handle the blocked request.",
            )
            return {"decision": "blocked", "reason": "human intervention required"}
        if self.state.recovery_attempts >= self.max_recoveries:
            self._set_status(
                "failed",
                "Recovery limit reached",
                self.state.error_info or self.state.detail,
                needs_human=True,
                recommended_action="Check Codex and network/proxy state manually.",
            )
            return {"decision": "blocked", "reason": "max recoveries reached"}
        if self.state.recoverable_error:
            if self.state.turn_started:
                return {
                    "decision": "resume_continue",
                    "reason": "recoverable error after turn started",
                    "prompt": self.continue_prompt,
                }
            return {
                "decision": "retry_original",
                "reason": "recoverable error before turn started",
                "prompt": self.state.prompt,
            }
        return {
            "decision": "blocked",
            "reason": self.state.error_info or "non-recoverable app-server failure",
        }

    def _request(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._write_line(payload)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            message = self._next_message(timeout_seconds=0.5)
            if message is None:
                continue
            if message.get("id") == request_id:
                self._log_event("response", message)
                if "error" in message:
                    self._handle_rpc_error(message["error"], method)
                    return None
                result = message.get("result")
                return result if isinstance(result, dict) else {}
            self._handle_message(message)
            if self.state.needs_human:
                return None
        self._set_status(
            "failed",
            f"{method} timed out",
            f"No JSON-RPC response within {int(timeout_seconds)}s.",
            error_info=f"{method} timed out",
        )
        return None

    def _process_one_message(self, *, timeout_seconds: float) -> bool:
        message = self._next_message(timeout_seconds=timeout_seconds)
        if message is None:
            return False
        self._handle_message(message)
        return True

    def _next_message(self, *, timeout_seconds: float) -> dict[str, Any] | None:
        try:
            stream, line = self._queue.get(timeout=timeout_seconds)
        except queue.Empty:
            return None
        if stream == "stderr":
            self._log_event("stderr", {"line": line})
            if not self.state.error_info and "ERROR" in line.upper():
                self.state.error_info = line
            return None
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            self._log_event("raw", {"stream": stream, "line": line})
            return None
        if isinstance(message, dict):
            return message
        return None

    def _handle_message(self, message: dict[str, Any]) -> bool:
        self._last_message_at = time.monotonic()
        method = message.get("method")
        if isinstance(method, str):
            self.state.last_event_type = method
            self.state.last_event_at = now_iso()
        self._log_event("message", message)

        if "id" in message and "method" in message:
            return self._handle_server_request(message)
        if "error" in message:
            self._handle_rpc_error(message["error"], "response")
            return self.state.terminal
        if not isinstance(method, str):
            return False

        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "thread/started":
            thread = params.get("thread") if isinstance(params.get("thread"), dict) else {}
            self._absorb_thread(thread)
            self._set_status("working", "Thread started", self.state.thread_id or "")
        elif method == "turn/started":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            self.state.turn_id = str(turn.get("id") or params.get("turnId") or "")
            self.state.turn_started = True
            self._set_status("working", "Thinking", f"Turn {self.state.turn_id}")
        elif method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            status = str(turn.get("status") or "")
            if status == "completed":
                self._set_status("completed", "Completed", "Turn completed.")
            else:
                error_text = self._error_text(turn.get("error"))
                self.state.error_info = error_text or f"Turn status: {status}"
                self.state.recoverable_error = contains_marker(
                    self.state.error_info, RECOVERABLE_MARKERS
                )
                self._set_status(
                    "reconnecting" if self.state.recoverable_error else "failed",
                    "Turn failed",
                    self.state.error_info,
                    error_info=self.state.error_info,
                )
            self.state.terminal = True
        elif method == "error":
            error_text = self._error_text(params.get("error"))
            will_retry = bool(params.get("willRetry"))
            self.state.error_info = error_text or "app-server reported an error"
            self.state.recoverable_error = will_retry or contains_marker(
                self.state.error_info, RECOVERABLE_MARKERS
            )
            self._set_status(
                "reconnecting" if self.state.recoverable_error else "failed",
                "Stream or network failure" if self.state.recoverable_error else "Error",
                self.state.error_info,
                error_info=self.state.error_info,
            )
            if not will_retry:
                self.state.terminal = True
        elif method == "item/agentMessage/delta":
            delta = str(params.get("delta") or "")
            self._set_status("outputting", "Outputting", delta[:160])
        elif method in {
            "item/reasoning/textDelta",
            "item/reasoning/summaryTextDelta",
            "item/reasoning/summaryPartAdded",
            "item/plan/delta",
            "turn/plan/updated",
        }:
            self._set_status("working", "Thinking", method)
        elif method in {"item/started", "item/completed"}:
            self._handle_item(method, params)
        elif method in {"thread/status/changed", "thread/tokenUsage/updated"}:
            self._set_status(self.state.state, self.state.label, method)
        self._write_status()
        return self.state.terminal

    def _handle_server_request(self, message: dict[str, Any]) -> bool:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        if method not in APPROVAL_REQUEST_METHODS:
            self._send_response(
                request_id,
                error={"code": -32601, "message": f"Unsupported server request: {method}"},
            )
            return False

        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        detail = (
            str(params.get("command") or "")
            or str(params.get("reason") or "")
            or "Codex is asking for approval or user input."
        )
        self._set_status(
            "waiting",
            "Waiting for human approval",
            detail,
            needs_human=True,
            recommended_action="Open Codex/app-server client and review the request manually.",
            error_info=f"{method}: {detail}",
        )
        if method in {
            "item/commandExecution/requestApproval",
            "applyPatchApproval",
            "execCommandApproval",
        }:
            self._send_response(request_id, {"decision": "cancel"})
        elif method == "item/fileChange/requestApproval":
            self._send_response(request_id, {"decision": "cancel"})
        elif method == "mcpServer/elicitation/request":
            self._send_response(request_id, {"action": "cancel", "content": None, "_meta": None})
        elif method == "item/tool/requestUserInput":
            self._send_response(request_id, {"answers": {}})
        else:
            self._send_response(
                request_id,
                error={"code": -32000, "message": "Manual permission approval required."},
            )
        self.state.terminal = True
        self._write_status()
        return True

    def _handle_item(self, method: str, params: dict[str, Any]) -> None:
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        item_type = str(item.get("type") or "")
        if item_type == "commandExecution":
            command = str(item.get("command") or "")
            self._set_status("executing", "Executing command", command[:160])
        elif item_type == "fileChange":
            self._set_status("executing", "Editing files", method)
        elif item_type in {"mcpToolCall", "dynamicToolCall", "collabAgentToolCall"}:
            tool = str(item.get("tool") or item.get("namespace") or item_type)
            self._set_status("executing", "Calling tool", tool[:160])
        elif item_type == "agentMessage":
            text = str(item.get("text") or "")
            self._set_status("outputting", "Outputting", text[:160] or method)
        elif item_type in {"reasoning", "plan"}:
            self._set_status("working", "Thinking", item_type)

    def _handle_rpc_error(self, error: Any, context: str) -> None:
        text = self._error_text(error) or str(error)
        self.state.error_info = f"{context}: {text}"
        self.state.recoverable_error = contains_marker(text, RECOVERABLE_MARKERS)
        self.state.needs_human = contains_marker(text, HUMAN_MARKERS)
        self._set_status(
            "reconnecting" if self.state.recoverable_error else "failed",
            "Recoverable app-server error" if self.state.recoverable_error else "app-server error",
            text,
            needs_human=self.state.needs_human,
            recommended_action=(
                "Handle login/quota/approval manually."
                if self.state.needs_human
                else "Retry or inspect app-server logs."
            ),
            error_info=self.state.error_info,
        )

    def _error_text(self, error: Any) -> str:
        if isinstance(error, str):
            return error
        if not isinstance(error, dict):
            return ""
        parts: list[str] = []
        for key in ("message", "additionalDetails"):
            value = error.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
        info = error.get("codexErrorInfo")
        if info is not None:
            parts.append(compact_json(info))
        code = error.get("code")
        if code is not None:
            parts.append(f"code={code}")
        return " | ".join(parts)

    def _absorb_thread_response(self, response: dict[str, Any]) -> None:
        thread = response.get("thread") if isinstance(response.get("thread"), dict) else {}
        self._absorb_thread(thread)
        self._set_status("working", "Thread ready", self.state.thread_id or "")

    def _absorb_thread(self, thread: dict[str, Any]) -> None:
        thread_id = thread.get("id")
        session_id = thread.get("sessionId")
        cwd = thread.get("cwd")
        if isinstance(thread_id, str):
            self.state.thread_id = thread_id
        if isinstance(session_id, str):
            self.state.session_id = session_id
        if isinstance(cwd, str):
            self.state.cwd = cwd

    def _set_status(
        self,
        state: str,
        label: str,
        detail: str,
        *,
        needs_human: bool | None = None,
        recommended_action: str | None = None,
        error_info: str | None = None,
    ) -> None:
        self.state.state = state
        self.state.label = label
        self.state.detail = detail
        self.state.updated_at = now_iso()
        if needs_human is not None:
            self.state.needs_human = needs_human
        if recommended_action is not None:
            self.state.recommended_action = recommended_action
        if error_info is not None:
            self.state.error_info = error_info
        self._write_status()

    def _write_status(self) -> None:
        atomic_write_json(self.status_path, asdict(self.state))

    def _log_event(self, kind: str, value: dict[str, Any]) -> None:
        append_jsonl(
            self.events_path,
            {
                "timestamp": now_iso(),
                "run_id": self.state.run_id,
                "kind": kind,
                "value": value,
            },
        )

    def _record_action(
        self,
        decision: str,
        reason: str,
        *,
        command: list[str] | None = None,
        prompt: str | None = None,
    ) -> None:
        append_jsonl(
            self.actions_path,
            {
                "timestamp": now_iso(),
                "run_id": self.state.run_id,
                "source": "app-server",
                "thread_id": self.state.thread_id,
                "turn_id": self.state.turn_id,
                "decision": decision,
                "reason": reason,
                "command": command,
                "prompt": prompt,
            },
        )

    def _read_stream(self, stream: Any, name: str) -> None:
        for line in stream:
            stripped = line.rstrip("\n")
            if stripped:
                self._queue.put((name, stripped))

    def _write_line(self, payload: dict[str, Any]) -> None:
        if not self._process or not self._process.stdin:
            raise RuntimeError("app-server is not running")
        self._process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._process.stdin.flush()

    def _send_notification(self, method: str) -> None:
        self._write_line({"method": method})

    def _send_response(
        self,
        request_id: Any,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        if request_id is None:
            return
        payload: dict[str, Any] = {"id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result if result is not None else {}
        if self._process and self._process.stdin:
            self._write_line(payload)

    def _stop_server(self) -> None:
        if not self._process:
            return
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self.state.exit_code = self._process.returncode


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Codex through app-server and publish status.")
    parser.add_argument("prompt", nargs="*", help="Prompt to send to Codex.")
    parser.add_argument("--cwd", default=str(Path.cwd()), help="Working directory for the Codex thread.")
    parser.add_argument("--state-dir", default=str(default_state_dir()), help="Status/log directory.")
    parser.add_argument("--codex-bin", default="codex", help="Path to codex/codex.cmd.")
    parser.add_argument("--max-recoveries", type=int, default=2, help="Maximum mechanical recoveries.")
    parser.add_argument("--continue-prompt", default="继续", help="Prompt used after a mid-turn failure.")
    parser.add_argument("--model", default=None, help="Optional model override.")
    parser.add_argument("--approval-policy", default=None, help="Optional app-server approval policy override.")
    parser.add_argument("--sandbox", default=None, help="Optional app-server sandbox override.")
    parser.add_argument("--ephemeral", action="store_true", help="Do not materialize the thread on disk.")
    parser.add_argument("--stale-seconds", type=int, default=300, help="Mark status stale after no events.")
    parser.add_argument(
        "--appserver-arg",
        action="append",
        default=[],
        help="Extra argument passed to `codex app-server` before --stdio. Repeatable.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        print("Prompt is required.", file=sys.stderr)
        return 2
    runner = CodexAppServerRunner(
        prompt=prompt,
        cwd=Path(args.cwd),
        state_dir=Path(args.state_dir),
        codex_bin=args.codex_bin,
        max_recoveries=args.max_recoveries,
        continue_prompt=args.continue_prompt,
        appserver_args=args.appserver_arg,
        model=args.model,
        approval_policy=args.approval_policy,
        sandbox=args.sandbox,
        ephemeral=args.ephemeral,
        stale_seconds=args.stale_seconds,
    )
    try:
        return runner.run()
    except KeyboardInterrupt:
        runner._set_status("failed", "Interrupted", "Stopped by user.", needs_human=True)
        return 130
    except Exception as exc:
        runner._set_status(
            "failed",
            "app-server runner crashed",
            str(exc),
            needs_human=True,
            error_info=str(exc),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
