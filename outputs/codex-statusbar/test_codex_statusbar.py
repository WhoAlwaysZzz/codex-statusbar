import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_statusbar import (
    CodexSessionWatcher,
    StatusBarApp,
    StatusSnapshot,
    StatusbarInstanceGuard,
    WindowsTrayIcon,
    _wsl_distro_names,
    always_on_top_label,
    autostart_enabled,
    clamp_window_position,
    disable_autostart,
    discover_wsl_codex_homes,
    enable_windows_dpi_awareness,
    enable_autostart,
    full_window_geometry_for_count,
    initial_window_geometry,
    main,
    mini_header_text,
    parse_args,
    render_autostart_cmd,
    status_surface_colors,
    tray_tooltip_text,
    windows_startup_dir,
)


class AutostartTests(unittest.TestCase):
    def test_windows_startup_dir_uses_appdata(self) -> None:
        path = windows_startup_dir("C:/Users/demo/AppData/Roaming")

        self.assertEqual(
            str(path).replace("\\", "/"),
            "C:/Users/demo/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup",
        )

    def test_render_autostart_cmd_quotes_command_parts(self) -> None:
        text = render_autostart_cmd(["C:/Tools/codex-stat.exe", "--allow-multiple"])

        self.assertIn("chcp 65001 >nul", text)
        self.assertIn('start "" /min "C:/Tools/codex-stat.exe" "--allow-multiple"', text)
        self.assertTrue(text.endswith("\n"))

    def test_enable_and_disable_autostart_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            startup_dir = Path(raw)

            path = enable_autostart(startup_dir, command=["C:/Tools/codex-stat.exe"])

            self.assertTrue(path.exists())
            self.assertTrue(autostart_enabled(startup_dir))
            self.assertIn("codex-stat.exe", path.read_text(encoding="utf-8"))
            self.assertTrue(disable_autostart(startup_dir))
            self.assertFalse(autostart_enabled(startup_dir))
            self.assertFalse(disable_autostart(startup_dir))

    def test_autostart_cli_actions_exit_before_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch.dict(os.environ, {"APPDATA": raw}):
                with patch("codex_statusbar.default_codex_homes") as default_homes:
                    with patch("sys.stdout", new_callable=io.StringIO):
                        self.assertEqual(main(["--enable-autostart"]), 0)
                        self.assertEqual(main(["--autostart-status"]), 0)
                        self.assertEqual(main(["--disable-autostart"]), 0)

            default_homes.assert_not_called()


class InstallerScriptTests(unittest.TestCase):
    def test_installer_is_user_scoped_and_reversible(self) -> None:
        script_path = Path(__file__).resolve().parent / "install.ps1"
        text = script_path.read_text(encoding="utf-8")

        self.assertIn("SupportsShouldProcess", text)
        self.assertIn("[switch]$RemovePath", text)
        self.assertIn('SetEnvironmentVariable("Path", $updatedPath, "User")', text)
        self.assertIn("codex-stat.exe", text)
        self.assertIn("codex-watchdog.exe", text)


