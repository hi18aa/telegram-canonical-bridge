from __future__ import annotations

import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
