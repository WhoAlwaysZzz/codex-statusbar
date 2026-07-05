import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_appserver import CodexAppServerRunner


class CapturingRunner(CodexAppServerRunner):
    def __init__(self, tmp: Path) -> None:
        super().__init__(
            prompt="do the thing",
            cwd=tmp,
            state_dir=tmp,
            codex_bin="codex.cmd",
            max_recoveries=2,
            continue_prompt="继续",
            appserver_args=[],
            model=None,
            approval_policy=None,
            sandbox=None,
            ephemeral=False,
            stale_seconds=300,
        )
        self.responses = []

    def _send_response(self, request_id, result=None, error=None):
        self.responses.append((request_id, result, error))


class CodexAppServerMappingTests(unittest.TestCase):
    def test_maps_core_thread_turn_and_item_states(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runner = CapturingRunner(Path(raw))

            runner._handle_message(
                {
                    "method": "thread/started",
                    "params": {
                        "thread": {
                            "id": "thread-1",
                            "sessionId": "session-1",
                            "cwd": raw,
                        }
                    },
                }
            )
            self.assertEqual(runner.state.thread_id, "thread-1")
            self.assertEqual(runner.state.session_id, "session-1")

            runner._handle_message(
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1", "status": "inProgress"},
                    },
                }
            )
            self.assertEqual(runner.state.state, "working")
            self.assertTrue(runner.state.turn_started)

            runner._handle_message(
                {
                    "method": "item/started",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {
                            "id": "item-1",
                            "type": "commandExecution",
                            "command": "python -m pytest",
                        },
                    },
                }
            )
            self.assertEqual(runner.state.state, "executing")
            self.assertIn("python -m pytest", runner.state.detail)

            runner._handle_message(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "itemId": "item-2",
                        "delta": "done",
                    },
                }
            )
            self.assertEqual(runner.state.state, "outputting")

    def test_approval_requests_are_blocked_and_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runner = CapturingRunner(Path(raw))

            terminal = runner._handle_message(
                {
                    "id": 42,
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": "Remove-Item -Recurse ."},
                }
            )

            self.assertTrue(terminal)
            self.assertEqual(runner.state.state, "waiting")
            self.assertTrue(runner.state.needs_human)
            self.assertEqual(runner.responses, [(42, {"decision": "cancel"}, None)])

    def test_recoverable_stream_error_requests_continue_after_started_turn(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runner = CapturingRunner(Path(raw))
            runner.state.thread_id = "thread-1"
            runner.state.turn_started = True
            runner.state.recoverable_error = True

            decision = runner._decide_after_terminal()

            self.assertEqual(decision["decision"], "resume_continue")
            self.assertEqual(decision["prompt"], "继续")


if __name__ == "__main__":
    unittest.main()
