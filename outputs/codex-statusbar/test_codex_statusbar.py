import json
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_statusbar import CodexSessionWatcher, StatusSnapshot


class MultiSessionBoardTests(unittest.TestCase):
    def test_scan_all_keeps_multiple_active_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            codex_home = root / "codex-home"
            session_dir = codex_home / "sessions" / "2026" / "07" / "06"
            session_dir.mkdir(parents=True)
            self._write_session(session_dir / "rollout-session-a.jsonl", "session-a", "working")
            self._write_session(session_dir / "rollout-session-b.jsonl", "session-b", "executing")

            watcher = CodexSessionWatcher(codex_home, root / "state")
            board = watcher.scan_all()

            session_ids = {snapshot.session_id for snapshot in board.snapshots}
            self.assertIn("session-a", session_ids)
            self.assertIn("session-b", session_ids)
            self.assertEqual(len(board.snapshots), 2)
            status_all = json.loads((root / "state" / "status_all.json").read_text(encoding="utf-8"))
            self.assertEqual(len(status_all["snapshots"]), 2)

    def test_old_completed_hides_unless_it_is_the_last_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            codex_home = root / "codex-home"
            session_dir = codex_home / "sessions" / "2026" / "07" / "06"
            session_dir.mkdir(parents=True)
            self._write_session(
                session_dir / "rollout-old-done.jsonl",
                "old-done",
                "completed",
                seconds_ago=600,
            )
            self._write_session(session_dir / "rollout-active.jsonl", "active", "working")

            watcher = CodexSessionWatcher(codex_home, root / "state")
            board = watcher.scan_all()

            self.assertEqual([snapshot.session_id for snapshot in board.snapshots], ["active"])

            (session_dir / "rollout-active.jsonl").unlink()
            board = watcher.scan_all()

            self.assertEqual([snapshot.session_id for snapshot in board.snapshots], ["old-done"])

    def test_old_stale_hides_unless_it_is_the_last_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            codex_home = root / "codex-home"
            session_dir = codex_home / "sessions" / "2026" / "07" / "06"
            session_dir.mkdir(parents=True)
            self._write_session(
                session_dir / "rollout-old-stale.jsonl",
                "old-stale",
                "working",
                seconds_ago=7200,
            )
            self._write_session(session_dir / "rollout-active.jsonl", "active", "working")

            watcher = CodexSessionWatcher(codex_home, root / "state")
            board = watcher.scan_all()

            self.assertEqual([snapshot.session_id for snapshot in board.snapshots], ["active"])

            (session_dir / "rollout-active.jsonl").unlink()
            board = watcher.scan_all()

            self.assertEqual([snapshot.session_id for snapshot in board.snapshots], ["old-stale"])

    def _write_session(
        self,
        path: Path,
        session_id: str,
        state: str,
        *,
        seconds_ago: int = 1,
    ) -> None:
        timestamp = (
            datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        rows = [
            {"timestamp": timestamp, "type": "session_meta", "payload": {"cwd": str(path.parent)}},
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "task_started"},
            },
        ]
        if state == "executing":
            rows.append(
                {
                    "timestamp": timestamp,
                    "type": "response_item",
                    "payload": {"type": "function_call", "name": "shell_command"},
                }
            )
        elif state == "completed":
            rows.append(
                {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "last_agent_message": "done"},
                }
            )
        path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )


class DesktopInternalLogTests(unittest.TestCase):
    def test_remote_control_reconnect_does_not_override_thinking(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            self._write_log(
                codex_home,
                "WARN",
                "codex_app_server_transport::transport::remote_control::websocket",
                "failed to connect app-server remote control websocket reconnect_attempt=3",
            )

            watcher = CodexSessionWatcher(codex_home, root / "state")
            base = StatusSnapshot(
                state="working",
                label="Thinking",
                detail="Last event 57s ago",
                session_id="2026-07-04T11-46-41-019f2b3c-518b-7e41-8b66-a8c3bbc3f64a",
                source_file="session.jsonl",
                cwd=str(root),
                last_event_type="event_msg:task_started",
                last_event_age_seconds=57,
                updated_at="2026-07-05T00:00:00+00:00",
            )

            snapshot = watcher._desktop_snapshot(base)

            self.assertIsNone(snapshot)

    def test_generated_text_reconnect_word_does_not_override_thinking(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            self._write_log(
                codex_home,
                "WARN",
                "codex_api::sse::responses",
                (
                    "thread.id=019f2b3c-518b-7e41-8b66-a8c3bbc3f64a "
                    "assistant output mentioned ResponseStreamDisconnected and reconnect"
                ),
            )

            watcher = CodexSessionWatcher(codex_home, root / "state")
            base = StatusSnapshot(
                state="working",
                label="Thinking",
                detail="Last event 57s ago",
                session_id="2026-07-04T11-46-41-019f2b3c-518b-7e41-8b66-a8c3bbc3f64a",
                source_file="session.jsonl",
                cwd=str(root),
                last_event_type="event_msg:task_started",
                last_event_age_seconds=57,
                updated_at="2026-07-05T00:00:00+00:00",
            )

            snapshot = watcher._desktop_snapshot(base)

            self.assertIsNone(snapshot)

    def test_current_session_stream_error_overrides_thinking(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            self._write_log(
                codex_home,
                "WARN",
                "codex_core::turn",
                (
                    "session_loop{thread_id=019f2b3c-518b-7e41-8b66-a8c3bbc3f64a}: "
                    "ResponseStreamDisconnected error=net::ERR_CONNECTION_TIMED_OUT"
                ),
            )

            watcher = CodexSessionWatcher(codex_home, root / "state")
            base = StatusSnapshot(
                state="working",
                label="Thinking",
                detail="Last event 57s ago",
                session_id="2026-07-04T11-46-41-019f2b3c-518b-7e41-8b66-a8c3bbc3f64a",
                source_file="session.jsonl",
                cwd=str(root),
                last_event_type="event_msg:task_started",
                last_event_age_seconds=57,
                updated_at="2026-07-05T00:00:00+00:00",
            )

            snapshot = watcher._desktop_snapshot(base)

            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.state, "reconnecting")
            self.assertEqual(
                snapshot.session_id,
                "2026-07-04T11-46-41-019f2b3c-518b-7e41-8b66-a8c3bbc3f64a",
            )

    def _write_log(
        self,
        codex_home: Path,
        level: str,
        target: str,
        body: str,
    ) -> None:
        db = codex_home / "logs_2.sqlite"
        con = sqlite3.connect(db)
        con.execute(
            """
            create table logs (
                id integer primary key autoincrement,
                ts integer not null,
                ts_nanos integer not null,
                level text not null,
                target text not null,
                feedback_log_body text,
                module_path text,
                file text,
                line integer,
                thread_id text,
                process_uuid text,
                estimated_bytes integer not null default 0
            )
            """
        )
        con.execute(
            """
            insert into logs (ts, ts_nanos, level, target, feedback_log_body)
            values (?, 0, ?, ?, ?)
            """,
            (int(time.time()), level, target, body),
        )
        con.commit()
        con.close()


if __name__ == "__main__":
    unittest.main()
