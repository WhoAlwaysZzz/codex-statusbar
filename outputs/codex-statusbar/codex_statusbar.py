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
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox
from typing import Any

try:
    import win32con
    import win32gui
except ImportError:  # pragma: no cover - non-Windows fallback
    win32con = None
    win32gui = None


APP_NAME = "Codex Statusbar"
FULL_WINDOW_WIDTH = 760
FULL_WINDOW_MIN_HEIGHT = 132
FULL_WINDOW_ROW_HEIGHT = 36
FULL_WINDOW_MAX_HEIGHT = 384
FULL_WINDOW_GEOMETRY = f"{FULL_WINDOW_WIDTH}x{FULL_WINDOW_MIN_HEIGHT}"
MINI_WINDOW_GEOMETRY = "230x42"
DEFAULT_STALE_SECONDS = 300
DEFAULT_POLL_SECONDS = 2.0
DEFAULT_DESKTOP_EVENT_SECONDS = 180
DEFAULT_COMPLETED_KEEP_SECONDS = 180
DEFAULT_INACTIVE_KEEP_SECONDS = 3600
MAX_FILES = 60
MAX_FILE_BYTES = 1_500_000
MAX_VISIBLE_SESSIONS = 8
MAX_DISPLAY_CHARS = 20
AUTOSTART_FILE_NAME = "codex-statusbar.cmd"
CARD_BG = "#f8fafc"
TEXT_FG = "#0f172a"
MUTED_FG = "#64748b"
DETAIL_FG = "#475569"
BUTTON_BG = "#e2e8f0"
BUTTON_HOVER_BG = "#cbd5e1"
BUTTON_SUBTLE_BG = "#f1f5f9"
BUTTON_SUBTLE_HOVER_BG = "#dbeafe"
BUTTON_DANGER_HOVER_BG = "#fecaca"
HEADER_FONT = ("Segoe UI", 10, "bold")
BODY_FONT = ("Segoe UI", 8)
ROW_TITLE_FONT = ("Segoe UI", 9, "bold")
ICON_FONT = ("Segoe UI", 9, "bold")


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
    "idle": ("#334155", CARD_BG),
}

MINI_STATE_LABELS = {
    "working": "Working",
    "outputting": "Output",
    "executing": "Command",
    "waiting": "Waiting",
    "recovering": "Recovering",
    "reconnecting": "Reconnect",
    "stale": "Stale",
    "completed": "Done",
    "failed": "Failed",
    "idle": "Idle",
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


@dataclass
class StatusBoard:
    primary: StatusSnapshot
    snapshots: list[StatusSnapshot]
    updated_at: str


def full_window_geometry_for_count(session_count: int) -> str:
    visible_count = max(1, min(session_count, MAX_VISIBLE_SESSIONS))
    height = FULL_WINDOW_MIN_HEIGHT + ((visible_count - 1) * FULL_WINDOW_ROW_HEIGHT)
    height = min(height, FULL_WINDOW_MAX_HEIGHT)
    return f"{FULL_WINDOW_WIDTH}x{height}"


def mini_header_text(
    state: str,
    label: str,
    session_count: int,
    *,
    needs_human: bool = False,
) -> str:
    if needs_human:
        summary = "Attention"
    else:
        summary = MINI_STATE_LABELS.get(state) or label or "Codex"
    return f"{summary} ({max(0, session_count)})"


def always_on_top_label(enabled: bool) -> str:
    return "Disable always on top" if enabled else "Enable always on top"


def clamp_window_position(
    x: int,
    y: int,
    screen_width: int,
    screen_height: int,
    *,
    window_width: int = FULL_WINDOW_WIDTH,
    window_height: int = FULL_WINDOW_MIN_HEIGHT,
    margin: int = 16,
) -> tuple[int, int]:
    if screen_width <= 0 or screen_height <= 0:
        return 30, 30
    max_x = max(margin, screen_width - min(window_width, screen_width) - margin)
    max_y = max(margin, screen_height - min(window_height, screen_height) - margin)
    return (
        min(max(x, margin), max_x),
        min(max(y, margin), max_y),
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        error_access_denied = 5
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ctypes.set_last_error(0)
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return ctypes.get_last_error() == error_access_denied
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def windows_startup_dir(appdata: str | None = None) -> Path | None:
    root = appdata if appdata is not None else os.environ.get("APPDATA")
    if not root:
        return None
    return Path(root) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def autostart_file_path(startup_dir: Path | None = None) -> Path | None:
    folder = startup_dir if startup_dir is not None else windows_startup_dir()
    if folder is None:
        return None
    return folder / AUTOSTART_FILE_NAME


def _cmd_quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def statusbar_launch_command() -> list[str]:
    script_dir = Path(__file__).resolve().parent
    bundled_launcher = script_dir / "codex-stat.exe"
    if bundled_launcher.exists():
        return [str(bundled_launcher)]
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve())]


