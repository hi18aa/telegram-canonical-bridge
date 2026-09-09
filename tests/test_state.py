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

    def test_expired_processing_lease_is_reclaimed(self) -> None:
        self.assertTrue(self.state.record_inbound(
            update_id="lease-1", chat_id="10", user_id="20", message_id="30", text="hello"
        ))
        first = self.state.claim_due_inbound(lease_seconds=0)
        self.assertEqual([record.update_id for record in first], ["lease-1"])
        reclaimed = self.state.claim_due_inbound()
        self.assertEqual([record.update_id for record in reclaimed], ["lease-1"])

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

    def test_task_lifecycle_uses_timeline_events_and_durable_inbox(self) -> None:
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
        self.assertIn("後續進度會以新訊息發布", initial_card[0].content)
        self.state.mark_outbox_sent(initial_card[0].id, "telegram-card-1")
        self.assertEqual(self.state.task(task.id).telegram_message_id, "telegram-card-1")
        self.assertFalse(self.state.clear_task_telegram_message(
            task.id, expected_message_id="different-card"
        ))
        self.assertTrue(self.state.clear_task_telegram_message(
            task.id, expected_message_id="telegram-card-1"
        ))
        self.assertIsNone(self.state.task(task.id).telegram_message_id)
        # 模擬 adapter 重建時間線錨點後，新的 ID 仍會被綁定。
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
        # lifecycle 與 OT 明確里程碑各自保留為不可變時間線事件。
        events = []
        for index in range(1, 4):
            record = self.state.claim_due_outbox()[0]
            events.append(record)
            self.state.mark_outbox_sent(record.id, f"telegram-event-{index}")
            self.assertEqual(
                self.state.task_by_telegram_message(
                    chat_id="10", telegram_message_id=f"telegram-event-{index}"
                ).id,
                task.id,
            )
        self.assertTrue(all(record.kind == "task_status" for record in events))

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
        _late_task, accepted, _detail = self.state.add_task_note(
            task_id=task.id,
            chat_id="10",
            user_id="20",
            telegram_message_id="user-note-before-final",
            text="這則留言來不及在 final 前讀取",
        )
        self.assertTrue(accepted)
        returning = self.state.complete_worker_turn(
            "worker-session",
            "worker-turn",
            assistant_response=f"已完成\n[TCB-TASK:{task.id}] 的公開結果。",
        )
        self.assertEqual(returning.status, "returning")
        self.assertEqual(returning.pending_notes, 0)
        self.assertEqual(returning.progress, "OT 最終回覆：已完成 [task] 的公開結果。")
        self.assertEqual(returning.evidence, "hook:post_llm_call (sanitized final fallback)")
        queued_after_final = self.state.claim_due_outbox()
        missed_notices = [record for record in queued_after_final if record.kind == "notice"]
        self.assertEqual(len(missed_notices), 1)
        self.assertIn("未及讀取 1 則", missed_notices[0].content)
        for record in queued_after_final:
            self.state.mark_outbox_sent(record.id, f"after-final-{record.id}")
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

    def test_typing_activity_tracks_controller_turn_and_stops_after_reply(self) -> None:
        self.assertTrue(self.state.record_inbound(
            update_id="typing-1", chat_id="10", user_id="20", message_id="30", text="請處理"
        ))
        claimed = self.state.claim_due_inbound()
        self.assertEqual(self.state.active_chat_ids(), ["10"])
        self.state.mark_inbound_submitted(claimed[0].id)
        self.assertEqual(self.state.active_chat_ids(), ["10"])

        queued = self.state.record_history(
            root_id="root",
            messages=[AssistantHistoryMessage(key="reply:1", content_digest="digest", text="完成")],
            chat_id="10",
            bootstrap=False,
        )
        self.assertEqual(queued, 1)
        self.assertEqual(self.state.active_chat_ids(), [])
        reply = self.state.claim_due_outbox()[0]
        self.assertEqual(reply.reply_to_message_id, "30")

    def test_task_timeline_preserves_per_task_delivery_order(self) -> None:
        task, _created = self.state.create_task(
            chat_id="10",
            origin_profile="default",
            origin_session_id="controller-session",
            origin_turn_id="controller-turn",
            origin_tool_call_id="ordered-call",
            target="worker",
        )
        self.state.acknowledge_dispatch(
            session_id="controller-session",
            tool_call_id="ordered-call",
            process_id="process-ordered",
        )
        first = self.state.claim_due_outbox(limit=1)[0]
        self.state.defer_outbox(first.id, error="temporary", delay_seconds=60)
        # 第一則仍在 retry 時，同一任務的第二則不得越過它。
        self.assertEqual(self.state.claim_due_outbox(), [])

    def test_legacy_finished_snapshot_migrates_to_unconfirmed(self) -> None:
        task, _created = self.state.create_task(
            chat_id="10",
            origin_profile="default",
            origin_session_id="controller-session",
            origin_turn_id="controller-turn",
            origin_tool_call_id="legacy-finished-call",
            target="worker",
        )
        connection = sqlite3.connect(self.state.path)
        try:
            connection.execute(
                "UPDATE bridge_tasks SET status = 'finished' WHERE task_id = ?",
                (task.id,),
            )
            connection.commit()
        finally:
            connection.close()

        reopened = BridgeState(self.state.path)
        self.assertEqual(reopened.task(task.id).status, "unconfirmed")
        recovered = reopened.bind_worker(
            task.id,
            worker_profile="worker",
            worker_session_id="late-worker-session",
            worker_turn_id="late-worker-turn",
        )
        self.assertEqual(recovered.status, "running")
        self.assertIsNone(recovered.finished_at)

    def test_startup_reconciles_unread_notes_left_on_closed_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "closed-notes.sqlite3"
            state = BridgeState(path)
            task, _created = state.create_task(
                chat_id="10",
                origin_profile="default",
                origin_session_id="controller-session",
                origin_turn_id="controller-turn",
                origin_tool_call_id="closed-note-call",
                target="worker",
            )
            _updated, accepted, _detail = state.add_task_note(
                task_id=task.id,
                chat_id="10",
                user_id="20",
                telegram_message_id="legacy-unread-note",
                text="舊版完成後遺留的未讀留言",
            )
            self.assertTrue(accepted)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE bridge_tasks SET status = 'completed' WHERE task_id = ?",
                    (task.id,),
                )
                connection.commit()
            finally:
                connection.close()

            restarted = BridgeState(path)
            self.assertEqual(restarted.task(task.id).pending_notes, 0)
            notices = [
                record for record in restarted.claim_due_outbox()
                if record.kind == "notice"
            ]
            self.assertEqual(len(notices), 1)
            self.assertIn("未及讀取 1 則", notices[0].content)

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
                inbound_columns = {
                    row[1] for row in check.execute("PRAGMA table_info(inbound)")
                }
            finally:
                check.close()
            self.assertTrue({
                "task_id", "reply_to_message_id", "silent"
            }.issubset(columns))
            self.assertTrue({"responded_at", "response_pending"}.issubset(inbound_columns))


if __name__ == "__main__":
    unittest.main()