class StatusbarInstanceTests(unittest.TestCase):
    def test_first_statusbar_launch_writes_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            guard = StatusbarInstanceGuard.acquire(state_dir)
            try:
                self.assertTrue(guard.acquired)
                payload = json.loads((state_dir / "statusbar.pid").read_text(encoding="utf-8"))
                self.assertEqual(payload["pid"], os.getpid())
            finally:
                guard.release()

            self.assertFalse((state_dir / "statusbar.pid").exists())

    def test_second_statusbar_launch_requests_existing_window(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            (state_dir / "statusbar.pid").write_text(
                json.dumps({"pid": 12345}),
                encoding="utf-8",
            )

            with patch("codex_statusbar.process_is_alive", return_value=True):
                guard = StatusbarInstanceGuard.acquire(state_dir)

            self.assertFalse(guard.acquired)
            self.assertEqual(guard.owner_pid, 12345)
            payload = json.loads((state_dir / "statusbar-control.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["action"], "show")

    def test_stale_statusbar_pid_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            (state_dir / "statusbar.pid").write_text(
                json.dumps({"pid": 12345}),
                encoding="utf-8",
            )

            with patch("codex_statusbar.process_is_alive", return_value=False):
                guard = StatusbarInstanceGuard.acquire(state_dir)
            try:
                self.assertTrue(guard.acquired)
                payload = json.loads((state_dir / "statusbar.pid").read_text(encoding="utf-8"))
                self.assertEqual(payload["pid"], os.getpid())
                self.assertFalse((state_dir / "statusbar-control.json").exists())
            finally:
                guard.release()

    def test_allow_multiple_does_not_claim_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            guard = StatusbarInstanceGuard.acquire(state_dir, allow_multiple=True)

            self.assertTrue(guard.acquired)
            self.assertFalse(guard.owns_pid_file)
            self.assertFalse((state_dir / "statusbar.pid").exists())


class WindowControlTests(unittest.TestCase):
    def test_dpi_awareness_is_a_noop_outside_windows(self) -> None:
        with patch("codex_statusbar.os.name", "posix"):
            self.assertFalse(enable_windows_dpi_awareness())

    def test_initial_window_geometry_uses_full_statusbar_size(self) -> None:
        self.assertEqual(initial_window_geometry(30, 30), "720x116+30+30")
        self.assertEqual(initial_window_geometry(1184, 948), "720x116+1184+948")

    def test_tray_tooltip_summarizes_state_and_attention(self) -> None:
        self.assertEqual(
            tray_tooltip_text("working", "Thinking", 2),
            "Codex Statusbar: Working (2)",
        )
        self.assertEqual(
            tray_tooltip_text("waiting", "Waiting approval", 1, needs_human=True),
            "Codex Statusbar: Attention (1)",
        )
        self.assertEqual(
            tray_tooltip_text("custom", "Custom state", -1),
            "Codex Statusbar: Custom state (0)",
        )

    def test_tray_tooltip_update_is_safe_without_windows_shell(self) -> None:
        tray = object.__new__(WindowsTrayIcon)
        tray.available = False
        tray.hwnd = None
        tray.tooltip = "old"

        tray.update_tooltip("x" * 130)

        self.assertEqual(tray.tooltip, "x" * 127)

    def test_window_close_exits_after_confirmation(self) -> None:
        app = object.__new__(StatusBarApp)
        app.exit_app = Mock()

        with patch("codex_statusbar.messagebox.askyesno", return_value=True):
            app.handle_window_close()

        app.exit_app.assert_called_once_with()

    def test_window_close_keeps_running_when_exit_is_declined(self) -> None:
        app = object.__new__(StatusBarApp)
        app.exit_app = Mock()

        with patch("codex_statusbar.messagebox.askyesno", return_value=False):
            app.handle_window_close()

        app.exit_app.assert_not_called()

    def test_reset_window_position_restores_default_and_persists_it(self) -> None:
        app = object.__new__(StatusBarApp)
        app.root = Mock()
        app._save_ui_settings = Mock()

        app.reset_window_position()

        app.root.geometry.assert_called_once_with("+30+30")
        app._save_ui_settings.assert_called_once_with()


class MultiSessionBoardTests(unittest.TestCase):
    def test_always_on_top_menu_label_reflects_state(self) -> None:
        self.assertEqual(always_on_top_label(True), "Disable always on top")
        self.assertEqual(always_on_top_label(False), "Enable always on top")

    def test_saved_window_position_is_clamped_to_visible_screen(self) -> None:
        self.assertEqual(clamp_window_position(100, 120, 1920, 1080), (100, 120))
        self.assertEqual(clamp_window_position(-500, -40, 1920, 1080), (16, 16))
        self.assertEqual(clamp_window_position(5000, 3000, 1920, 1080), (1184, 948))
        self.assertEqual(clamp_window_position(50, 50, 0, 0), (30, 30))

    def test_status_surface_uses_neutral_background_with_state_accent(self) -> None:
        self.assertEqual(status_surface_colors("completed"), ("#15803d", "#ffffff"))
        self.assertEqual(status_surface_colors("unknown"), ("#334155", "#ffffff"))

    def test_mini_header_shows_state_summary_and_count(self) -> None:
        self.assertEqual(mini_header_text("working", "Thinking", 2), "Working (2)")
        self.assertEqual(mini_header_text("reconnecting", "Desktop reconnecting", 1), "Reconnect (1)")
        self.assertEqual(
            mini_header_text("waiting", "Waiting approval", 3, needs_human=True),
            "Attention (3)",
        )
        self.assertEqual(mini_header_text("custom", "Custom state", -1), "Custom state (0)")

    def test_full_window_height_grows_with_visible_sessions(self) -> None:
        self.assertEqual(full_window_geometry_for_count(0), "720x116")
        self.assertEqual(full_window_geometry_for_count(1), "720x116")
        self.assertEqual(full_window_geometry_for_count(3), "720x196")
        self.assertEqual(full_window_geometry_for_count(20), "720x396")

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

    def test_scan_all_reads_multiple_codex_homes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home_a = root / "windows-codex"
            home_b = root / "wsl-codex"
            session_dir_a = home_a / "sessions" / "2026" / "07" / "06"
            session_dir_b = home_b / "sessions" / "2026" / "07" / "06"
            session_dir_a.mkdir(parents=True)
            session_dir_b.mkdir(parents=True)
            self._write_session(session_dir_a / "rollout-session-a.jsonl", "session-a", "working")
            self._write_session(session_dir_b / "rollout-session-b.jsonl", "session-b", "executing")

            watcher = CodexSessionWatcher([home_a, home_b], root / "state")
            board = watcher.scan_all()

            session_ids = {snapshot.session_id for snapshot in board.snapshots}
            self.assertEqual(session_ids, {"session-a", "session-b"})

    def test_explicit_codex_home_arguments_are_preserved(self) -> None:
        args = parse_args(["--codex-home", "C:/a/.codex", "--codex-home", "C:/b/.codex"])

        self.assertEqual([str(path).replace("\\", "/") for path in args.codex_home], ["C:/a/.codex", "C:/b/.codex"])

    def test_wsl_discovery_is_best_effort(self) -> None:
        homes = discover_wsl_codex_homes()

        self.assertIsInstance(homes, list)

    def test_wsl_distro_names_decodes_utf16_output(self) -> None:
        raw = "Ubuntu\nUbuntu-22.04\n".encode("utf-16le")

        with patch("codex_statusbar.subprocess.check_output", return_value=raw):
            names = _wsl_distro_names()

        self.assertEqual(names, ["Ubuntu", "Ubuntu-22.04"])

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

            self.assertEqual(board.snapshots, [])
            self.assertEqual(board.primary.state, "idle")

    def test_turn_aborted_is_not_reported_as_stale_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            codex_home = root / "codex-home"
            session_dir = codex_home / "sessions" / "2026" / "07" / "06"
            session_dir.mkdir(parents=True)
            self._write_session(
                session_dir / "rollout-aborted.jsonl",
                "aborted",
                "aborted",
                seconds_ago=7200,
            )

            watcher = CodexSessionWatcher(codex_home, root / "state")
            board = watcher.scan_all()

            self.assertEqual(len(board.snapshots), 1)
            self.assertEqual(board.primary.state, "completed")
            self.assertEqual(board.primary.label, "Interrupted")

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
        elif state == "aborted":
            rows.append(
                {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "payload": {"type": "turn_aborted", "reason": "interrupted"},
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
