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
    bridge_task_inbox,
    bridge_task_update,
)
from telegram_canonical_bridge.task_model import extract_task_id


class TaskFeatureTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
