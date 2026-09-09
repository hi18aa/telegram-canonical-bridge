from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from telegram_canonical_bridge.protocol import AssistantHistoryMessage, telegram_text_units
from telegram_canonical_bridge.state import BridgeState


class BridgeStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state = BridgeState(Path(self.temp_dir.name) / "bridge.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_inbound_is_deduplicated_and_route_is_single_owner(self) -> None:
        self.assertTrue(self.state.record_inbound(
            update_id="100", chat_id="10", user_id="20", message_id="30", text="hello"
        ))
        self.assertFalse(self.state.record_inbound(
            update_id="100", chat_id="10", user_id="20", message_id="30", text="hello again"
        ))
        claimed = self.state.claim_due_inbound()
        self.assertEqual([record.update_id for record in claimed], ["100"])
        self.assertTrue(self.state.bind_active_route(controller_profile="default", chat_id="10", user_id="20"))
        self.assertFalse(self.state.bind_active_route(controller_profile="default", chat_id="11", user_id="21"))

    def test_bootstrap_does_not_replay_old_history_and_new_output_is_chunked(self) -> None:
        old = AssistantHistoryMessage(key="row:1", content_digest="old", text="舊回覆")
        self.assertEqual(self.state.record_history(
            root_id="root", messages=[old], chat_id="10", bootstrap=True
        ), 0)
        self.assertEqual(self.state.claim_due_outbox(), [])

        long_text = "🙂" * 2_500
        new = AssistantHistoryMessage(key="row:2", content_digest="new", text=long_text)
        queued = self.state.record_history(root_id="root", messages=[old, new], chat_id="10", bootstrap=False)
        records = self.state.claim_due_outbox()

        self.assertEqual(queued, len(records))
        self.assertGreater(len(records), 1)
        self.assertEqual("".join(record.content for record in records), long_text)
        self.assertTrue(all(telegram_text_units(record.content) <= 4_000 for record in records))

    def test_outbox_failure_returns_to_retry_queue(self) -> None:
        self.assertTrue(self.state.enqueue_notice(chat_id="10", dedup_key="notice", content="稍後重試"))
        record = self.state.claim_due_outbox()[0]
        self.assertEqual(self.state.defer_outbox(record.id, error="network", delay_seconds=0), 1)
        retried = self.state.claim_due_outbox()[0]
        self.state.mark_outbox_sent(retried.id, "999")
        self.assertEqual(self.state.counts()["pending_outbox"], 0)

    def test_task_lifecycle_uses_one_editable_status_card_and_durable_inbox(self) -> None:
        self.assertTrue(self.state.bind_active_route(
            controller_profile="default", chat_id="10", user_id="20"
        ))
        task, created = self.state.create_task(
            chat_id="10",
            origin_profile="default",
            origin_session_id="controller-session",
            origin_turn_id="controller-turn",
            origin_tool_call_id="tool-call-1",
            target="operitrace-agent",
        )
        self.assertTrue(created)
        duplicate, created_again = self.state.create_task(
            chat_id="10",
            origin_profile="default",
            origin_session_id="controller-session",
            origin_turn_id="controller-turn",
            origin_tool_call_id="tool-call-1",
            target="operitrace-agent",
        )
        self.assertFalse(created_again)
        self.assertEqual(duplicate.id, task.id)

        initial_card = self.state.claim_due_outbox()
        self.assertEqual(len(initial_card), 1)
        self.assertEqual(initial_card[0].kind, "task_status")
        self.assertEqual(initial_card[0].task_id, task.id)
        self.state.mark_outbox_sent(initial_card[0].id, "telegram-card-1")
        self.assertEqual(self.state.task(task.id).telegram_message_id, "telegram-card-1")
        self.assertFalse(self.state.clear_task_telegram_message(
            task.id, expected_message_id="different-card"
        ))
        self.assertTrue(self.state.clear_task_telegram_message(
            task.id, expected_message_id="telegram-card-1"
        ))
        self.assertIsNone(self.state.task(task.id).telegram_message_id)
        # 模擬 adapter 重建狀態卡後，新的 ID 仍會被綁定。
        self.state.mark_outbox_sent(initial_card[0].id, "telegram-card-2")
        self.assertEqual(self.state.task(task.id).telegram_message_id, "telegram-card-2")

        acknowledged = self.state.acknowledge_dispatch(
            session_id="controller-session",
            tool_call_id="tool-call-1",
            process_id="process-1",
        )
        self.assertEqual(acknowledged.status, "dispatched")
        worker = self.state.bind_worker(
            task.id,
            worker_profile="operitrace-agent",
            worker_session_id="worker-session",
            worker_turn_id="worker-turn",
        )
        self.assertEqual(worker.status, "running")
        self.state.transition_task(
            task.id,
            status="running",
            progress="OT 已回報一般進度，但這不是最終結果。",
            evidence="OT explicit bridge_task_update",
        )
        # 兩個快速 revision 應合併成一筆待 edit outbox。
        edits = self.state.claim_due_outbox()
        self.assertEqual(len(edits), 1)
        self.state.mark_outbox_sent(edits[0].id, "telegram-card-1")

        updated, accepted, _detail = self.state.add_task_note(
            task_id=task.id,
            chat_id="10",
            user_id="20",
            telegram_message_id="user-note-1",
            text="請先確認登入狀態",
        )
        self.assertTrue(accepted)
        self.assertEqual(updated.pending_notes, 1)
        self.assertEqual(
            self.state.task_by_telegram_message(
                chat_id="10", telegram_message_id="telegram-card-2"
            ).id,
            task.id,
        )

        inbox_task, notes = self.state.read_task_notes(task.id, mark_read=True)
        self.assertEqual([note.text for note in notes], ["請先確認登入狀態"])
        self.assertEqual(inbox_task.pending_notes, 0)
        returning = self.state.complete_worker_turn(
            "worker-session",
            "worker-turn",
            assistant_response=f"已完成\n[TCB-TASK:{task.id}] 的公開結果。",
        )
        self.assertEqual(returning.status, "returning")
        self.assertEqual(returning.progress, "OT 最終回覆：已完成 [task] 的公開結果。")
        self.assertEqual(returning.evidence, "hook:post_llm_call (sanitized final fallback)")
        _returning, accepted, detail = self.state.add_task_note(
            task_id=task.id,
            chat_id="10",
            user_id="20",
            telegram_message_id="user-note-returning",
            text="已來不及納入的留言",
        )
        self.assertFalse(accepted)
        self.assertIn("已產生最終回覆", detail)
        completed = self.state.observe_process(
            task.id, process_status="exited", exit_code=0
        )
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.exit_code, 0)
        self.assertEqual(completed.progress, returning.progress)

        terminal, accepted, detail = self.state.add_task_note(
            task_id=task.id,
            chat_id="10",
            user_id="20",
            telegram_message_id="user-note-2",
            text="太晚的留言",
        )
        self.assertFalse(accepted)
        self.assertTrue(terminal.terminal)
        self.assertIn("已結束", detail)

    def test_existing_v1_outbox_is_migrated_with_task_id_column(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE outbox ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, "
                "dedup_key TEXT NOT NULL UNIQUE, content TEXT NOT NULL, kind TEXT NOT NULL, "
                "status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
                "next_attempt_at REAL NOT NULL DEFAULT 0, lease_until REAL, last_error TEXT, "
                "telegram_message_id TEXT, created_at REAL NOT NULL, sent_at REAL)"
            )
            connection.commit()
            connection.close()

            migrated = BridgeState(path)
            check = sqlite3.connect(path)
            try:
                columns = {row[1] for row in check.execute("PRAGMA table_info(outbox)")}
            finally:
                check.close()
            self.assertIn("task_id", columns)


if __name__ == "__main__":
    unittest.main()
