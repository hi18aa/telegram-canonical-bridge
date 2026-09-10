from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from telegram_canonical_bridge import agent_task_runner, agent_tasks
from telegram_canonical_bridge import native_delivery_runner
from telegram_canonical_bridge.native_delivery import flush_native_outbox, kick_native_outbox
from telegram_canonical_bridge.state import BridgeState


class _FakeContext:
    def __init__(self, settings=None, terminal_result=None, process_result=None) -> None:
        self.settings = settings or {}
        self.terminal_result = terminal_result or {
            "session_id": "proc_test1234",
            "exit_code": None,
            "error": None,
        }
        self.process_result = process_result or {"status": "killed"}
        self.calls = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def dispatch_tool(self, name, args, **kwargs):
        self.calls.append((name, args, kwargs))
        result = self.process_result if name == "process_manage" else self.terminal_result
        return json.dumps(result)


class AgentTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = BridgeState(Path(self.temp.name) / "tasks.sqlite3")
        self.original_settings = agent_tasks._SETTINGS
        self.original_ctx = agent_tasks._CTX
        self.call_number = 0

    def tearDown(self) -> None:
        agent_tasks._SETTINGS = self.original_settings
        agent_tasks._CTX = self.original_ctx
        self.temp.cleanup()

    def _task(
        self,
        *,
        status: str = "dispatched",
        process_id: str | None = "proc_test1234",
        delivery_target: str = "local",
        profile: str = "default",
    ):
        self.call_number += 1
        task, _ = self.state.create_task(
            delivery_target=delivery_target,
            origin_profile=profile,
            origin_session_id=f"main-session-{self.call_number}",
            origin_turn_id=f"turn-{self.call_number}",
            origin_tool_call_id=f"call-{self.call_number}",
            target="operitrace-agent",
            queue_outbox=False,
        )
        if status == "dispatching" and process_id is None:
            return task
        return self.state.transition_task(
            task.id,
            status=status,
            process_id=process_id,
            progress="runner started",
            evidence="test",
        )

    def test_local_delivery_is_durably_acknowledged(self) -> None:
        self._task()
        self.assertEqual(flush_native_outbox(self.state), {"sent": 1, "failed": 0})
        self.assertEqual(self.state.claim_due_outbox(), [])

    def test_delivery_uses_origin_profile_and_public_send_cli(self) -> None:
        self._task(delivery_target="telegram:123", profile="controller-two")
        completed = Mock(returncode=0, stdout="", stderr="")
        with patch(
            "telegram_canonical_bridge.native_delivery.subprocess.run",
            return_value=completed,
        ) as run:
            self.assertEqual(flush_native_outbox(self.state), {"sent": 1, "failed": 0})
        argv = run.call_args.args[0]
        self.assertEqual(argv[1:5], ["-p", "controller-two", "send", "--to"])
        self.assertEqual(argv[5], "telegram:123")
        self.assertIn("--file", argv)

    def test_remote_delivery_kick_is_non_blocking(self) -> None:
        self._task(delivery_target="telegram")
        with patch("telegram_canonical_bridge.native_delivery.subprocess.Popen") as popen:
            self.assertTrue(kick_native_outbox(self.state))
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1:3], ["-m", "telegram_canonical_bridge.native_delivery_runner"])
        self.assertIn(str(self.state.path.resolve()), argv)

    def test_delivery_runner_retries_and_drains(self) -> None:
        self._task(delivery_target="telegram")
        claimed = self.state.claim_due_outbox(limit=1)
        self.state.defer_outbox(claimed[0].id, error="temporary", delay_seconds=0.05)
        with patch("telegram_canonical_bridge.native_delivery._send_text", return_value=(True, "")):
            code = native_delivery_runner.run([
                "--state",
                str(self.state.path),
                "--max-runtime",
                "5",
                "--idle-grace",
                "0.1",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(self.state.pending_delivery_targets(), set())

    def test_prepare_start_is_idempotent_per_tool_call(self) -> None:
        agent_tasks._SETTINGS = agent_tasks.AgentTaskSettings(delivery_target="local")
        values = dict(
            origin_profile="default",
            session_id="main-session",
            turn_id="turn-1",
            tool_call_id="same-call",
        )
        first = agent_tasks.prepare_start(
            self.state, {"target": "operitrace-agent", "message": "test"}, **values
        )
        second = agent_tasks.prepare_start(
            self.state, {"target": "operitrace-agent", "message": "test"}, **values
        )
        self.assertEqual(first["args"]["_bridge_task_id"], second["args"]["_bridge_task_id"])
        self.assertEqual(len(self.state.list_tasks()), 1)

    def test_start_dispatches_secret_via_file_and_tracks_process(self) -> None:
        context = _FakeContext()
        agent_tasks._CTX = context
        agent_tasks._SETTINGS = agent_tasks.AgentTaskSettings(
            delivery_target="local", controller_profiles=("default",)
        )
        task = self._task(status="dispatching", process_id=None)
        with (
            patch.object(agent_tasks, "_state_and_profile", return_value=(self.state, "default")),
            patch.object(
                agent_tasks,
                "available_agents",
                return_value={"operitrace-agent": "網站操作"},
            ),
        ):
            result = json.loads(agent_tasks.agent_task_start(
                {
                    "_bridge_task_id": task.id,
                    "target": "operitrace-agent",
                    "message": "SECRET_SENTINEL_CONTENT",
                },
                task_id="gateway-task",
                session_id=task.origin_session_id,
            ))
        self.assertTrue(result["ok"])
        name, args, kwargs = context.calls[0]
        self.assertEqual(name, "terminal")
        self.assertTrue(args["background"])
        self.assertTrue(args["notify"])
        self.assertNotIn("SECRET_SENTINEL_CONTENT", args["command"])
        self.assertIn("--task-id", args["command"])
        self.assertIn(task.id, args["command"])
        self.assertEqual(kwargs["session_id"], task.origin_session_id)
        self.assertEqual(self.state.task(task.id).process_id, "proc_test1234")

    def test_runner_uses_unique_task_conversation_instead_of_bot_chat(self) -> None:
        task_id = "TCB-20260910-ABC123"
        message = Path(self.temp.name) / "message.txt"
        message.write_text("safe test", encoding="utf-8")
        completed = Mock(returncode=0, stdout="done", stderr="")
        with patch.object(agent_task_runner.subprocess, "run", return_value=completed) as run:
            code = agent_task_runner.run([
                "--target",
                "operitrace-agent",
                "--task-id",
                task_id,
                "--message-file",
                str(message),
                "--lock-root",
                str(Path(self.temp.name) / "locks"),
                "--hermes",
                "hermes",
            ])
        self.assertEqual(code, 0)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("-c") + 1], f"TCB Task {task_id}")
        self.assertNotIn("Bot Chat", argv)
        self.assertEqual(argv[argv.index("--source") + 1], "tool")
        self.assertFalse(message.exists())

    def test_reentered_start_does_not_spawn_duplicate_runner(self) -> None:
        context = _FakeContext()
        agent_tasks._CTX = context
        agent_tasks._SETTINGS = agent_tasks.AgentTaskSettings(
            delivery_target="local", controller_profiles=("default",)
        )
        task = self._task()
        with patch.object(agent_tasks, "_state_and_profile", return_value=(self.state, "default")):
            result = json.loads(agent_tasks.agent_task_start(
                {
                    "_bridge_task_id": task.id,
                    "target": "operitrace-agent",
                    "message": "相同 tool call 重入",
                },
                session_id=task.origin_session_id,
            ))
        self.assertTrue(result["deduplicated"])
        self.assertEqual(context.calls, [])

    def test_cancel_tree_kills_process(self) -> None:
        context = _FakeContext(process_result={"status": "killed"})
        agent_tasks._CTX = context
        task = self._task()
        with patch.object(agent_tasks, "_state_and_profile", return_value=(self.state, "default")):
            result = json.loads(agent_tasks.agent_task_cancel({"task_id": task.id}))
        self.assertTrue(result["ok"])
        self.assertEqual(self.state.task(task.id).status, "cancelled")
        self.assertEqual(context.calls[-1][0], "process_manage")
        self.assertEqual(context.calls[-1][1]["session_id"], "proc_test1234")

    def test_new_instruction_is_inbox_note_not_interrupt(self) -> None:
        task = self._task()
        with patch.object(agent_tasks, "_state_and_profile", return_value=(self.state, "default")):
            result = json.loads(agent_tasks.agent_task_message({
                "task_id": task.id,
                "message": "補充條件",
            }))
        self.assertTrue(result["ok"])
        updated, notes = self.state.read_task_notes(task.id, mark_read=False)
        self.assertEqual([note.text for note in notes], ["補充條件"])
        self.assertNotEqual(updated.status, "cancelled")

    def test_slash_status_is_human_readable(self) -> None:
        rendered = agent_tasks._render_status_command(json.dumps({
            "ok": True,
            "tasks": [{
                "task_id": "TCB-20260910-ABC123",
                "target": "operitrace-agent",
                "status": "running",
                "status_label": "Bot 處理中",
                "progress": "正在安全檢查",
                "worker_started": True,
                "final_observed": False,
                "exit_code": None,
                "pending_notes": 1,
                "evidence": "hook:pre_llm_call",
            }],
        }), detailed=True)
        self.assertIn("📋 TCB-20260910-ABC123｜Bot 處理中", rendered)
        self.assertIn("Worker turn：已啟動", rendered)
        self.assertNotIn('{"ok"', rendered)


if __name__ == "__main__":
    unittest.main()
