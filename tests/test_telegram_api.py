from __future__ import annotations

import asyncio
import unittest
from typing import Any

from telegram_canonical_bridge.telegram_api import TelegramBotApi


class RecordingTelegramApi(TelegramBotApi):
    def __init__(self) -> None:
        super().__init__("test-token")
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _call(
        self, method: str, payload: dict[str, Any], *, timeout_seconds: float
    ) -> Any:
        self.calls.append((method, payload))
        return {"message_id": 42}


class TelegramBotApiTests(unittest.TestCase):
    def test_send_message_supports_reply_and_silent_progress(self) -> None:
        async def scenario() -> None:
            api = RecordingTelegramApi()
            message_id = await api.send_message(
                chat_id="10",
                text="進度",
                reply_to_message_id="30",
                disable_notification=True,
            )

            self.assertEqual(message_id, "42")
            method, payload = api.calls[0]
            self.assertEqual(method, "sendMessage")
            self.assertEqual(payload["reply_parameters"], {"message_id": 30})
            self.assertTrue(payload["disable_notification"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
