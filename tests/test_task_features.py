from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_canonical_bridge import agent_tasks, task_features
from telegram_canonical_bridge.state import BridgeState
from telegram_canonical_bridge.task_model import task_marker


class TaskFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = BridgeState(Path(self.temp.name) / "tasks.sqlite3")
        self.patches = [
            patch.object(task_features, "_state", return_value=self.state),
            patch.object(task_features, "kick_native_outbox", return_value=True),
        ]
        for active in self.patches:
            active.start()
        self.original_settings = agent_tasks._SETTINGS
        agent_tasks._SETTINGS = agent_tasks.AgentTaskSettings(delivery_target="local")
        self.call_number = 0

    def tearDown(self) -> None:
        agent_tasks._SETTINGS = self.original_settings
        for active in reversed(self.patches):
            active.stop()
        self.temp.cleanup()

    def _dispatched(self, *, process_id: str = "proc-test"):
        self.call_number += 1
        task, _ = self.state.create_task(
            delivery_target="local",
            origin_profile="default",
            origin_session_id=f"controller-session-{self.call_number}",
            origin_turn_id=f"controller-turn-{self.call_number}",
            origin_tool_call_id=f"controller-call-{self.call_number}",
            target="operitrace-agent",
            queue_outbox=False,
        )
        return self.state.transition_task(
            task.id,
            status="dispatched",
            process_id=process_id,
            progress="runner ready",
            evidence="agent_task_start background acknowledgement",
        )

    def test_message_agent_is_never_wrapped_or_modified(self) -> None:
        before = len(self.state.list_tasks())
        result = task_features._before_tool(
            "message_agent",
            {"target": "worker", "message": "原始內容"},
            session_id="session",
            tool_call_id="call",
            turn_id="turn",
        )
        self.assertIsNone(result)
        self.assertEqual(len(self.state.list_tasks()), before)

    def test_agent_task_start_pre_hook_creates_only_an_idempotent_intent(self) -> None:
        with patch.object(task_features, "_current_profile", return_value="default"):
            first = task_features._before_tool(
                "agent_task_start",
                {"target": "operitrace-agent", "message": "診斷任務"},
                session_id="controller-session",
                tool_call_id="same-call",
                turn_id="controller-turn",
            )
            second = task_features._before_tool(
                "agent_task_start",
                {"target": "operitrace-agent", "message": "診斷任務"},
                session_id="controller-session",
                tool_call_id="same-call",
                turn_id="controller-turn",
            )
        self.assertEqual(first["action"], "modify")
        self.assertEqual(first["args"], second["args"])
        self.assertEqual(set(first["args"]), {"_bridge_task_id"})
        self.assertEqual(len(self.state.list_tasks()), 1)

    def test_only_target_profile_can_bind_worker_turn(self) -> None:
        task = self._dispatched()
        payload = f"{task_marker(task.id)}\n請執行安全測試"
        with patch.object(task_features, "_current_profile", return_value="default"):
            self.assertIsNone(task_features._before_llm(
                session_id="wrong-session",
                turn_id="wrong-turn",
                user_message=payload,
            ))
        self.assertIsNone(self.state.task(task.id).worker_session_id)

        with patch.object(task_features, "_current_profile", return_value="operitrace-agent"):
            injected = task_features._before_llm(
                session_id="worker-session",
                turn_id="worker-turn",
                user_message=payload,
            )
        self.assertIsNone(injected)
        self.assertEqual(self.state.task(task.id).worker_session_id, "worker-session")

    def test_worker_binding_does_not_inject_control_text_after_task_payload(self) -> None:
        task = self._dispatched()
        clean_end = "保留換行、兩個空格  與星星 ⭐⭐"
        payload = f"{task_marker(task.id)}\n{clean_end}"
        with patch.object(task_features, "_current_profile", return_value="operitrace-agent"):
            injected = task_features._before_llm(
                session_id="worker-session",
                turn_id="worker-turn",
                user_message=payload,
            )
        self.assertIsNone(injected)
        self.assertEqual(self.state.task(task.id).worker_session_id, "worker-session")

    def test_worker_progress_inbox_and_final_result_form_a_closed_loop(self) -> None:
        task = self._dispatched(process_id="proc-closed-loop")
        with patch.object(task_features, "_current_profile", return_value="operitrace-agent"):
            task_features._before_llm(
                session_id="worker-session",
                turn_id="worker-turn",
                user_message=f"{task_marker(task.id)}\n請檢查",
            )
        progress = json.loads(task_features.bridge_task_update(
            {
                "task_id": task.id,
                "status": "working",
                "message": "已啟動瀏覽器工具，正在讀取公開頁面。",
            },
            session_id="worker-session",
        ))
        self.assertTrue(progress["ok"])
        self.assertEqual(
            self.state.task(task.id).evidence, "Bot explicit bridge_task_update"
        )

        self.state.add_agent_task_note(
            task_id=task.id,
            origin_profile="default",
            text="不要登入",
            note_id="note-1",
        )
        inbox = json.loads(task_features.bridge_task_inbox(
            {"task_id": task.id, "acknowledge": True},
            session_id="worker-session",
        ))
        self.assertEqual([item["text"] for item in inbox["messages"]], ["不要登入"])

        result = json.loads(task_features.bridge_task_update(
            {
                "task_id": task.id,
                "status": "result",
                "message": "公開頁面檢查完成，未登入也未修改資料。",
            },
            session_id="worker-session",
        ))
        self.assertTrue(result["ok"])
        task_features._after_llm(
            session_id="worker-session",
            turn_id="worker-turn",
            assistant_response="fallback response",
        )
        returning = self.state.task(task.id)
        self.assertEqual(returning.status, "returning")
        self.assertEqual(returning.progress, "公開頁面檢查完成，未登入也未修改資料。")

        notification = (
            "[IMPORTANT: Background process proc-closed-loop completed normally (exit code 0).\n"
            "Command: hidden\nOutput:\nworker final]"
        )
        with patch.object(task_features, "_current_profile", return_value="default"):
            self.assertIsNone(task_features._before_llm(
                session_id=task.origin_session_id,
                turn_id="completion-turn",
                user_message=notification,
            ))
        completed = self.state.task(task.id)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.exit_code, 0)

    def test_explicit_progress_is_not_overwritten_by_generic_tool_activity(self) -> None:
        task = self._dispatched()
        self.state.bind_worker(
            task.id,
            worker_profile="operitrace-agent",
            worker_session_id="worker-session",
            worker_turn_id="worker-turn",
        )
        self.state.transition_task(
            task.id,
            status="running",
            progress="具體里程碑",
            evidence="Bot explicit bridge_task_update",
        )
        task_features._before_tool(
            "terminal",
            {"command": "ignored"},
            session_id="worker-session",
            tool_call_id="tool-call",
            turn_id="worker-turn",
        )
        self.assertEqual(self.state.task(task.id).progress, "具體里程碑")

    def test_typed_process_failure_is_reported_without_command_output(self) -> None:
        task = self._dispatched(process_id="proc-failed")
        notification = (
            "[IMPORTANT: Background process proc-failed exited (exit code 1).\n"
            "Command: SECRET COMMAND\nOutput:\n"
            '{"error":"provider denied","reason":"provider_auth_or_access"}]'
        )
        parsed = task_features._parse_process_completion(notification)
        self.assertEqual(parsed.process_id, "proc-failed")
        with patch.object(task_features, "_current_profile", return_value="default"):
            task_features._before_llm(
                session_id=task.origin_session_id,
                turn_id="completion-turn",
                user_message=notification,
            )
        failed = self.state.task(task.id)
        self.assertEqual(failed.status, "failed")
        self.assertIn("provider", failed.progress)
        self.assertNotIn("SECRET COMMAND", failed.progress)

    def test_hookless_final_output_can_complete_task(self) -> None:
        task = self._dispatched(process_id="proc-hookless")
        notification = (
            "[IMPORTANT: Background process proc-hookless completed normally (exit code 0).\n"
            "Command: hidden\nOutput:\n"
            f"{task_marker(task.id)} 公開檢查已完成。]"
        )
        task_features._before_llm(
            session_id=task.origin_session_id,
            turn_id="completion-turn",
            user_message=notification,
        )
        completed = self.state.task(task.id)
        self.assertEqual(completed.status, "completed")
        self.assertIn("公開檢查已完成", completed.progress)
        self.assertNotIn("TCB-TASK", completed.progress)
        self.assertIsNone(completed.worker_session_id)

    def test_empty_success_without_final_evidence_is_unconfirmed(self) -> None:
        task = self._dispatched(process_id="proc-empty")
        notification = (
            "[IMPORTANT: Background process proc-empty completed normally (exit code 0).\n"
            "Command: hidden\nOutput:\n(empty reply)]"
        )
        task_features._before_llm(
            session_id=task.origin_session_id,
            turn_id="completion-turn",
            user_message=notification,
        )
        uncertain = self.state.task(task.id)
        self.assertEqual(uncertain.status, "unconfirmed")
        self.assertIn("無法宣稱任務完成", uncertain.progress)


if __name__ == "__main__":
    unittest.main()
