from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from telegram_canonical_bridge.state import BridgeState


class BridgeStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "tasks.sqlite3"
        self.state = BridgeState(self.path)
        self.call_number = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create(self, *, delivery_target: str = "local", session: str = "main-session"):
        self.call_number += 1
        return self.state.create_task(
            delivery_target=delivery_target,
            origin_profile="default",
            origin_session_id=session,
            origin_turn_id=f"turn-{self.call_number}",
            origin_tool_call_id=f"call-{self.call_number}",
            target="operitrace-agent",
            queue_outbox=False,
        )[0]

    def test_fresh_database_contains_only_task_bridge_tables(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
                if not row[0].startswith("sqlite_")
            }
        self.assertEqual(names, {"tasks", "task_events", "task_notes", "outbox"})

    def test_create_is_idempotent_for_same_source_tool_call(self) -> None:
        values = dict(
            delivery_target="local",
            origin_profile="default",
            origin_session_id="same-session",
            origin_turn_id="same-turn",
            origin_tool_call_id="same-call",
            target="operitrace-agent",
            queue_outbox=False,
        )
        first, first_created = self.state.create_task(**values)
        second, second_created = self.state.create_task(**values)
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.state.list_tasks()), 1)

    def test_status_timeline_and_outbox_are_ordered(self) -> None:
        task = self._create(delivery_target="telegram")
        self.state.transition_task(
            task.id, status="dispatched", progress="runner ready", evidence="test:spawn"
        )
        self.state.transition_task(
            task.id, status="running", progress="Bot started", evidence="hook:pre_llm_call"
        )
        self.state.transition_task(
            task.id,
            status="returning",
            progress="public result",
            evidence="hook:post_llm_call",
        )
        self.state.transition_task(
            task.id, status="completed", evidence="process exit 0", exit_code=0
        )

        first = self.state.claim_due_outbox(limit=10)
        self.assertEqual(len(first), 1)
        self.assertIn("已排入 Hermes 背景程序", first[0].content)
        self.state.mark_outbox_sent(first[0].id)
        second = self.state.claim_due_outbox(limit=10)
        self.assertEqual(len(second), 1)
        self.assertIn("Bot 處理中", second[0].content)

    def test_outbox_lease_can_be_retried(self) -> None:
        task = self._create(delivery_target="telegram")
        self.state.transition_task(
            task.id, status="dispatched", progress="ready", evidence="test"
        )
        claimed = self.state.claim_due_outbox(limit=1)
        self.assertEqual(len(claimed), 1)
        attempts = self.state.defer_outbox(
            claimed[0].id, error="temporary", delay_seconds=0
        )
        self.assertEqual(attempts, 1)
        retried = self.state.claim_due_outbox(limit=1)
        self.assertEqual([row.id for row in retried], [claimed[0].id])
        self.assertEqual(retried[0].attempts, 1)

    def test_explicit_result_wins_over_final_fallback(self) -> None:
        task = self._create()
        self.state.bind_worker(
            task.id,
            worker_profile="operitrace-agent",
            worker_session_id="worker-session",
            worker_turn_id="worker-turn",
        )
        self.state.transition_task(
            task.id,
            status="running",
            progress="已完成公開檢查，沒有修改資料。",
            evidence="Bot explicit bridge_task_result",
        )
        completed_turn = self.state.complete_worker_turn(
            "worker-session",
            "worker-turn",
            assistant_response="這個 fallback 不應蓋過明確結果。",
        )
        self.assertEqual(completed_turn.status, "returning")
        self.assertEqual(completed_turn.progress, "已完成公開檢查，沒有修改資料。")

    def test_notes_are_read_at_checkpoint(self) -> None:
        task = self._create()
        updated, accepted, _ = self.state.add_agent_task_note(
            task_id=task.id,
            origin_profile="default",
            text="請再確認一次",
            note_id="note-1",
        )
        self.assertTrue(accepted)
        self.assertEqual(updated.pending_notes, 1)
        after_read, notes = self.state.read_task_notes(task.id, mark_read=True)
        self.assertEqual([note.text for note in notes], ["請再確認一次"])
        self.assertEqual(after_read.pending_notes, 0)

    def test_unread_notes_are_marked_missed_when_turn_closes(self) -> None:
        task = self._create(delivery_target="telegram")
        self.state.add_agent_task_note(
            task_id=task.id,
            origin_profile="default",
            text="太晚送到的補充",
            note_id="note-late",
        )
        closed = self.state.transition_task(
            task.id,
            status="returning",
            progress="Bot already replied",
            evidence="hook:post_llm_call",
        )
        self.assertEqual(closed.pending_notes, 0)
        _task, unread = self.state.read_task_notes(task.id, mark_read=False)
        self.assertEqual(unread, [])
        with closing(sqlite3.connect(self.path)) as connection:
            contents = [row[0] for row in connection.execute("SELECT content FROM outbox")]
        self.assertTrue(any("未及讀取 1 則" in content for content in contents))

    def test_resumable_interruption_preserves_notes_and_claims_once(self) -> None:
        task = self._create(delivery_target="telegram")
        self.state.bind_worker(
            task.id,
            worker_profile="operitrace-agent",
            worker_session_id="worker-session",
            worker_turn_id="worker-turn",
        )
        self.state.add_agent_task_note(
            task_id=task.id,
            origin_profile="default",
            text="只查詢既有 synthetic transaction，不要重做",
            note_id="reconcile-note",
        )
        interrupted = self.state.transition_task(
            task.id,
            status="interrupted",
            progress="原 turn 已中斷；外部結果不明。",
            evidence="synthetic runner: recoverable exit 1",
            exit_code=1,
        )
        self.assertTrue(interrupted.resumable)
        self.assertEqual(interrupted.pending_notes, 1)

        claimed, first, _ = self.state.claim_task_continuation(
            task_id=task.id,
            origin_profile="default",
        )
        claimed_again, second, _ = self.state.claim_task_continuation(
            task_id=task.id,
            origin_profile="default",
        )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(claimed.status, "continuing")
        self.assertEqual(claimed.lifecycle, "active")
        self.assertIsNone(claimed.process_id)
        self.assertIsNone(claimed.worker_session_id)
        self.assertIsNone(claimed.exit_code)
        self.assertEqual(claimed.pending_notes, 1)
        self.assertEqual(claimed_again.status, "continuing")

    def test_legacy_started_worker_failure_is_resumable_without_migration(self) -> None:
        task = self._create(delivery_target="telegram")
        self.state.bind_worker(
            task.id,
            worker_profile="operitrace-agent",
            worker_session_id="legacy-worker-session",
            worker_turn_id="legacy-worker-turn",
        )
        self.state.add_agent_task_note(
            task_id=task.id,
            origin_profile="default",
            text="只查詢既有結果，不要重做",
            note_id="legacy-reconcile-note",
        )
        legacy = self.state.transition_task(
            task.id,
            status="failed",
            progress="v0.6.4 在 final 前把 worker 中斷標為 failed",
            evidence="hook:on_session_end failed",
            last_error="provider server error",
        )

        self.assertEqual(legacy.status, "failed")
        self.assertTrue(legacy.resumable)
        self.assertFalse(legacy.final)
        self.assertEqual(legacy.lifecycle, "resumable")
        self.assertEqual(legacy.pending_notes, 1)

        # 重新開啟既有 ledger 不會改寫 status，也不會把待讀 reconcile 指示
        # 誤標成 missed；只有明確 continuation claim 才改變狀態。
        reopened = BridgeState(self.path)
        unchanged = reopened.task(task.id)
        self.assertEqual(unchanged.status, "failed")
        self.assertEqual(unchanged.pending_notes, 1)
        claimed, should_spawn, _ = reopened.claim_task_continuation(
            task_id=task.id,
            origin_profile="default",
        )
        self.assertTrue(should_spawn)
        self.assertEqual(claimed.status, "continuing")
        self.assertEqual(claimed.pending_notes, 1)

    def test_continuation_does_not_reuse_previous_turn_result(self) -> None:
        task = self._create()
        self.state.bind_worker(
            task.id,
            worker_profile="operitrace-agent",
            worker_session_id="worker-session-old",
            worker_turn_id="worker-turn-old",
        )
        self.state.transition_task(
            task.id,
            status="running",
            progress="前一個 turn 的 provisional result",
            evidence="Bot explicit bridge_task_result",
        )
        self.state.transition_task(
            task.id,
            status="interrupted",
            progress="前一個 turn 中斷",
            evidence="synthetic runner: recoverable exit 1",
            exit_code=1,
        )
        self.state.claim_task_continuation(
            task_id=task.id,
            origin_profile="default",
        )
        self.state.bind_worker(
            task.id,
            worker_profile="operitrace-agent",
            worker_session_id="worker-session-new",
            worker_turn_id="worker-turn-new",
        )
        returning = self.state.complete_worker_turn(
            "worker-session-new",
            "worker-turn-new",
            assistant_response="新的 reconcile final",
        )
        self.assertEqual(returning.status, "returning")
        self.assertEqual(returning.progress, "Bot 最終回覆：新的 reconcile final")

    def test_confirmed_terminal_is_stable_but_unconfirmed_can_recover(self) -> None:
        failed = self._create()
        self.state.transition_task(
            failed.id, status="failed", progress="failed", evidence="test"
        )
        failed_snapshot = self.state.task(failed.id)
        self.assertFalse(failed_snapshot.resumable)
        self.assertTrue(failed_snapshot.final)
        self.assertEqual(failed_snapshot.lifecycle, "terminated")
        still_failed = self.state.transition_task(
            failed.id, status="running", progress="late event", evidence="late"
        )
        self.assertEqual(still_failed.status, "failed")
        self.assertEqual(still_failed.progress, "failed")

        uncertain = self._create(session="other-session")
        self.state.transition_task(
            uncertain.id, status="unconfirmed", progress="unknown", evidence="test"
        )
        recovered = self.state.transition_task(
            uncertain.id, status="completed", progress="confirmed", evidence="late proof"
        )
        self.assertEqual(recovered.status, "completed")
        self.assertEqual(recovered.progress, "confirmed")


if __name__ == "__main__":
    unittest.main()
