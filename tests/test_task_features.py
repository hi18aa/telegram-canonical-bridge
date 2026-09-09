from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_canonical_bridge.config import STATE_PATH_ENV
from telegram_canonical_bridge.state import BridgeState
from telegram_canonical_bridge.task_features import (
    _after_llm,
    _after_tool,
    _before_llm,
    _before_tool,
    _parse_process_completion,
    bridge_task_inbox,
    bridge_task_update,
)
from telegram_canonical_bridge.task_model import extract_task_id


class TaskFeatureTests(unittest.TestCase):
    def _create_dispatched_task(self, state: BridgeState, *, process_id: str) -> str:
        state.bind_active_route(
            controller_profile="default", chat_id="chat-1", user_id="user-1"
        )
        state.set_canonical_binding(
            controller_profile="default",
            root_id="controller-root",
            runtime_id="controller-session",
        )
        directive = _before_tool(
            "message_agent",
            {"target": "operitrace-agent", "message": "診斷任務"},
            session_id="controller-session",
            tool_call_id=f"tool-{process_id}",
            turn_id="controller-turn",
        )
        task_id = extract_task_id(directive["args"]["message"])
        _after_tool(
            "message_agent",
            json.dumps({"status": "sent", "process_id": process_id}),
            session_id="controller-session",
            tool_call_id=f"tool-{process_id}",
            turn_id="controller-turn",
            status="success",
        )
        return task_id

    def test_native_message_agent_is_wrapped_and_worker_can_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.sqlite3"
            with patch.dict(os.environ, {STATE_PATH_ENV: str(path)}):
                state = BridgeState(path)
                state.bind_active_route(
                    controller_profile="default", chat_id="chat-1", user_id="user-1"
                )
                state.set_canonical_binding(
                    controller_profile="default",
                    root_id="controller-root",
                    runtime_id="controller-session",
                )

                directive = _before_tool(
                    "message_agent",
                    {"target": "operitrace-agent", "message": "請執行測試"},
                    session_id="controller-session",
                    tool_call_id="tool-call-1",
                    turn_id="controller-turn",
                )
                self.assertEqual(directive["action"], "modify")
                tracked_message = directive["args"]["message"]
                task_id = extract_task_id(tracked_message)
                self.assertTrue(task_id)
                self.assertIn("請執行測試", tracked_message)

                _after_tool(
                    "message_agent",
                    json.dumps({"status": "sent", "process_id": "process-1"}),
                    session_id="controller-session",
                    tool_call_id="tool-call-1",
                    turn_id="controller-turn",
                    status="success",
                )
                self.assertEqual(state.task(task_id).process_id, "process-1")

                context = _before_llm(
                    session_id="worker-session",
                    turn_id="worker-turn",
                    user_message=f"Message from Controller: {tracked_message}",
                )
                self.assertIn(task_id, context["context"])
                self.assertEqual(state.task(task_id).worker_session_id, "worker-session")

                update = json.loads(bridge_task_update(
                    {
                        "task_id": task_id,
                        "status": "result",
                        "message": "瀏覽器已透過工具成功開啟，正在檢查頁面。",
                    },
                    session_id="worker-session",
                ))
                self.assertTrue(update["ok"])
                self.assertIn("瀏覽器", state.task(task_id).progress)

                _before_tool(
                    "terminal",
                    {"command": "close browser"},
                    session_id="worker-session",
                    tool_call_id="tool-after-explicit-update",
                    turn_id="worker-turn",
                )
                self.assertIn("成功開啟", state.task(task_id).progress)

                state.add_task_note(
                    task_id=task_id,
                    chat_id="chat-1",
                    user_id="user-1",
                    telegram_message_id="note-1",
                    text="請再確認登入帳號",
                )
                inbox = json.loads(bridge_task_inbox(
                    {"task_id": task_id, "acknowledge": True},
                    session_id="worker-session",
                ))
                self.assertEqual(
                    [message["text"] for message in inbox["messages"]],
                    ["請再確認登入帳號"],
                )
                self.assertEqual(state.task(task_id).pending_notes, 0)

                _after_llm(
                    session_id="worker-session",
                    turn_id="worker-turn",
                    assistant_response="這段 fallback 不應覆寫 OT 的明確里程碑。",
                )
                self.assertEqual(state.task(task_id).status, "returning")
                self.assertIn("成功開啟", state.task(task_id).progress)
                self.assertNotIn("fallback", state.task(task_id).progress)

    def test_without_active_telegram_route_message_agent_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.sqlite3"
            with patch.dict(os.environ, {STATE_PATH_ENV: str(path)}):
                self.assertIsNone(_before_tool(
                    "message_agent",
                    {"target": "worker", "message": "原始內容"},
                    session_id="session",
                    tool_call_id="call",
                    turn_id="turn",
                ))

    def test_unrelated_session_is_not_tracked_even_when_a_route_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.sqlite3"
            with patch.dict(os.environ, {STATE_PATH_ENV: str(path)}):
                state = BridgeState(path)
                state.bind_active_route(
                    controller_profile="default", chat_id="chat-1", user_id="user-1"
                )
                state.set_canonical_binding(
                    controller_profile="default",
                    root_id="controller-root",
                    runtime_id="controller-session",
                )

                self.assertIsNone(_before_tool(
                    "message_agent",
                    {"target": "worker", "message": "不屬於 Telegram 的派工"},
                    session_id="unrelated-session",
                    tool_call_id="unrelated-call",
                    turn_id="unrelated-turn",
                ))
                self.assertEqual(state.list_tasks(), [])

    def test_background_completion_failure_exposes_typed_runner_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.sqlite3"
            with patch.dict(os.environ, {STATE_PATH_ENV: str(path)}):
                state = BridgeState(path)
                task_id = self._create_dispatched_task(state, process_id="proc-failed")
                notification = (
                    "[IMPORTANT: Background process proc-failed exited (exit code 1).\n"
                    "Command: hidden\nOutput:\n"
                    '{"error":"Session has a live owner","reason":"target_busy"}]'
                )

                self.assertIsNone(_before_llm(
                    session_id="controller-session",
                    turn_id="completion-turn",
                    user_message=notification,
                ))
                failed = state.task(task_id)
                self.assertEqual(failed.status, "failed")
                self.assertEqual(failed.exit_code, 1)
                self.assertIn("Bot Chat", failed.progress)
                self.assertIsNone(failed.worker_session_id)

    def test_live_delivery_ack_waits_for_real_worker_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.sqlite3"
            with patch.dict(os.environ, {STATE_PATH_ENV: str(path)}):
                state = BridgeState(path)
                task_id = self._create_dispatched_task(state, process_id="proc-queued")
                task = state.task(task_id)
                tracked = f"[TCB-TASK:{task_id}]\n請只回覆完成"
                notification = (
                    "[IMPORTANT: Background process proc-queued completed normally (exit code 0).\n"
                    "Command: hidden\nOutput:\n"
                    "Delivered into @operitrace-agent's open Bot Chat; "
                    "the reply will appear there.]"
                )

                self.assertIsNone(_before_llm(
                    session_id="controller-session",
                    turn_id="completion-turn",
                    user_message=notification,
                ))
                queued = state.task(task_id)
                self.assertEqual(queued.status, "waiting")
                self.assertIn("收件回條", queued.progress)
                self.assertFalse(queued.terminal)

                context = _before_llm(
                    session_id="worker-session",
                    turn_id="worker-turn",
                    user_message=tracked,
                )
                self.assertIn(task_id, context["context"])
                self.assertEqual(state.task(task_id).status, "running")

    def test_completion_reply_can_close_remote_or_hookless_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.sqlite3"
            with patch.dict(os.environ, {STATE_PATH_ENV: str(path)}):
                state = BridgeState(path)
                task_id = self._create_dispatched_task(state, process_id="proc-remote")
                notification = (
                    "[IMPORTANT: Background process proc-remote completed normally (exit code 0).\n"
                    "Command: hidden\nOutput:\n"
                    "Reply from @operitrace-agent on peer 'office':\n"
                    f"[TCB-TASK:{task_id}] 跨機器任務已完成。]"
                )

                parsed = _parse_process_completion(notification)
                self.assertEqual(parsed.process_id, "proc-remote")
                self.assertEqual(parsed.exit_code, 0)
                self.assertIsNone(_before_llm(
                    session_id="controller-session",
                    turn_id="completion-turn",
                    user_message=notification,
                ))
                completed = state.task(task_id)
                self.assertEqual(completed.status, "completed")
                self.assertEqual(completed.exit_code, 0)
                self.assertIn("跨機器任務已完成", completed.progress)
                self.assertNotIn("TCB-TASK", completed.progress)
                # 完成通知 output 即使含 marker，也不能把 Controller 誤綁成 OT。
                self.assertIsNone(completed.worker_session_id)

    def test_queued_ack_without_process_handle_stays_honestly_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.sqlite3"
            with patch.dict(os.environ, {STATE_PATH_ENV: str(path)}):
                state = BridgeState(path)
                state.bind_active_route(
                    controller_profile="default", chat_id="chat-1", user_id="user-1"
                )
                state.set_canonical_binding(
                    controller_profile="default",
                    root_id="controller-root",
                    runtime_id="controller-session",
                )
                directive = _before_tool(
                    "message_agent",
                    {"target": "operitrace-agent", "message": "新版收件測試"},
                    session_id="controller-session",
                    tool_call_id="tool-queued-no-process",
                    turn_id="controller-turn",
                )
                task_id = extract_task_id(directive["args"]["message"])

                _after_tool(
                    "message_agent",
                    json.dumps({"status": "queued", "delivery_id": "delivery-1"}),
                    session_id="controller-session",
                    tool_call_id="tool-queued-no-process",
                    turn_id="controller-turn",
                    status="success",
                )
                task = state.task(task_id)
                self.assertEqual(task.status, "waiting")
                self.assertIsNone(task.process_id)
                self.assertIn("尚未觀察 OT turn", task.progress)


if __name__ == "__main__":
    unittest.main()
