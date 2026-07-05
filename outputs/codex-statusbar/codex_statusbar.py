#!/usr/bin/env python3
"""Tiny Codex status bar for Windows.

This MVP watches local Codex rollout JSONL files and shows an always-on-top
desktop status strip. It is intentionally read-only: it never sends input to
Codex and never changes network/proxy settings.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox
from typing import Any


APP_NAME = "Codex Statusbar"
DEFAULT_STALE_SECONDS = 300
DEFAULT_POLL_SECONDS = 2.0
DEFAULT_DESKTOP_EVENT_SECONDS = 180
MAX_FILES = 60
MAX_FILE_BYTES = 1_500_000


PALETTE = {
    "working": ("#0f766e", "#ecfeff"),
    "outputting": ("#2563eb", "#eff6ff"),
    "executing": ("#7c3aed", "#f5f3ff"),
    "waiting": ("#b45309", "#fffbeb"),
    "recovering": ("#0369a1", "#e0f2fe"),
    "reconnecting": ("#c2410c", "#fff7ed"),
    "stale": ("#991b1b", "#fef2f2"),
    "completed": ("#15803d", "#f0fdf4"),
    "failed": ("#7f1d1d", "#fef2f2"),
    "idle": ("#334155", "#f8fafc"),
}


@dataclass
class StatusSnapshot:
    state: str
    label: str
    detail: str
    session_id: str | None
    source_file: str | None
    cwd: str | None
    last_event_type: str | None
    last_event_age_seconds: float | None
    updated_at: str
    needs_human: bool = False
    recommended_action: str | None = None
    error_info: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def safe_read_recent_text(path: Path) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > MAX_FILE_BYTES:
                handle.seek(size - MAX_FILE_BYTES)
                data = handle.read()
                first_newline = data.find(b"\n")
                if first_newline >= 0:
                    data = data[first_newline + 1 :]
            else:
                data = handle.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    text = safe_read_recent_text(path)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def session_id_from_path(path: Path) -> str:
    stem = path.stem
    return stem.removeprefix("rollout-")


class CodexSessionWatcher:
    def __init__(
        self,
        codex_home: Path,
        state_dir: Path,
        stale_seconds: int = DEFAULT_STALE_SECONDS,
    ) -> None:
        self.codex_home = codex_home
        self.sessions_dir = codex_home / "sessions"
        self.state_dir = state_dir
        self.status_path = state_dir / "status.json"
        self.appserver_status_path = state_dir / "appserver_status.json"
        self.guard_status_path = state_dir / "guard_status.json"
        self.events_path = state_dir / "events.jsonl"
        self.actions_path = state_dir / "actions.jsonl"
        self.stale_seconds = stale_seconds
        self._last_event_key: str | None = None
        self._last_action_key: str | None = None
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def scan(self) -> StatusSnapshot:
        external_snapshot = self._external_snapshot()
        if external_snapshot:
            self._write_status(external_snapshot)
            self._append_event_if_changed(external_snapshot)
            self._append_action_if_needed(external_snapshot)
            return external_snapshot

        files = self._recent_session_files()
        if not files:
            snapshot = self._desktop_snapshot(None) or StatusSnapshot(
                state="idle",
                label="No recent Codex session",
                detail=f"Watching {self.sessions_dir}",
                session_id=None,
                source_file=None,
                cwd=None,
                last_event_type=None,
                last_event_age_seconds=None,
                updated_at=now_iso(),
                recommended_action="Start a Codex task, then this bar will follow it.",
            )
            self._write_status(snapshot)
            self._append_event_if_changed(snapshot)
            self._append_action_if_needed(snapshot)
            return snapshot

        candidates: list[StatusSnapshot] = []
        for path in files:
            snapshot = self._snapshot_for_file(path)
            if snapshot:
                candidates.append(snapshot)

        if not candidates:
            snapshot = self._desktop_snapshot(None) or StatusSnapshot(
                state="idle",
                label="No readable Codex events",
                detail=f"Found {len(files)} recent JSONL file(s), but no parseable events.",
                session_id=None,
                source_file=None,
                cwd=None,
                last_event_type=None,
                last_event_age_seconds=None,
                updated_at=now_iso(),
                needs_human=True,
                recommended_action="Check whether session files are still being written.",
            )
            self._write_status(snapshot)
            self._append_event_if_changed(snapshot)
            self._append_action_if_needed(snapshot)
            return snapshot

        snapshot = max(
            candidates,
            key=lambda item: (
                item.last_event_age_seconds is not None,
                -(item.last_event_age_seconds or 10**9),
            ),
        )
        desktop_snapshot = self._desktop_snapshot(snapshot)
        if desktop_snapshot:
            snapshot = desktop_snapshot
        self._write_status(snapshot)
        self._append_event_if_changed(snapshot)
        self._append_action_if_needed(snapshot)
        return snapshot

    def _recent_session_files(self) -> list[Path]:
        if not self.sessions_dir.exists():
            return []
        files: list[Path] = []
        try:
            iterator = self.sessions_dir.rglob("*.jsonl")
            for path in iterator:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                age = time.time() - stat.st_mtime
                if age <= 48 * 3600:
                    files.append(path)
        except OSError:
            return []
        files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        return files[:MAX_FILES]

    def _snapshot_for_file(self, path: Path) -> StatusSnapshot | None:
        rows = load_jsonl(path)
        if not rows:
            return None

        session_id = session_id_from_path(path)
        cwd: str | None = None
        last_type: str | None = None
        last_payload_type: str | None = None
        last_ts: datetime | None = None
        saw_task_complete = False
        saw_error = False
        error_info: str | None = None
        last_agent_message = ""
        saw_reasoning = False
        saw_tool_or_command = False
        waiting_reason: str | None = None

        for row in rows:
            row_type = str(row.get("type") or "")
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            timestamp = parse_timestamp(row.get("timestamp"))
            if timestamp:
                last_ts = timestamp

            if row_type in {"session_meta", "turn_context"}:
                cwd_value = payload.get("cwd")
                if isinstance(cwd_value, str) and cwd_value:
                    cwd = cwd_value

            if row_type == "event_msg":
                payload_type = str(payload.get("type") or "")
                last_type = "event_msg"
                last_payload_type = payload_type or None

                if payload_type == "task_started":
                    saw_task_complete = False
                    saw_error = False
                    saw_reasoning = False
                    saw_tool_or_command = False
                    waiting_reason = None
                elif payload_type == "task_complete":
                    saw_task_complete = True
                    message = payload.get("last_agent_message")
                    if isinstance(message, str):
                        last_agent_message = message
                elif payload_type == "error":
                    saw_error = True
                    info = payload.get("codex_error_info") or payload.get("message")
                    if isinstance(info, str):
                        error_info = info
                elif payload_type == "agent_message":
                    message = payload.get("message")
                    if isinstance(message, str):
                        last_agent_message = message
                elif payload_type == "user_message":
                    saw_task_complete = False
                    saw_error = False

            elif row_type == "response_item":
                payload_type = str(payload.get("type") or "")
                last_type = "response_item"
                last_payload_type = payload_type or None

                if payload_type == "reasoning":
                    saw_reasoning = True
                elif payload_type in {"function_call", "commandExecution", "mcpToolCall"}:
                    saw_tool_or_command = True
                    args = payload.get("arguments")
                    if isinstance(args, str) and "require_escalated" in args:
                        waiting_reason = "Codex requested elevated permissions."
                    name = payload.get("name") or payload.get("tool")
                    if isinstance(name, str) and name:
                        last_agent_message = f"Tool: {name}"
                elif payload_type == "message":
                    role = payload.get("role")
                    if role == "assistant":
                        text = payload.get("content") or payload.get("text")
                        if isinstance(text, str):
                            last_agent_message = text

        age: float | None = None
        if last_ts:
            age = max(0.0, (datetime.now(timezone.utc) - last_ts).total_seconds())
        else:
            try:
                age = max(0.0, time.time() - path.stat().st_mtime)
            except OSError:
                age = None

        state, label, detail, needs_human, action = self._classify(
            age=age,
            saw_task_complete=saw_task_complete,
            saw_error=saw_error,
            error_info=error_info,
            saw_reasoning=saw_reasoning,
            saw_tool_or_command=saw_tool_or_command,
            waiting_reason=waiting_reason,
            last_type=last_type,
            last_payload_type=last_payload_type,
            last_agent_message=last_agent_message,
        )

        return StatusSnapshot(
            state=state,
            label=label,
            detail=detail,
            session_id=session_id,
            source_file=str(path),
            cwd=cwd,
            last_event_type=":".join(
                part for part in [last_type, last_payload_type] if part
            )
            or None,
            last_event_age_seconds=age,
            updated_at=now_iso(),
            needs_human=needs_human,
            recommended_action=action,
            error_info=error_info,
        )

    def _desktop_snapshot(self, base: StatusSnapshot | None) -> StatusSnapshot | None:
        internal = self._internal_log_snapshot(base)
        if internal:
            return internal
        desktop_log = self._desktop_file_log_snapshot(base)
        if desktop_log:
            return desktop_log
        return None

    def _internal_log_snapshot(self, base: StatusSnapshot | None) -> StatusSnapshot | None:
        db_path = self.codex_home / "logs_2.sqlite"
        if not db_path.exists():
            return None
        cutoff = int(time.time()) - DEFAULT_DESKTOP_EVENT_SECONDS
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.2)
        except sqlite3.Error:
            return None
        try:
            rows = connection.execute(
                """
                select ts, level, target, feedback_log_body
                from logs
                where ts >= ?
                order by ts desc
                limit 200
                """,
                (cutoff,),
            ).fetchall()
        except sqlite3.Error:
            return None
        finally:
            connection.close()

        for ts, level, target, body in rows:
            text = str(body or "")
            target_text = str(target or "")
            level_text = str(level or "")
            if self._is_internal_reconnect_log(target_text, level_text, text, base):
                attempt = self._extract_reconnect_attempt(text)
                detail = "Codex Desktop is reconnecting"
                if attempt:
                    detail += f" (attempt {attempt})"
                error = self._short_error(text)
                if error:
                    detail += f": {error}"
                return StatusSnapshot(
                    state="reconnecting",
                    label="Desktop reconnecting",
                    detail=detail,
                    session_id=base.session_id if base else None,
                    source_file=str(db_path),
                    cwd=base.cwd if base else None,
                    last_event_type=f"{target_text}:{level_text}",
                    last_event_age_seconds=max(0.0, time.time() - float(ts)),
                    updated_at=now_iso(),
                    needs_human=False,
                    recommended_action="Network/proxy path is unstable; wait briefly or switch proxy node.",
                    error_info=text[:500],
                )
            if self._is_internal_human_blocker(target_text, level_text, text, base):
                return StatusSnapshot(
                    state="failed",
                    label="Desktop needs attention",
                    detail=self._short_error(text) or text[:160],
                    session_id=base.session_id if base else None,
                    source_file=str(db_path),
                    cwd=base.cwd if base else None,
                    last_event_type=f"{target_text}:{level_text}",
                    last_event_age_seconds=max(0.0, time.time() - float(ts)),
                    updated_at=now_iso(),
                    needs_human=True,
                    recommended_action="Open Codex and handle login/quota/permission.",
                    error_info=text[:500],
                )
        return None

    def _desktop_file_log_snapshot(self, base: StatusSnapshot | None) -> StatusSnapshot | None:
        log_root = self._desktop_log_root()
        if not log_root or not log_root.exists():
            return None
        files = self._recent_desktop_logs(log_root)
        cutoff = datetime.now(timezone.utc).timestamp() - DEFAULT_DESKTOP_EVENT_SECONDS
        for path in files:
            text = safe_read_recent_text(path)
            for line in reversed(text.splitlines()):
                timestamp = self._timestamp_from_log_line(line)
                if timestamp and timestamp.timestamp() < cutoff:
                    continue
                if self._is_desktop_network_line(line, base):
                    age = (
                        max(0.0, datetime.now(timezone.utc).timestamp() - timestamp.timestamp())
                        if timestamp
                        else max(0.0, time.time() - path.stat().st_mtime)
                    )
                    return StatusSnapshot(
                        state="reconnecting",
                        label="Desktop network error",
                        detail=self._short_error(line) or line[:160],
                        session_id=base.session_id if base else None,
                        source_file=str(path),
                        cwd=base.cwd if base else None,
                        last_event_type="desktop_log:network_error",
                        last_event_age_seconds=age,
                        updated_at=now_iso(),
                        needs_human=False,
                        recommended_action="Network/proxy path is unstable; wait briefly or switch proxy node.",
                        error_info=line[:500],
                    )
        return None

    def _desktop_log_root(self) -> Path | None:
        package_root = (
            os.environ.get("LOCALAPPDATA")
            and Path(os.environ["LOCALAPPDATA"]) / "Packages" / "OpenAI.Codex_2p2nqsd0c76g0"
        )
        if not package_root:
            return None
        return package_root / "LocalCache" / "Local" / "Codex" / "Logs"

    def _recent_desktop_logs(self, log_root: Path) -> list[Path]:
        files: list[Path] = []
        try:
            for path in log_root.rglob("codex-desktop-*.log"):
                try:
                    if time.time() - path.stat().st_mtime <= 24 * 3600:
                        files.append(path)
                except OSError:
                    continue
        except OSError:
            return []
        files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        return files[:20]

    def _timestamp_from_log_line(self, line: str) -> datetime | None:
        if len(line) < 24:
            return None
        return parse_timestamp(line[:24].replace("Z", "+00:00"))

    def _is_internal_reconnect_log(
        self,
        target: str,
        level: str,
        text: str,
        base: StatusSnapshot | None,
    ) -> bool:
        if level.upper() not in {"WARN", "ERROR"}:
            return False
        if base and base.state in {"completed", "failed", "waiting", "idle"}:
            return False
        if not self._log_matches_current_session(base, text):
            return False

        lower = text.lower()
        target_lower = target.lower()
        if any(
            ignored in target_lower
            for ignored in [
                "remote_control::websocket",
                "codex_api::sse::responses",
                "codex_core::stream_events_utils",
            ]
        ):
            return False

        is_relevant_target = any(
            marker in target_lower
            for marker in [
                "codex_client::transport",
                "codex_core::client",
                "codex_core::codex",
                "codex_core::session",
                "codex_core::turn",
            ]
        )
        if not is_relevant_target:
            return False

        return any(
            marker in lower
            for marker in [
                "responsestreamdisconnected",
                "responsestreamconnectionfailed",
                "httpconnectionfailed",
                "stream disconnected",
                "stream failed",
                "net::err_connection",
                "err_connection_timed_out",
                "err_timed_out",
                "connection timed out",
            ]
        )

    def _log_matches_current_session(self, base: StatusSnapshot | None, text: str) -> bool:
        keys = self._session_match_keys(base)
        if not keys:
            return False
        lower = text.lower()
        return any(key.lower() in lower for key in keys)

    def _session_match_keys(self, base: StatusSnapshot | None) -> list[str]:
        if not base or not base.session_id:
            return []
        keys = [base.session_id]
        uuid_match = re.search(
            r"(019[0-9a-f]{5,}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            base.session_id,
            re.IGNORECASE,
        )
        if uuid_match:
            keys.append(uuid_match.group(1))
        return list(dict.fromkeys(keys))

    def _is_internal_human_blocker(
        self,
        target: str,
        level: str,
        text: str,
        base: StatusSnapshot | None,
    ) -> bool:
        if target.lower() == "feedback_tags":
            return False
        if level.upper() not in {"WARN", "ERROR"}:
            return False
        if base and base.state in {"completed", "idle"}:
            return False
        if not self._log_matches_current_session(base, text):
            return False
        lower = text.lower()
        return any(
            marker in lower
            for marker in [
                "unauthorized",
                "usagelimitexceeded",
                "quota",
                "login required",
                "permission denied",
                "approval required",
            ]
        )

    def _is_desktop_network_line(self, line: str, base: StatusSnapshot | None) -> bool:
        if base and base.state in {"completed", "failed", "waiting", "idle"}:
            return False
        if not self._log_matches_current_session(base, line):
            return False
        lower = line.lower()
        return any(
            marker in lower
            for marker in [
                "net::err_connection",
                "err_timed_out",
                "connection_timed_out",
                "sync_failed",
                "failed to send request",
                "failed to parse json response",
            ]
        )

    def _extract_reconnect_attempt(self, text: str) -> str | None:
        match = re.search(r"reconnect_attempt=(\d+)", text)
        return match.group(1) if match else None

    def _short_error(self, text: str) -> str | None:
        for pattern in [
            r"errorMessage=([^ ]+)",
            r"error=([^ ]+.*?)(?: reconnect_attempt=| server_|$)",
            r'"message":"([^"]+)"',
        ]:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip().strip('"')[:180]
        return None

    def _classify(
        self,
        *,
        age: float | None,
        saw_task_complete: bool,
        saw_error: bool,
        error_info: str | None,
        saw_reasoning: bool,
        saw_tool_or_command: bool,
        waiting_reason: str | None,
        last_type: str | None,
        last_payload_type: str | None,
        last_agent_message: str,
    ) -> tuple[str, str, str, bool, str | None]:
        age_text = "unknown age" if age is None else f"{int(age)}s ago"

        if saw_error:
            info = error_info or "Codex reported an error."
            recoverable = any(
                marker in info
                for marker in [
                    "ResponseStreamDisconnected",
                    "ResponseStreamConnectionFailed",
                    "HttpConnectionFailed",
                    "timeout",
                    "disconnected",
                    "connection",
                ]
            )
            if recoverable:
                return (
                    "reconnecting",
                    "Stream or network failure",
                    info,
                    False,
                    "If the turn had already started, send '继续' after reconnecting.",
                )
            human_markers = [
                "Unauthorized",
                "UsageLimitExceeded",
                "quota",
                "login",
                "auth",
                "approval",
            ]
            return (
                "failed",
                "Needs attention",
                info,
                any(marker.lower() in info.lower() for marker in human_markers),
                "Open Codex and handle the reported failure.",
            )

        if waiting_reason:
            return (
                "waiting",
                "Waiting for approval",
                waiting_reason,
                True,
                "Review the requested permission in Codex.",
            )

        if saw_task_complete:
            preview = last_agent_message.strip().replace("\n", " ")
            detail = preview[:120] if preview else f"Last event {age_text}"
            return ("completed", "Completed", detail, False, None)

        if age is not None and age > self.stale_seconds:
            return (
                "stale",
                "Possibly stuck",
                f"No new Codex event for {int(age)}s.",
                False,
                "Check Codex; if it was mid-turn, sending '继续' is usually safe.",
            )

        if saw_tool_or_command:
            return (
                "executing",
                "Executing or editing",
                last_agent_message[:120] or f"Last event {age_text}",
                False,
                None,
            )

        if last_payload_type in {"agent_message"} or (
            last_type == "response_item" and last_payload_type == "message"
        ):
            preview = last_agent_message.strip().replace("\n", " ")
            return (
                "outputting",
                "Outputting",
                preview[:120] if preview else f"Last event {age_text}",
                False,
                None,
            )

        if saw_reasoning or last_payload_type in {"task_started", "user_message"}:
            return ("working", "Thinking", f"Last event {age_text}", False, None)

        return ("working", "Working", f"Last event {age_text}", False, None)

    def _write_status(self, snapshot: StatusSnapshot) -> None:
        tmp = self.status_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(asdict(snapshot), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.status_path)

    def _append_event_if_changed(self, snapshot: StatusSnapshot) -> None:
        key = "|".join(
            [
                snapshot.state,
                snapshot.session_id or "",
                snapshot.last_event_type or "",
                str(int(snapshot.last_event_age_seconds or 0) // 5),
            ]
        )
        if key == self._last_event_key:
            return
        self._last_event_key = key
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(snapshot), ensure_ascii=False) + "\n")

    def _append_action_if_needed(self, snapshot: StatusSnapshot) -> None:
        should_record = snapshot.needs_human or snapshot.state in {
            "reconnecting",
            "stale",
            "failed",
        }
        if not should_record:
            return

        key = "|".join(
            [
                snapshot.state,
                snapshot.session_id or "",
                snapshot.error_info or "",
                snapshot.recommended_action or "",
                str(int(snapshot.last_event_age_seconds or 0) // 30),
            ]
        )
        if key == self._last_action_key:
            return
        self._last_action_key = key

        record = {
            "timestamp": now_iso(),
            "session_id": snapshot.session_id,
            "state": snapshot.state,
            "decision": "no_auto_recovery",
            "reason": (
                "human_required"
                if snapshot.needs_human
                else "mvp_statusbar_is_read_only"
            ),
            "recommended_action": snapshot.recommended_action,
            "error_info": snapshot.error_info,
            "source_file": snapshot.source_file,
        }
        with self.actions_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _external_snapshot(self) -> StatusSnapshot | None:
        snapshots: list[tuple[float, StatusSnapshot]] = []
        for path in [self.appserver_status_path, self.guard_status_path]:
            snapshot = self._snapshot_from_status_file(path)
            if not snapshot:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            snapshots.append((mtime, snapshot))
        if not snapshots:
            return None
        snapshots.sort(key=lambda item: item[0], reverse=True)
        return snapshots[0][1]

    def _snapshot_from_status_file(self, path: Path) -> StatusSnapshot | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        if time.time() - stat.st_mtime > 20:
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None

        state = str(raw.get("state") or "working")
        if state in {"done", "success"}:
            state = "completed"
        label = str(raw.get("label") or raw.get("phase") or "Guarded Codex run")
        detail = str(raw.get("detail") or raw.get("last_event_type") or "")
        updated_at = str(raw.get("updated_at") or now_iso())
        age = None
        parsed_updated_at = parse_timestamp(updated_at)
        if parsed_updated_at:
            age = max(0.0, (datetime.now(timezone.utc) - parsed_updated_at).total_seconds())

        session_id = raw.get("session_id")
        cwd = raw.get("cwd")
        last_event_type = raw.get("last_event_type")
        recommended_action = raw.get("recommended_action")
        error_info = raw.get("error_info")
        return StatusSnapshot(
            state=state,
            label=label,
            detail=detail,
            session_id=session_id if isinstance(session_id, str) else None,
            source_file=str(path),
            cwd=cwd if isinstance(cwd, str) else None,
            last_event_type=last_event_type if isinstance(last_event_type, str) else None,
            last_event_age_seconds=age,
            updated_at=updated_at,
            needs_human=bool(raw.get("needs_human")),
            recommended_action=recommended_action if isinstance(recommended_action, str) else None,
            error_info=error_info if isinstance(error_info, str) else None,
        )


class StatusBarApp:
    def __init__(self, watcher: CodexSessionWatcher, poll_seconds: float) -> None:
        self.watcher = watcher
        self.poll_seconds = poll_seconds
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("+30+30")
        self.root.minsize(440, 68)
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)
        self.root.configure(bg="#0f172a")
        self._drag_start: tuple[int, int] | None = None

        self.shell = tk.Frame(self.root, bg="#0f172a", padx=8, pady=8)
        self.shell.pack(fill="both", expand=True)

        self.card = tk.Frame(self.shell, bg="#f8fafc", padx=10, pady=8)
        self.card.pack(fill="both", expand=True)

        self.dot = tk.Canvas(self.card, width=18, height=18, bg="#f8fafc", highlightthickness=0)
        self.dot.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 8), pady=(1, 0))
        self.dot_id = self.dot.create_oval(2, 2, 16, 16, fill="#334155", outline="")

        self.title_var = tk.StringVar(value="Starting Codex watcher")
        self.detail_var = tk.StringVar(value="Scanning local sessions...")
        self.meta_var = tk.StringVar(value="")

        self.title = tk.Label(
            self.card,
            textvariable=self.title_var,
            bg="#f8fafc",
            fg="#0f172a",
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        )
        self.title.grid(row=0, column=1, sticky="ew")

        self.detail = tk.Label(
            self.card,
            textvariable=self.detail_var,
            bg="#f8fafc",
            fg="#334155",
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.detail.grid(row=1, column=1, sticky="ew", pady=(1, 0))

        self.meta = tk.Label(
            self.card,
            textvariable=self.meta_var,
            bg="#f8fafc",
            fg="#64748b",
            font=("Segoe UI", 8),
            anchor="w",
        )
        self.meta.grid(row=2, column=1, sticky="ew", pady=(3, 0))

        self.refresh_btn = tk.Button(
            self.card,
            text="Refresh",
            command=self.refresh_now,
            relief="flat",
            bg="#e2e8f0",
            fg="#0f172a",
            activebackground="#cbd5e1",
            font=("Segoe UI", 8),
            padx=8,
        )
        self.refresh_btn.grid(row=0, column=2, rowspan=2, padx=(10, 4), sticky="e")

        self.log_btn = tk.Button(
            self.card,
            text="Logs",
            command=self.open_logs,
            relief="flat",
            bg="#e2e8f0",
            fg="#0f172a",
            activebackground="#cbd5e1",
            font=("Segoe UI", 8),
            padx=8,
        )
        self.log_btn.grid(row=0, column=3, rowspan=2, padx=(0, 4), sticky="e")

        self.close_btn = tk.Button(
            self.card,
            text="x",
            command=self.root.destroy,
            relief="flat",
            bg="#f1f5f9",
            fg="#475569",
            activebackground="#fecaca",
            font=("Segoe UI", 9, "bold"),
            width=3,
        )
        self.close_btn.grid(row=0, column=4, rowspan=2, sticky="ne")

        self.card.grid_columnconfigure(1, weight=1, minsize=250)
        for widget in [self.shell, self.card, self.title, self.detail, self.meta, self.dot]:
            widget.bind("<ButtonPress-1>", self.start_drag)
            widget.bind("<B1-Motion>", self.on_drag)

        self.refresh_now()
        self.root.after(int(self.poll_seconds * 1000), self.tick)

    def start_drag(self, event: tk.Event[Any]) -> None:
        self._drag_start = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def on_drag(self, event: tk.Event[Any]) -> None:
        if not self._drag_start:
            return
        dx, dy = self._drag_start
        self.root.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def tick(self) -> None:
        self.refresh_now()
        self.root.after(int(self.poll_seconds * 1000), self.tick)

    def refresh_now(self) -> None:
        def worker() -> None:
            try:
                snapshot = self.watcher.scan()
            except Exception as exc:  # defensive UI boundary
                snapshot = StatusSnapshot(
                    state="failed",
                    label="Watcher error",
                    detail=str(exc),
                    session_id=None,
                    source_file=None,
                    cwd=None,
                    last_event_type=None,
                    last_event_age_seconds=None,
                    updated_at=now_iso(),
                    needs_human=True,
                    recommended_action="Open logs or restart the status bar.",
                )
            self.root.after(0, lambda: self.render(snapshot))

        threading.Thread(target=worker, daemon=True).start()

    def render(self, snapshot: StatusSnapshot) -> None:
        color, bg = PALETTE.get(snapshot.state, PALETTE["idle"])
        self.shell.configure(bg=color)
        self.card.configure(bg=bg)
        self.dot.configure(bg=bg)
        self.dot.itemconfigure(self.dot_id, fill=color)
        for label in [self.title, self.detail, self.meta]:
            label.configure(bg=bg)

        prefix = "!" if snapshot.needs_human else ""
        self.title_var.set(f"{prefix}{snapshot.label}")
        self.detail_var.set(snapshot.detail or "")

        age = snapshot.last_event_age_seconds
        age_text = "-" if age is None else f"{int(age)}s"
        session = (snapshot.session_id or "-")[:18]
        self.meta_var.set(f"{snapshot.state} | age {age_text} | session {session}")

        if snapshot.needs_human:
            self.root.bell()

    def open_logs(self) -> None:
        path = self.watcher.state_dir
        try:
            subprocess.Popen(["explorer", str(path)])
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not open log folder:\n{exc}")

    def run(self) -> None:
        self.root.mainloop()


def default_codex_home() -> Path:
    if os.environ.get("CODEX_HOME"):
        return Path(os.environ["CODEX_HOME"]).expanduser()
    return Path.home() / ".codex"


def default_state_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "CodexStatusbar"
    return Path.cwd() / ".codex-statusbar"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Always-on-top Codex status bar.")
    parser.add_argument("--codex-home", type=Path, default=default_codex_home())
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Scan once, print status JSON, and exit without opening the UI.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    watcher = CodexSessionWatcher(
        codex_home=args.codex_home.expanduser(),
        state_dir=args.state_dir.expanduser(),
        stale_seconds=args.stale_seconds,
    )
    if args.once:
        snapshot = watcher.scan()
        print(json.dumps(asdict(snapshot), ensure_ascii=False, indent=2))
        return 0

    app = StatusBarApp(watcher, poll_seconds=max(0.5, args.poll_seconds))
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
