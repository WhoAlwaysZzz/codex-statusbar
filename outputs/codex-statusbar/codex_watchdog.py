#!/usr/bin/env python3
"""Global Codex session watchdog with narrow mechanical recovery.

This watches local Codex session files. When a current session is clearly in a
recoverable stream/network failure, it runs `codex exec resume <id> "继续"`.
It never clicks the screen, never approves permissions, and never changes proxy
or network settings.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codex_statusbar import (
    CodexSessionWatcher,
    StatusSnapshot,
    default_codex_home,
    default_state_dir,
    load_jsonl,
    now_iso,
)


HUMAN_MARKERS = [
    "unauthorized",
    "usagelimitexceeded",
    "sessionbudgetexceeded",
    "contextwindowexceeded",
    "quota",
    "rate limit",
    "login",
    "auth",
    "forbidden",
    "approval",
    "permission",
    "dangerously",
]


@dataclass
class SessionTurnInfo:
    session_id: str
    resume_id: str
    cwd: str | None
    last_user_message: str | None = None
    turn_started_after_last_user: bool = False
    task_completed_after_last_user: bool = False


@dataclass
class WatchdogState:
    state: str = "watching"
    label: str = "Watchdog watching"
    detail: str = ""
    source: str = "watchdog"
    session_id: str | None = None
    cwd: str | None = None
    watched_sessions: int = 0
    recovery_attempts: int = 0
    needs_human: bool = False
    recommended_action: str | None = None
    error_info: str | None = None
    updated_at: str = field(default_factory=now_iso)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def contains_human_marker(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in HUMAN_MARKERS)


def extract_resume_id(session_id: str) -> str:
    match = re.search(
        r"(019[0-9a-f]{5,}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        session_id,
        re.IGNORECASE,
    )
    return match.group(1) if match else session_id


def resolve_codex_bin(configured: str) -> str:
    if configured != "codex":
        return configured
    return (
        shutil.which("codex.cmd")
        or shutil.which("codex.exe")
        or shutil.which("codex")
        or configured
    )


class DesktopSessionWatchdog:
    def __init__(
        self,
        *,
        codex_home: Path,
        state_dir: Path,
        codex_bin: str,
        poll_seconds: float,
        max_recoveries_per_session: int,
        cooldown_seconds: int,
        continue_prompt: str,
        recent_hours: float,
        dry_run: bool,
        codex_args: list[str],
        skip_git_repo_check: bool,
    ) -> None:
        self.codex_home = codex_home
        self.state_dir = state_dir
        self.codex_bin = resolve_codex_bin(codex_bin)
        self.poll_seconds = max(1.0, poll_seconds)
        self.max_recoveries_per_session = max(0, max_recoveries_per_session)
        self.cooldown_seconds = max(0, cooldown_seconds)
        self.continue_prompt = continue_prompt
        self.recent_hours = max(0.1, recent_hours)
        self.dry_run = dry_run
        self.codex_args = codex_args
        self.skip_git_repo_check = skip_git_repo_check

        self.watcher = CodexSessionWatcher(codex_home, state_dir)
        self.status_path = state_dir / "watchdog_status.json"
        self.events_path = state_dir / "watchdog_events.jsonl"
        self.actions_path = state_dir / "actions.jsonl"
        self.recovery_state_path = state_dir / "watchdog_recoveries.json"
        self.recovery_state = self._load_recovery_state()
        self.state = WatchdogState()
        self._write_status()

    def run_forever(self) -> int:
        while True:
            self.scan_once()
            time.sleep(self.poll_seconds)

    def scan_once(self) -> WatchdogState:
        snapshots = self._session_snapshots()
        watched = len(snapshots)
        for snapshot, info in snapshots:
            if snapshot.needs_human or (
                snapshot.state in {"failed", "waiting"}
                and contains_human_marker(
                    " ".join([snapshot.detail or "", snapshot.error_info or ""])
                )
            ):
                self._set_state(
                    "failed",
                    "Watchdog needs human",
                    snapshot.detail or snapshot.error_info or "Codex needs human attention.",
                    info,
                    watched,
                    needs_human=True,
                    recommended_action="Open Codex and handle login/quota/approval/permission.",
                    error_info=snapshot.error_info,
                )
                self._record_action("blocked", "human_required", snapshot, info)
                return self.state

            if snapshot.state == "reconnecting" and not snapshot.needs_human:
                self._recover(snapshot, info, watched)
                return self.state

        self._set_state(
            "watching",
            "Watchdog watching",
            f"Watching {watched} recent Codex session(s).",
            None,
            watched,
        )
        return self.state

    def _session_snapshots(self) -> list[tuple[StatusSnapshot, SessionTurnInfo]]:
        cutoff = time.time() - self.recent_hours * 3600
        pairs: list[tuple[StatusSnapshot, SessionTurnInfo]] = []
        for path in self.watcher._recent_session_files():
            try:
                if path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            snapshot = self.watcher._snapshot_for_file(path)
            if not snapshot or not snapshot.session_id:
                continue
            desktop_snapshot = self.watcher._desktop_snapshot(snapshot)
            if desktop_snapshot:
                snapshot = desktop_snapshot
            info = self._turn_info(path, snapshot)
            pairs.append((snapshot, info))
        pairs.sort(key=lambda pair: pair[0].last_event_age_seconds or 10**9)
        return pairs

    def _turn_info(self, path: Path, snapshot: StatusSnapshot) -> SessionTurnInfo:
        info = SessionTurnInfo(
            session_id=snapshot.session_id or path.stem,
            resume_id=extract_resume_id(snapshot.session_id or path.stem),
            cwd=snapshot.cwd,
        )
        for row in load_jsonl(path):
            if row.get("type") != "event_msg":
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            payload_type = str(payload.get("type") or "")
            if payload_type == "user_message":
                message = payload.get("message")
                info.last_user_message = message if isinstance(message, str) else None
                info.turn_started_after_last_user = False
                info.task_completed_after_last_user = False
            elif payload_type == "task_started":
                info.turn_started_after_last_user = True
                info.task_completed_after_last_user = False
            elif payload_type == "task_complete":
                info.task_completed_after_last_user = True
        return info

    def _recover(
        self,
        snapshot: StatusSnapshot,
        info: SessionTurnInfo,
        watched: int,
    ) -> None:
        recovery = self.recovery_state.get(info.session_id, {})
        count = int(recovery.get("count") or 0)
        last_at = float(recovery.get("last_at") or 0)
        now = time.time()

        if count >= self.max_recoveries_per_session:
            self._set_state(
                "failed",
                "Watchdog recovery limit",
                f"Recovery limit reached for {info.resume_id}.",
                info,
                watched,
                needs_human=True,
                recommended_action="Open Codex and decide whether to continue manually.",
                error_info=snapshot.error_info,
            )
            self._record_action("blocked", "recovery_limit", snapshot, info)
            return

        if now - last_at < self.cooldown_seconds:
            self._set_state(
                "watching",
                "Watchdog cooldown",
                f"Recent recovery already attempted for {info.resume_id}.",
                info,
                watched,
                error_info=snapshot.error_info,
            )
            self._record_action("skip", "cooldown", snapshot, info)
            return

        command = self._recovery_command(info)
        reason = (
            "resume_continue_after_started_turn"
            if info.turn_started_after_last_user
            else "resume_original_after_pre_turn_failure"
        )
        self._set_state(
            "recovering",
            "Watchdog recovering",
            f"{reason}: {info.resume_id}",
            info,
            watched,
            error_info=snapshot.error_info,
        )
        self._record_action("start_recovery", reason, snapshot, info, command=command)

        recovery["count"] = count + 1
        recovery["last_at"] = now
        recovery["last_reason"] = reason
        self.recovery_state[info.session_id] = recovery
        self._save_recovery_state()

        if self.dry_run:
            self._record_action("dry_run", reason, snapshot, info, command=command)
            return

        result = self._run_recovery_command(command, Path(info.cwd or Path.cwd()))
        output = "\n".join(part for part in [result.stdout, result.stderr] if part).strip()
        self._record_action(
            "recovery_complete",
            f"exit_code={result.returncode}",
            snapshot,
            info,
            command=command,
            output=output[-1000:] if output else None,
        )
        if result.returncode == 0:
            self._set_state(
                "watching",
                "Watchdog recovered",
                f"Recovery command completed for {info.resume_id}.",
                info,
                watched,
            )
            return
        needs_human = contains_human_marker(output)
        self._set_state(
            "failed",
            "Watchdog recovery failed",
            output[-240:] if output else f"codex exited with {result.returncode}",
            info,
            watched,
            needs_human=needs_human,
            recommended_action=(
                "Open Codex and handle the blocker."
                if needs_human
                else "Check network/proxy or inspect watchdog_events.jsonl."
            ),
            error_info=output[-1000:] if output else None,
        )

    def _recovery_command(self, info: SessionTurnInfo) -> list[str]:
        prompt = self.continue_prompt
        if not info.turn_started_after_last_user and info.last_user_message:
            prompt = info.last_user_message
        command = [self.codex_bin, "exec", "resume", "--json"]
        if self.skip_git_repo_check:
            command.append("--skip-git-repo-check")
        command.extend(self.codex_args)
        command.extend([info.resume_id, prompt])
        return command

    def _run_recovery_command(
        self,
        command: list[str],
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout, stderr = process.communicate()
        append_jsonl(
            self.events_path,
            {
                "timestamp": now_iso(),
                "kind": "recovery_process",
                "command": command,
                "cwd": str(cwd),
                "returncode": process.returncode,
                "stdout_tail": stdout[-2000:] if stdout else "",
                "stderr_tail": stderr[-2000:] if stderr else "",
            },
        )
        return subprocess.CompletedProcess(command, process.returncode or 0, stdout, stderr)

    def _set_state(
        self,
        state: str,
        label: str,
        detail: str,
        info: SessionTurnInfo | None,
        watched: int,
        *,
        needs_human: bool = False,
        recommended_action: str | None = None,
        error_info: str | None = None,
    ) -> None:
        attempts = sum(int(item.get("count") or 0) for item in self.recovery_state.values())
        self.state = WatchdogState(
            state=state,
            label=label,
            detail=detail,
            session_id=info.session_id if info else None,
            cwd=info.cwd if info else None,
            watched_sessions=watched,
            recovery_attempts=attempts,
            needs_human=needs_human,
            recommended_action=recommended_action,
            error_info=error_info,
            updated_at=now_iso(),
        )
        self._write_status()

    def _write_status(self) -> None:
        atomic_write_json(self.status_path, asdict(self.state))

    def _record_action(
        self,
        decision: str,
        reason: str,
        snapshot: StatusSnapshot,
        info: SessionTurnInfo,
        *,
        command: list[str] | None = None,
        output: str | None = None,
    ) -> None:
        record = {
            "timestamp": now_iso(),
            "source": "watchdog",
            "decision": decision,
            "reason": reason,
            "session_id": info.session_id,
            "resume_id": info.resume_id,
            "cwd": info.cwd,
            "snapshot_state": snapshot.state,
            "snapshot_label": snapshot.label,
            "snapshot_detail": snapshot.detail,
            "error_info": snapshot.error_info,
            "command": command,
            "output": output,
        }
        append_jsonl(self.actions_path, record)
        append_jsonl(self.events_path, record)

    def _load_recovery_state(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.recovery_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_recovery_state(self) -> None:
        atomic_write_json(self.recovery_state_path, self.recovery_state)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch all recent Codex sessions and recover network failures.")
    parser.add_argument("--codex-home", type=Path, default=default_codex_home())
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument("--max-recoveries-per-session", type=int, default=2)
    parser.add_argument("--cooldown-seconds", type=int, default=90)
    parser.add_argument("--continue-prompt", default="继续")
    parser.add_argument("--recent-hours", type=float, default=24.0)
    parser.add_argument("--codex-arg", action="append", default=[])
    parser.add_argument("--require-git-repo", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    watchdog = DesktopSessionWatchdog(
        codex_home=args.codex_home.expanduser(),
        state_dir=args.state_dir.expanduser(),
        codex_bin=args.codex_bin,
        poll_seconds=args.poll_seconds,
        max_recoveries_per_session=args.max_recoveries_per_session,
        cooldown_seconds=args.cooldown_seconds,
        continue_prompt=args.continue_prompt,
        recent_hours=args.recent_hours,
        dry_run=args.dry_run,
        codex_args=args.codex_arg,
        skip_git_repo_check=not args.require_git_repo,
    )
    if args.once:
        state = watchdog.scan_once()
        print(json.dumps(asdict(state), ensure_ascii=False, indent=2))
        return 0
    try:
        return watchdog.run_forever()
    except KeyboardInterrupt:
        watchdog._set_state(
            "failed",
            "Watchdog stopped",
            "Stopped by user.",
            None,
            0,
            needs_human=False,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
