import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_watchdog import DesktopSessionWatchdog, SessionTurnInfo


class WatchdogRecoveryTests(unittest.TestCase):
    def test_uses_continue_after_turn_started(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            watchdog = self._watchdog(Path(raw))
            info = SessionTurnInfo(
                session_id="2026-07-04T11-46-41-019f2b3c-518b-7e41-8b66-a8c3bbc3f64a",
                resume_id="019f2b3c-518b-7e41-8b66-a8c3bbc3f64a",
                cwd=raw,
                last_user_message="原始请求",
                turn_started_after_last_user=True,
            )

            command = watchdog._recovery_command(info)

            self.assertEqual(command[-2], "019f2b3c-518b-7e41-8b66-a8c3bbc3f64a")
            self.assertEqual(command[-1], "继续")

    def test_replays_original_message_before_turn_started(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            watchdog = self._watchdog(Path(raw))
            info = SessionTurnInfo(
                session_id="2026-07-04T11-46-41-019f2b3c-518b-7e41-8b66-a8c3bbc3f64a",
                resume_id="019f2b3c-518b-7e41-8b66-a8c3bbc3f64a",
                cwd=raw,
                last_user_message="原始请求",
                turn_started_after_last_user=False,
            )

            command = watchdog._recovery_command(info)

            self.assertEqual(command[-1], "原始请求")

    def test_dry_run_recovers_reconnecting_session_without_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            codex_home = root / "codex-home"
            session_dir = codex_home / "sessions" / "2026" / "07" / "05"
            session_dir.mkdir(parents=True)
            session = (
                session_dir
                / "rollout-2026-07-05T10-00-00-019f2b3c-518b-7e41-8b66-a8c3bbc3f64a.jsonl"
            )
            self._write_jsonl(
                session,
                [
                    {"type": "session_meta", "payload": {"cwd": raw}},
                    {
                        "timestamp": "2026-07-05T00:00:00Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "做一件事"},
                    },
                    {
                        "timestamp": "2026-07-05T00:00:01Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started"},
                    },
                    {
                        "timestamp": "2026-07-05T00:00:02Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "error",
                            "codex_error_info": "ResponseStreamDisconnected",
                        },
                    },
                ],
            )
            watchdog = self._watchdog(root, codex_home=codex_home)

            state = watchdog.scan_once()

            self.assertEqual(state.state, "recovering")
            self.assertEqual(state.session_id, "2026-07-05T10-00-00-019f2b3c-518b-7e41-8b66-a8c3bbc3f64a")
            actions = (root / "state" / "actions.jsonl").read_text(encoding="utf-8")
            self.assertIn("start_recovery", actions)
            self.assertIn("dry_run", actions)

    def test_reconnect_grace_period_waits_before_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            codex_home = root / "codex-home"
            session_dir = codex_home / "sessions" / "2026" / "07" / "05"
            session_dir.mkdir(parents=True)
            session = (
                session_dir
                / "rollout-2026-07-05T10-00-00-019f2b3c-518b-7e41-8b66-a8c3bbc3f64a.jsonl"
            )
            self._write_jsonl(
                session,
                [
                    {"type": "session_meta", "payload": {"cwd": raw}},
                    {
                        "timestamp": "2026-07-05T00:00:00Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "做一件事"},
                    },
                    {
                        "timestamp": "2026-07-05T00:00:01Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started"},
                    },
                    {
                        "timestamp": "2026-07-05T00:00:02Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "error",
                            "codex_error_info": "ResponseStreamDisconnected",
                        },
                    },
                ],
            )
            watchdog = DesktopSessionWatchdog(
                codex_home=codex_home,
                state_dir=root / "state",
                codex_bin="codex",
                poll_seconds=1,
                max_recoveries_per_session=2,
                cooldown_seconds=0,
                reconnect_grace_seconds=120,
                continue_prompt="继续",
                recent_hours=24,
                dry_run=True,
                codex_args=[],
                skip_git_repo_check=True,
            )

            state = watchdog.scan_once()

            self.assertEqual(state.state, "watching")
            self.assertEqual(state.label, "Watchdog observing reconnect")
            actions = (root / "state" / "actions.jsonl").read_text(encoding="utf-8")
            self.assertIn("reconnect_grace_period", actions)
            self.assertNotIn("start_recovery", actions)

    def _watchdog(
        self,
        root: Path,
        *,
        codex_home: Path | None = None,
    ) -> DesktopSessionWatchdog:
        return DesktopSessionWatchdog(
            codex_home=codex_home or root / "codex-home",
            state_dir=root / "state",
            codex_bin="codex",
            poll_seconds=1,
            max_recoveries_per_session=2,
            cooldown_seconds=0,
            reconnect_grace_seconds=0,
            continue_prompt="继续",
            recent_hours=24,
            dry_run=True,
            codex_args=[],
            skip_git_repo_check=True,
        )

    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    unittest.main()
