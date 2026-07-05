import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_statusbar import CodexSessionWatcher, StatusSnapshot


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
