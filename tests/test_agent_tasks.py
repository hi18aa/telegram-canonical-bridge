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
            "exit_code": 0,
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
        self.state = BridgeState(Path(self.temp.name) / "bridge.sqlite3")
        self.original_settings = agent_tasks._SETTINGS
        self.original_ctx = agent_tasks._CTX

    def tearDown(self) -> None:
        agent_tasks._SETTINGS = self.original_settings
        agent_tasks._CTX = self.original_ctx
        self.temp.cleanup()

    def _task(self, *, status="dispatched", process_id="proc_test1234"):
        task, _ = self.state.create_task(
            chat_id="native:local",
            origin_profile="default",
            origin_session_id="main-session",
            origin_turn_id="turn-1",
            origin_tool_call_id="call-1",
            target="operitrace-agent",
            queue_outbox=False,
        )
        return self.state.transition_task(
            task.id,
            status=status,
            process_id=process_id,
            progress="runner started",
            evidence="test",
        )

    def test_native_outbox_does_not_leak_to_legacy_adapter(self) -> None:
        task = self._task()
        self.assertEqual(self.state.claim_due_outbox(), [])
        native = self.state.claim_due_native_outbox()
        self.assertEqual([record.task_id for record in native], [task.id])

    def test_local_native_delivery_is_durably_acknowledged(self) -> None:
        self._task()
        self.assertEqual(flush_native_outbox(self.state), {"sent": 1, "failed": 0})
        self.assertEqual(self.state.claim_due_native_outbox(), [])

    def test_single_flush_drains_ordered_events_for_same_task(self) -> None:
        task = self._task()
        self.state.transition_task(
            task.id, status="running", progress="working", evidence="test"
        )
        self.state.transition_task(
            task.id, status="returning", progress="result ready", evidence="test"
        )
        self.state.transition_task(
            task.id, status="completed", progress="done", evidence="test", exit_code=0
        )

        self.assertEqual(flush_native_outbox(self.state), {"sent": 4, "failed": 0})
        self.assertEqual(self.state.claim_due_native_outbox(), [])

    def test_native_delivery_uses_origin_profile_and_public_send_cli(self) -> None:
        task, _ = self.state.create_task(
            chat_id="native:telegram:123",
            origin_profile="controller-two",
            origin_session_id="main-session",
            origin_turn_id="turn-1",
            origin_tool_call_id="call-native",
            target="operitrace-agent",
            queue_outbox=False,
        )
        self.state.transition_task(task.id, status="dispatched", progress="sent", evidence="test")
        completed = Mock(returncode=0, stdout="", stderr="")
        with patch("telegram_canonical_bridge.native_delivery.subprocess.run", return_value=completed) as run:
            self.assertEqual(flush_native_outbox(self.state), {"sent": 1, "failed": 0})
        argv = run.call_args.args[0]
        self.assertEqual(argv[1:5], ["-p", "controller-two", "send", "--to"])
        self.assertEqual(argv[5], "telegram:123")

    def test_remote_delivery_kick_is_non_blocking_background_process(self) -> None:
        task, _ = self.state.create_task(
            chat_id="native:telegram",
            origin_profile="default",
            origin_session_id="main-session",
            origin_turn_id="turn-1",
            origin_tool_call_id="call-kick",
            target="operitrace-agent",
            queue_outbox=False,
        )
        self.state.transition_task(task.id, status="dispatched", progress="sent", evidence="test")
        with patch("telegram_canonical_bridge.native_delivery.subprocess.Popen") as popen:
            self.assertTrue(kick_native_outbox(self.state))
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1:3], ["-m", "telegram_canonical_bridge.native_delivery_runner"])
        self.assertIn(str(self.state.path.resolve()), argv)

    def test_delivery_runner_waits_for_deferred_record_and_drains_it(self) -> None:
        task, _ = self.state.create_task(
            chat_id="native:telegram",
            origin_profile="default",
            origin_session_id="main-session",
            origin_turn_id="turn-1",
            origin_tool_call_id="call-retry",
            target="operitrace-agent",
            queue_outbox=False,
        )
        self.state.transition_task(task.id, status="dispatched", progress="sent", evidence="test")
        claimed = self.state.claim_due_native_outbox(limit=1)
        self.assertEqual(len(claimed), 1)
        self.state.defer_outbox(claimed[0].id, error="temporary", delay_seconds=0.1)
        with patch("telegram_canonical_bridge.native_delivery._send_text", return_value=(True, "")):
            exit_code = native_delivery_runner.run([
                "--state", str(self.state.path),
                "--max-runtime", "5",
                "--idle-grace", "0.1",
            ])
        self.assertEqual(exit_code, 0)
        self.assertEqual(self.state.pending_native_routes(), set())

    def test_prepare_start_is_idempotent_per_tool_call(self) -> None:
        agent_tasks._SETTINGS = agent_tasks.AgentTaskSettings(delivery_target="local")
        first = agent_tasks.prepare_start(
            self.state,
            {"target": "operitrace-agent", "message": "test"},
            origin_profile="default",
            session_id="main-session",
            turn_id="turn-1",
            tool_call_id="same-call",
        )
        second = agent_tasks.prepare_start(
            self.state,
            {"target": "operitrace-agent", "message": "test"},
            origin_profile="default",
            session_id="main-session",
            turn_id="turn-1",
            tool_call_id="same-call",
        )
        self.assertEqual(first["args"]["_bridge_task_id"], second["args"]["_bridge_task_id"])
        self.assertEqual(len(self.state.list_tasks()), 1)

    def test_start_dispatches_tracked_runner_without_message_on_command_line(self) -> None:
        context = _FakeContext()
        agent_tasks._CTX = context
        agent_tasks._SETTINGS = agent_tasks.AgentTaskSettings(
            delivery_target="local", controller_profiles=("default",)
        )
        task = self._task(status="dispatching", process_id=None)
        with (
            patch.object(agent_tasks, "_state_and_profile", return_value=(self.state, "default")),
            patch.object(agent_tasks, "available_agents", return_value={"operitrace-agent": "OT"}),
        ):
            result = json.loads(agent_tasks.agent_task_start(
                {
                    "_bridge_task_id": task.id,
                    "target": "operitrace-agent",
                    "message": "SECRET_SENTINEL_CONTENT",
                },
                task_id="gateway-task",
                session_id="main-session",
            ))
        self.assertTrue(result["ok"])
        name, args, kwargs = context.calls[0]
        self.assertEqual(name, "terminal")
        self.assertTrue(args["background"])
        self.assertTrue(args["notify"])
        self.assertNotIn("SECRET_SENTINEL_CONTENT", args["command"])
        self.assertEqual(kwargs["session_id"], "main-session")
        self.assertEqual(self.state.task(task.id).process_id, "proc_test1234")

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
                    "message": "同一個 tool call 重入",
                },
                session_id="main-session",
            ))
        self.assertTrue(result["deduplicated"])
        self.assertEqual(context.calls, [])

    def test_runner_filters_only_its_known_false_positive_toolset_warning(self) -> None:
        filtered = agent_task_runner._filter_plugin_toolset_startup_warning(
            "Warning: Unknown toolsets: agent_tasks, telegram_canonical_bridge\nreal stderr"
        )
        self.assertEqual(filtered, "real stderr")
        self.assertIn(
            "unrelated",
            agent_task_runner._filter_plugin_toolset_startup_warning(
                "Warning: Unknown toolsets: unrelated"
            ),
        )

    def test_slash_status_is_human_readable_instead_of_json(self) -> None:
        rendered = agent_tasks._render_status_command(json.dumps({
            "ok": True,
            "tasks": [{
                "task_id": "TCB-20260909-ABC123",
                "target": "operitrace-agent",
                "status": "running",
                "status_label": "OT 處理中",
                "progress": "正在安全檢查",
                "worker_started": True,
                "final_observed": False,
                "exit_code": None,
                "pending_notes": 1,
                "evidence": "hook:pre_llm_call",
            }],
        }), detailed=True)
        self.assertIn("📋 TCB-20260909-ABC123｜OT 處理中", rendered)
        self.assertIn("Worker turn：已啟動", rendered)
        self.assertNotIn("{\"ok\"", rendered)

    def test_cancel_tree_kills_process_and_marks_cancelled(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