def render_autostart_cmd(command: list[str] | None = None) -> str:
    parts = command or statusbar_launch_command()
    quoted = " ".join(_cmd_quote(part) for part in parts)
    return "\n".join(
        [
            "@echo off",
            "chcp 65001 >nul",
            "rem Created by Codex Statusbar. Delete this file to disable autostart.",
            f"start \"\" /min {quoted}",
            "",
        ]
    )


def enable_autostart(
    startup_dir: Path | None = None,
    command: list[str] | None = None,
) -> Path:
    path = autostart_file_path(startup_dir)
    if path is None:
        raise RuntimeError("Windows Startup folder was not found.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(render_autostart_cmd(command), encoding="utf-8")
    tmp.replace(path)
    return path


def disable_autostart(startup_dir: Path | None = None) -> bool:
    path = autostart_file_path(startup_dir)
    if path is None:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        raise
    return True


def autostart_enabled(startup_dir: Path | None = None) -> bool:
    path = autostart_file_path(startup_dir)
    return bool(path and path.exists())


@dataclass
class StatusbarInstanceGuard:
    state_dir: Path
    acquired: bool
    owner_pid: int | None = None
    owns_pid_file: bool = True

    @property
    def pid_file(self) -> Path:
        return self.state_dir / "statusbar.pid"

    @property
    def control_file(self) -> Path:
        return self.state_dir / "statusbar-control.json"

    @classmethod
    def acquire(cls, state_dir: Path, *, allow_multiple: bool = False) -> "StatusbarInstanceGuard":
        guard = cls(state_dir=state_dir, acquired=True)
        if allow_multiple:
            return cls(state_dir=state_dir, acquired=True, owns_pid_file=False)
        current_pid = os.getpid()
        existing = _read_json_object(guard.pid_file)
        owner_pid = existing.get("pid")
        if isinstance(owner_pid, int) and owner_pid != current_pid and process_is_alive(owner_pid):
            existing_guard = cls(
                state_dir=state_dir,
                acquired=False,
                owner_pid=owner_pid,
                owns_pid_file=False,
            )
            existing_guard.request_show()
            return existing_guard
        guard.write_pid()
        return guard

    def write_pid(self) -> None:
        if not self.acquired or not self.owns_pid_file:
            return
        _write_json_atomic(
            self.pid_file,
            {
                "pid": os.getpid(),
                "started_at": now_iso(),
            },
        )

    def request_show(self) -> None:
        _write_json_atomic(
            self.control_file,
            {
                "action": "show",
                "requested_at": now_iso(),
                "requester_pid": os.getpid(),
            },
        )

    def release(self) -> None:
        if not self.acquired or not self.owns_pid_file:
            return
        current = _read_json_object(self.pid_file)
        if current.get("pid") != os.getpid():
            return
        try:
            self.pid_file.unlink()
        except OSError:
            pass


class WindowsTrayIcon:
    def __init__(self, app: "StatusBarApp") -> None:
        self.app = app
        self.available = win32gui is not None and win32con is not None
        self.hwnd: int | None = None
        self.message_id = win32con.WM_USER + 20 if win32con else 0
        self.class_name = f"CodexStatusbarTray-{uuid.uuid4()}"
        self.icon_handle: int | None = None

    def install(self) -> None:
        if not self.available:
            return
        assert win32gui is not None and win32con is not None
        message_map = {
            self.message_id: self._on_notify,
            win32con.WM_DESTROY: self._on_destroy,
            win32con.WM_COMMAND: self._on_command,
        }
        wnd_class = win32gui.WNDCLASS()
        wnd_class.hInstance = win32gui.GetModuleHandle(None)
        wnd_class.lpszClassName = self.class_name
        wnd_class.lpfnWndProc = message_map
        try:
            win32gui.RegisterClass(wnd_class)
        except win32gui.error:
            pass
        self.hwnd = win32gui.CreateWindow(
            self.class_name,
            APP_NAME,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            wnd_class.hInstance,
            None,
        )
        self.icon_handle = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        self.show()

    def show(self) -> None:
        if not self.available or not self.hwnd:
            return
        assert win32gui is not None
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        nid = (
            self.hwnd,
            0,
            flags,
            self.message_id,
            self.icon_handle,
            APP_NAME,
        )
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
        except win32gui.error:
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, nid)

    def remove(self) -> None:
        if not self.available or not self.hwnd:
            return
        assert win32gui is not None
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
        except win32gui.error:
            pass

    def pump(self) -> None:
        if not self.available:
            return
        assert win32gui is not None
        win32gui.PumpWaitingMessages()

    def _on_notify(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if win32con is None:
            return 0
        if lparam in {
            win32con.WM_LBUTTONUP,
            win32con.WM_LBUTTONDBLCLK,
        }:
            self.app.show_from_tray()
        elif lparam == win32con.WM_RBUTTONUP:
            self._show_menu()
        return 0

    def _show_menu(self) -> None:
        if win32gui is None or win32con is None or not self.hwnd:
            return
        menu = win32gui.CreatePopupMenu()
        try:
            mode_label = "Full" if self.app.mini_mode else "Mini"
            win32gui.AppendMenu(menu, win32con.MF_STRING, 1001, "Show")
            win32gui.AppendMenu(menu, win32con.MF_STRING, 1002, mode_label)
            win32gui.AppendMenu(menu, win32con.MF_STRING, 1004, "Refresh")
            win32gui.AppendMenu(menu, win32con.MF_STRING, 1005, "Logs")
            win32gui.AppendMenu(menu, win32con.MF_STRING, 1008, "Reset window position")
            win32gui.AppendMenu(menu, win32con.MF_STRING, 1007, always_on_top_label(self.app.always_on_top))
            win32gui.AppendMenu(menu, win32con.MF_STRING, 1006, self.app.autostart_label())
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(menu, win32con.MF_STRING, 1003, "Exit")
            pos = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(self.hwnd)
            win32gui.TrackPopupMenu(
                menu,
                win32con.TPM_LEFTALIGN,
                pos[0],
                pos[1],
                0,
                self.hwnd,
                None,
            )
            win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
        finally:
            win32gui.DestroyMenu(menu)

    def _on_command(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        command_id = wparam & 0xFFFF
        if command_id == 1001:
            self.app.show_from_tray()
        elif command_id == 1002:
            self.app.show_from_tray()
            self.app.toggle_mini_mode()
        elif command_id == 1003:
            self.app.exit_app()
        elif command_id == 1004:
            self.app.show_from_tray()
            self.app.refresh_now()
        elif command_id == 1005:
            self.app.show_from_tray()
            self.app.open_logs()
        elif command_id == 1008:
            self.app.show_from_tray()
            self.app.reset_window_position()
        elif command_id == 1006:
            self.app.toggle_autostart()
        elif command_id == 1007:
            self.app.toggle_always_on_top()
        return 0

    def _on_destroy(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        self.remove()
        return 0


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
        codex_home: Path | list[Path],
        state_dir: Path,
        stale_seconds: int = DEFAULT_STALE_SECONDS,
    ) -> None:
        self.codex_homes = codex_home if isinstance(codex_home, list) else [codex_home]
        self.codex_homes = [home.expanduser() for home in self.codex_homes]
        self.codex_home = self.codex_homes[0]
        self.sessions_dir = self.codex_home / "sessions"
        self.state_dir = state_dir
        self.status_path = state_dir / "status.json"
        self.status_all_path = state_dir / "status_all.json"
        self.appserver_status_path = state_dir / "appserver_status.json"
        self.guard_status_path = state_dir / "guard_status.json"
        self.watchdog_status_path = state_dir / "watchdog_status.json"
        self.events_path = state_dir / "events.jsonl"
        self.actions_path = state_dir / "actions.jsonl"
        self.stale_seconds = stale_seconds
        self._last_event_key: str | None = None
        self._last_action_key: str | None = None
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def scan(self) -> StatusSnapshot:
        return self.scan_all().primary

    def scan_all(self) -> StatusBoard:
        external_candidates = self._external_snapshots()
        files = self._recent_session_files()
        if external_candidates and not files:
            return self._finalize_board(external_candidates)
        if not files:
            snapshot = self._desktop_snapshot(None) or StatusSnapshot(
                state="idle",
                label="No recent Codex session",
                detail=f"Watching {self._watching_detail()}",
                session_id=None,
                source_file=None,
                cwd=None,
                last_event_type=None,
                last_event_age_seconds=None,
                updated_at=now_iso(),
                recommended_action="Start a Codex task, then this bar will follow it.",
            )
            return self._finalize_board([snapshot])

        candidates: list[StatusSnapshot] = []
        candidates.extend(external_candidates)
        for path in files:
            snapshot = self._snapshot_for_file(path)
            if snapshot:
                desktop_snapshot = self._desktop_snapshot(snapshot)
                if desktop_snapshot:
                    snapshot = desktop_snapshot
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
            return self._finalize_board([snapshot])

        return self._finalize_board(candidates)

    def _finalize_board(self, snapshots: list[StatusSnapshot]) -> StatusBoard:
        visible = self._visible_snapshots(snapshots)
        primary = self._primary_snapshot(visible)
        board = StatusBoard(primary=primary, snapshots=visible, updated_at=now_iso())
        self._write_status_board(board)
        self._append_event_if_changed(primary)
        for snapshot in visible:
            self._append_action_if_needed(snapshot)
        return board

    def _visible_snapshots(self, snapshots: list[StatusSnapshot]) -> list[StatusSnapshot]:
        unique: dict[str, StatusSnapshot] = {}
        for snapshot in snapshots:
            key = snapshot.session_id or snapshot.source_file or snapshot.label
            existing = unique.get(key)
            if existing is None or self._sort_key(snapshot) < self._sort_key(existing):
                unique[key] = snapshot

        all_items = list(unique.values())
        active = [item for item in all_items if self._should_show_active(item)]
        recent_completed = [
            item
            for item in all_items
            if item.state == "completed"
            and (
                item.last_event_age_seconds is None
                or item.last_event_age_seconds <= DEFAULT_COMPLETED_KEEP_SECONDS
            )
        ]
        visible = active + recent_completed
        if not visible:
            completed_items = [item for item in all_items if item.state == "completed"]
            if completed_items:
                visible = [min(completed_items, key=self._sort_key)]
        if not visible:
            return []
        visible.sort(key=self._sort_key)
        return visible[:MAX_VISIBLE_SESSIONS]

    def _should_show_active(self, snapshot: StatusSnapshot) -> bool:
        if snapshot.state in {"completed", "idle"}:
            return False
        if snapshot.needs_human:
            return True
        age = snapshot.last_event_age_seconds
        return age is None or age <= DEFAULT_INACTIVE_KEEP_SECONDS

    def _primary_snapshot(self, snapshots: list[StatusSnapshot]) -> StatusSnapshot:
        if not snapshots:
            return StatusSnapshot(
                state="idle",
                label="No recent Codex session",
                detail=f"Watching {self._watching_detail()}",
                session_id=None,
                source_file=None,
                cwd=None,
                last_event_type=None,
                last_event_age_seconds=None,
                updated_at=now_iso(),
            )
        return min(snapshots, key=self._sort_key)

    def _sort_key(self, snapshot: StatusSnapshot) -> tuple[int, float]:
        priority = {
            "waiting": 0,
            "failed": 1,
            "reconnecting": 2,
            "recovering": 3,
            "stale": 4,
            "executing": 5,
            "outputting": 6,
            "working": 7,
            "completed": 8,
            "idle": 9,
        }.get(snapshot.state, 7)
        if snapshot.needs_human:
            priority = -1
        age = snapshot.last_event_age_seconds
        return (priority, age if age is not None else 10**9)

    def _recent_session_files(self) -> list[Path]:
        files: list[Path] = []
        for codex_home in self.codex_homes:
            sessions_dir = codex_home / "sessions"
            if not sessions_dir.exists():
                continue
            try:
                iterator = sessions_dir.rglob("*.jsonl")
                for path in iterator:
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    age = time.time() - stat.st_mtime
                    if age <= 48 * 3600:
                        files.append(path)
            except OSError:
                continue
        files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        return files[:MAX_FILES]

    def _watching_detail(self) -> str:
        if len(self.codex_homes) == 1:
            return str(self.codex_homes[0] / "sessions")
        return f"{len(self.codex_homes)} Codex homes"

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
        saw_turn_aborted = False
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
                elif payload_type == "turn_aborted":
                    saw_turn_aborted = True
                    saw_task_complete = True
                    reason = payload.get("reason")
                    if isinstance(reason, str) and reason:
                        last_agent_message = f"Interrupted: {reason}"
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
            saw_turn_aborted=saw_turn_aborted,
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
        for codex_home in self.codex_homes:
            snapshot = self._internal_log_snapshot_for_home(codex_home, base)
            if snapshot:
                return snapshot
        return None

    def _internal_log_snapshot_for_home(
        self,
        codex_home: Path,
        base: StatusSnapshot | None,
    ) -> StatusSnapshot | None:
        db_path = codex_home / "logs_2.sqlite"
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
        saw_turn_aborted: bool,
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
            if saw_turn_aborted:
                preview = last_agent_message.strip().replace("\n", " ")
                return (
                    "completed",
                    "Interrupted",
                    preview[:120] if preview else f"Last event {age_text}",
                    False,
                    None,
                )
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

    def _write_status_board(self, board: StatusBoard) -> None:
        self._write_status(board.primary)
        tmp = self.status_all_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(asdict(board), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.status_all_path)

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
        snapshots = self._external_snapshots()
        if not snapshots:
            return None
        return self._primary_snapshot(snapshots)

    def _external_snapshots(self) -> list[StatusSnapshot]:
        snapshots: list[tuple[float, StatusSnapshot]] = []
        for path in [
            self.appserver_status_path,
            self.guard_status_path,
            self.watchdog_status_path,
        ]:
            snapshot = self._snapshot_from_status_file(path)
            if not snapshot:
                continue
            if path == self.watchdog_status_path and not (
                snapshot.needs_human
                or snapshot.state in {"recovering", "reconnecting", "failed", "waiting"}
            ):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            snapshots.append((mtime, snapshot))
        if not snapshots:
            return []
        snapshots.sort(key=lambda item: item[0], reverse=True)
        return [snapshot for _, snapshot in snapshots]

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
    def __init__(
        self,
        watcher: CodexSessionWatcher,
        poll_seconds: float,
        instance_guard: StatusbarInstanceGuard | None = None,
    ) -> None:
        self.watcher = watcher
        self.poll_seconds = poll_seconds
        self.instance_guard = instance_guard
        self.ui_settings_path = watcher.state_dir / "ui_settings.json"
        self.ui_settings = self._load_ui_settings()
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry(self._initial_position())
        self.root.minsize(720, 92)
        self.always_on_top = self._load_bool_setting("always_on_top", default=True)
        self.root.attributes("-topmost", self.always_on_top)
        self.root.overrideredirect(True)
        self.root.configure(bg=TEXT_FG)
        self._drag_start: tuple[int, int] | None = None
        self._last_render_signature: tuple[Any, ...] | None = None
        self.mini_mode = bool(self.ui_settings.get("mini_mode"))
        self.last_board: StatusBoard | None = None
        self.context_menu = tk.Menu(self.root, tearoff=0)

        self.shell = tk.Frame(self.root, bg=TEXT_FG, padx=8, pady=8)
        self.shell.pack(fill="both", expand=True)

        self.card = tk.Frame(self.shell, bg=CARD_BG, padx=10, pady=8)
        self.card.pack(fill="both", expand=True)

        self.header_var = tk.StringVar(value="Starting Codex watcher")
        self.header = tk.Label(
            self.card,
            textvariable=self.header_var,
            bg=CARD_BG,
            fg=TEXT_FG,
            font=HEADER_FONT,
            anchor="w",
        )
        self.header.grid(row=0, column=0, sticky="ew")

        self.summary_var = tk.StringVar(value="Scanning local sessions...")
        self.summary = tk.Label(
            self.card,
            textvariable=self.summary_var,
            bg=CARD_BG,
            fg=MUTED_FG,
            font=BODY_FONT,
            anchor="w",
        )
        self.summary.grid(row=1, column=0, sticky="ew", pady=(1, 5))

        self.task_frame = tk.Frame(self.card, bg=CARD_BG)
        self.task_frame.grid(row=2, column=0, columnspan=6, sticky="ew")
        self.task_rows: list[tk.Widget] = []

        self.refresh_btn = self._make_button("Refresh", self.refresh_now)
        self.refresh_btn.grid(row=0, column=1, rowspan=2, padx=(10, 4), sticky="e")

        self.log_btn = self._make_button("Logs", self.open_logs)
        self.log_btn.grid(row=0, column=2, rowspan=2, padx=(0, 4), sticky="e")

        self.mini_btn = self._make_button(
            "Full" if self.mini_mode else "Mini",
            self.toggle_mini_mode,
        )
        self.mini_btn.grid(row=0, column=3, rowspan=2, padx=(0, 4), sticky="e")

        self.tray_btn = self._make_button(
            "-",
            self.minimize_to_tray,
            subtle=True,
            width=3,
            font=ICON_FONT,
        )
        self.tray_btn.grid(row=0, column=4, rowspan=2, padx=(0, 4), sticky="e")

        self.close_btn = self._make_button(
            "X",
            self.handle_window_close,
            subtle=True,
            danger=True,
            width=3,
            font=ICON_FONT,
        )
        self.close_btn.grid(row=0, column=5, rowspan=2, sticky="ne")

        self.card.grid_columnconfigure(0, weight=1, minsize=380)
        for column in range(1, 6):
            self.card.grid_columnconfigure(column, weight=0)
        self.task_frame.grid_columnconfigure(0, weight=1)
        for widget in [self.shell, self.card, self.header, self.summary, self.task_frame]:
            self._bind_window_controls(widget)

        self.tray = WindowsTrayIcon(self)
        self.tray.install()
        self.root.protocol("WM_DELETE_WINDOW", self.handle_window_close)
        self.refresh_now()
        self.root.after(int(self.poll_seconds * 1000), self.tick)

    def _make_button(
        self,
        text: str,
        command: Any,
        *,
        subtle: bool = False,
        danger: bool = False,
        width: int | None = None,
        font: tuple[str, int] | tuple[str, int, str] = BODY_FONT,
    ) -> tk.Button:
        bg = BUTTON_SUBTLE_BG if subtle else BUTTON_BG
        hover_bg = BUTTON_DANGER_HOVER_BG if danger else (
            BUTTON_SUBTLE_HOVER_BG if subtle else BUTTON_HOVER_BG
        )
        button = tk.Button(
            self.card,
            text=text,
            command=command,
            relief="flat",
            bd=0,
            highlightthickness=0,
            bg=bg,
            fg=TEXT_FG if not subtle else DETAIL_FG,
            activebackground=hover_bg,
            activeforeground=TEXT_FG,
            font=font,
            padx=10 if width is None else 0,
            pady=3,
            width=width or 0,
            cursor="hand2",
            takefocus=0,
        )
        button.bind("<Enter>", lambda _event: button.configure(bg=hover_bg))
        button.bind("<Leave>", lambda _event: button.configure(bg=bg))
        return button

    def start_drag(self, event: tk.Event[Any]) -> None:
        self._drag_start = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def on_drag(self, event: tk.Event[Any]) -> None:
        if not self._drag_start:
            return
        dx, dy = self._drag_start
        self.root.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")
        self._save_ui_settings()

    def _bind_window_controls(self, widget: tk.Widget) -> None:
        widget.bind("<ButtonPress-1>", self.start_drag)
        widget.bind("<B1-Motion>", self.on_drag)
        widget.bind("<Button-3>", self.show_context_menu)

    def show_context_menu(self, event: tk.Event[Any]) -> None:
        self.context_menu.delete(0, "end")
        mode_label = "Full mode" if self.mini_mode else "Mini mode"
        self.context_menu.add_command(label="Refresh", command=self.refresh_now)
        self.context_menu.add_command(label=mode_label, command=self.toggle_mini_mode)
        self.context_menu.add_command(label="Hide to tray", command=self.minimize_to_tray)
        self.context_menu.add_command(label="Reset window position", command=self.reset_window_position)
        self.context_menu.add_command(label="Open logs", command=self.open_logs)
        self.context_menu.add_command(label=always_on_top_label(self.always_on_top), command=self.toggle_always_on_top)
        self.context_menu.add_command(label=self.autostart_label(), command=self.toggle_autostart)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Exit", command=self.exit_app)
        self.context_menu.tk_popup(event.x_root, event.y_root)
        self.context_menu.grab_release()

    def tick(self) -> None:
        self.tray.pump()
        self._process_control_request()
        self.refresh_now()
        self.root.after(int(self.poll_seconds * 1000), self.tick)

    def _process_control_request(self) -> None:
        if (
            not self.instance_guard
            or not self.instance_guard.acquired
            or not self.instance_guard.owns_pid_file
        ):
            return
        payload = _read_json_object(self.instance_guard.control_file)
        if payload.get("action") != "show":
            return
        try:
            self.instance_guard.control_file.unlink()
        except OSError:
            pass
        self.show_from_tray()

    def refresh_now(self) -> None:
        def worker() -> None:
            try:
                board = self.watcher.scan_all()
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
                board = StatusBoard(
                    primary=snapshot,
                    snapshots=[snapshot],
                    updated_at=now_iso(),
                )
            self.root.after(0, lambda: self.render_board(board))

        threading.Thread(target=worker, daemon=True).start()

    def render_board(self, board: StatusBoard) -> None:
        signature = self._render_signature(board)
        if signature == self._last_render_signature:
            return
        self._last_render_signature = signature
        self.last_board = board

        color, bg = PALETTE.get(board.primary.state, PALETTE["idle"])
        self.shell.configure(bg=color)
        self.card.configure(bg=bg)
        self.task_frame.configure(bg=bg)
        for label in [self.header, self.summary]:
            label.configure(bg=bg)

        if self.mini_mode:
            self._render_mini(board, bg)
            return

        count = len(board.snapshots)
        self._show_full_widgets(count)
        attention = sum(1 for item in board.snapshots if item.needs_human)
        self.header_var.set(f"Codex sessions ({count})")
        summary = self._clip_text(board.primary.label)
        if attention:
            summary = f"{attention} need attention | {summary}"
        self.summary_var.set(summary)

        self._clear_task_rows()
        for index, snapshot in enumerate(board.snapshots):
            self._add_task_row(index, snapshot, bg)

        if board.primary.needs_human:
            self.root.bell()

    def _render_signature(self, board: StatusBoard) -> tuple[Any, ...]:
        rows = tuple(
            (
                item.state,
                item.label,
                self._task_name(item),
                self._clip_text(item.detail or item.last_event_type or ""),
                item.needs_human,
            )
            for item in board.snapshots
        )
        return (self.mini_mode, board.primary.state, board.primary.label, rows)

    def _render_mini(self, board: StatusBoard, bg: str) -> None:
        self._clear_task_rows()
        self.task_frame.grid_remove()
        self.summary.grid_remove()
        self.refresh_btn.grid_remove()
        self.log_btn.grid_remove()
        self.header.grid()
        count = len(board.snapshots)
        self.card.grid_columnconfigure(0, minsize=72)
        self.header.configure(anchor="center")
        self.header_var.set(
            mini_header_text(
                board.primary.state,
                board.primary.label,
                count,
                needs_human=board.primary.needs_human,
            )
        )
        self.root.minsize(230, 42)
        self.root.geometry(MINI_WINDOW_GEOMETRY)

    def _show_full_widgets(self, session_count: int) -> None:
        self.header.grid()
        self.header.configure(anchor="w")
        self.summary.grid()
        self.task_frame.grid()
        self.refresh_btn.grid()
        self.log_btn.grid()
        self.card.grid_columnconfigure(0, minsize=380)
        self.root.minsize(720, 92)
        self.root.update_idletasks()
        self.root.geometry(full_window_geometry_for_count(session_count))

    def _clear_task_rows(self) -> None:
        for row in self.task_rows:
            row.destroy()
        self.task_rows = []

    def _add_task_row(self, index: int, snapshot: StatusSnapshot, bg: str) -> None:
        color, _ = PALETTE.get(snapshot.state, PALETTE["idle"])
        row = tk.Frame(self.task_frame, bg=bg, pady=2)
        row.grid(row=index, column=0, sticky="ew")
        row.grid_columnconfigure(1, weight=1)

        dot = tk.Canvas(row, width=14, height=14, bg=bg, highlightthickness=0)
        dot.create_oval(2, 2, 12, 12, fill=color, outline="")
        dot.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 7), pady=(3, 0))

        title = tk.Label(
            row,
            text=self._task_title(snapshot),
            bg=bg,
            fg=TEXT_FG,
            font=ROW_TITLE_FONT,
            anchor="w",
        )
        title.grid(row=0, column=1, sticky="ew")

        detail = tk.Label(
            row,
            text=self._task_detail(snapshot),
            bg=bg,
            fg=DETAIL_FG,
            font=BODY_FONT,
            anchor="w",
        )
        detail.grid(row=1, column=1, sticky="ew")

        meta = tk.Label(
            row,
            text=self._task_meta(snapshot),
            bg=bg,
            fg=MUTED_FG,
            font=BODY_FONT,
            anchor="e",
        )
        meta.grid(row=0, column=2, rowspan=2, sticky="e", padx=(10, 0))

        for widget in [row, dot, title, detail, meta]:
            self._bind_window_controls(widget)
        self.task_rows.append(row)

    def _task_title(self, snapshot: StatusSnapshot) -> str:
        prefix = "!" if snapshot.needs_human else ""
        return f"{prefix}{self._clip_text(snapshot.label)} | {self._task_name(snapshot)}"

    def _task_detail(self, snapshot: StatusSnapshot) -> str:
        detail = (snapshot.detail or snapshot.last_event_type or "").replace("\n", " ")
        return self._clip_text(detail)

    def _task_meta(self, snapshot: StatusSnapshot) -> str:
        age = snapshot.last_event_age_seconds
        age_text = "-" if age is None else f"{int(age)}s"
        return f"{snapshot.state} | {age_text}"

    def _short_session(self, session_id: str | None) -> str:
        if not session_id:
            return "-"
        return session_id[-12:]

    def _task_name(self, snapshot: StatusSnapshot) -> str:
        project = self._project_name(snapshot)
        session = self._short_session(snapshot.session_id)
        return f"{project}/{session}"

    def _project_name(self, snapshot: StatusSnapshot) -> str:
        if snapshot.cwd:
            name = Path(snapshot.cwd).name
            if name:
                return self._clip_text(name)
        if snapshot.source_file:
            parent = Path(snapshot.source_file).parent.name
            if parent:
                return self._clip_text(parent)
        return "Codex"

    def _clip_text(self, text: str, limit: int = MAX_DISPLAY_CHARS) -> str:
        clean = " ".join(str(text).split())
        if len(clean) <= limit:
            return clean
        return clean[:limit] + "..."

    def toggle_mini_mode(self) -> None:
        self.set_mini_mode(not self.mini_mode)

    def set_mini_mode(self, enabled: bool) -> None:
        if self.mini_mode == enabled:
            return
        self.mini_mode = enabled
        self.mini_btn.configure(text="Full" if enabled else "Mini")
        self._last_render_signature = None
        self._save_ui_settings()
        if self.last_board:
            self.render_board(self.last_board)

    def minimize_to_tray(self) -> None:
        self.root.withdraw()

    def handle_window_close(self) -> None:
        self.minimize_to_tray()

    def show_from_tray(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", self.always_on_top)

    def reset_window_position(self) -> None:
        self.root.geometry("+30+30")
        self.root.update_idletasks()
        self._save_ui_settings()

    def toggle_always_on_top(self) -> None:
        self.set_always_on_top(not self.always_on_top)

    def set_always_on_top(self, enabled: bool) -> None:
        self.always_on_top = enabled
        self.root.attributes("-topmost", enabled)
        self._save_ui_settings()

    def exit_app(self) -> None:
        self._save_ui_settings()
        if self.instance_guard:
            self.instance_guard.release()
            self.instance_guard = None
        self.tray.remove()
        self.root.destroy()

    def _initial_position(self) -> str:
        x = self.ui_settings.get("x")
        y = self.ui_settings.get("y")
        if isinstance(x, int) and isinstance(y, int):
            safe_x, safe_y = clamp_window_position(
                x,
                y,
                self.root.winfo_screenwidth(),
                self.root.winfo_screenheight(),
            )
            return f"+{safe_x}+{safe_y}"
        return "+30+30"

    def _load_ui_settings(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.ui_settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _load_bool_setting(self, key: str, *, default: bool) -> bool:
        value = self.ui_settings.get(key)
        return value if isinstance(value, bool) else default

    def _save_ui_settings(self) -> None:
        settings = {
            "x": int(self.root.winfo_x()),
            "y": int(self.root.winfo_y()),
            "mini_mode": self.mini_mode,
            "always_on_top": self.always_on_top,
        }
        tmp = self.ui_settings_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(settings, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.ui_settings_path)
        except OSError:
            pass

    def open_logs(self) -> None:
        path = self.watcher.state_dir
        try:
            subprocess.Popen(["explorer", str(path)])
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not open log folder:\n{exc}")

    def autostart_label(self) -> str:
        return "Disable autostart" if autostart_enabled() else "Enable autostart"

    def toggle_autostart(self) -> None:
        try:
            if autostart_enabled():
                disable_autostart()
                message = "Autostart disabled."
            else:
                path = enable_autostart()
                message = f"Autostart enabled:\n{path}"
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not update autostart:\n{exc}")
            return
        except RuntimeError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        messagebox.showinfo(APP_NAME, message)

    def run(self) -> None:
        self.root.mainloop()


def default_codex_home() -> Path:
    if os.environ.get("CODEX_HOME"):
        return Path(os.environ["CODEX_HOME"]).expanduser()
    return Path.home() / ".codex"


def default_codex_homes() -> list[Path]:
    homes = [default_codex_home()]
    homes.extend(discover_wsl_codex_homes())
    unique: list[Path] = []
    seen: set[str] = set()
    for home in homes:
        key = str(home).lower()
        if key not in seen:
            unique.append(home)
            seen.add(key)
    return unique


def discover_wsl_codex_homes() -> list[Path]:
    roots = [Path(r"\\wsl.localhost"), Path(r"\\wsl$")]
    homes: list[Path] = []
    for root in roots:
        try:
            distros = [path for path in root.iterdir() if path.is_dir()]
        except OSError:
            distros = [root / name for name in _wsl_distro_names()]
        for distro in distros:
            if distro.name.lower() == "docker-desktop":
                continue
            user_home_root = distro / "home"
            try:
                users = [path for path in user_home_root.iterdir() if path.is_dir()]
            except OSError:
                continue
            for user_home in users:
                codex_home = user_home / ".codex"
                if (codex_home / "sessions").exists():
                    homes.append(codex_home)
        if homes:
            break
    return homes


def _wsl_distro_names() -> list[str]:
    try:
        raw = subprocess.check_output(
            ["wsl.exe", "-l", "-q"],
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    names: list[str] = []
    for encoding in ["utf-16le", "utf-8"]:
        try:
            text = raw.decode(encoding, errors="ignore")
        except UnicodeDecodeError:
            continue
        text = text.replace("\x00", "")
        parsed = [line.strip() for line in text.splitlines() if line.strip()]
        if parsed:
            names = parsed
            break
    return [name for name in names if name.lower() != "docker-desktop"]


def default_state_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "CodexStatusbar"
    return Path.cwd() / ".codex-statusbar"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Always-on-top Codex status bar.")
    parser.add_argument(
        "--codex-home",
        type=Path,
        action="append",
        default=None,
        help="Codex data directory. Repeat to watch Windows and WSL Codex homes together.",
    )
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Scan once, print status JSON, and exit without opening the UI.",
    )
    parser.add_argument(
        "--allow-multiple",
        action="store_true",
        help="Allow multiple statusbar windows. By default, a second launch shows the existing window.",
    )
    autostart_group = parser.add_mutually_exclusive_group()
    autostart_group.add_argument(
        "--enable-autostart",
        action="store_true",
        help="Create a Windows Startup entry for codex-stat and exit.",
    )
    autostart_group.add_argument(
        "--disable-autostart",
        action="store_true",
        help="Remove the Windows Startup entry for codex-stat and exit.",
    )
    autostart_group.add_argument(
        "--autostart-status",
        action="store_true",
        help="Print whether the Windows Startup entry is enabled and exit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.enable_autostart:
        path = enable_autostart()
        print(f"Autostart enabled: {path}")
        return 0
    if args.disable_autostart:
        removed = disable_autostart()
        state = "disabled" if removed else "already disabled"
        print(f"Autostart {state}.")
        return 0
    if args.autostart_status:
        state = "enabled" if autostart_enabled() else "disabled"
        print(f"Autostart is {state}.")
        return 0

    codex_homes = args.codex_home or default_codex_homes()
    watcher = CodexSessionWatcher(
        codex_home=[home.expanduser() for home in codex_homes],
        state_dir=args.state_dir.expanduser(),
        stale_seconds=args.stale_seconds,
    )
    if args.once:
        snapshot = watcher.scan()
        print(json.dumps(asdict(snapshot), ensure_ascii=False, indent=2))
        return 0

    instance_guard = StatusbarInstanceGuard.acquire(
        args.state_dir.expanduser(),
        allow_multiple=args.allow_multiple,
    )
    if not instance_guard.acquired:
        owner = instance_guard.owner_pid or "unknown"
        print(f"{APP_NAME} is already running (pid {owner}); requested that window to show.")
        return 0

    try:
        app = StatusBarApp(
            watcher,
            poll_seconds=max(0.5, args.poll_seconds),
            instance_guard=instance_guard,
        )
        app.run()
    finally:
        instance_guard.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
