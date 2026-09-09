from __future__ import annotations

import unittest

from telegram_canonical_bridge.protocol import (
    extract_assistant_history,
    split_telegram_text,
    telegram_text_units,
)


class ProtocolTests(unittest.TestCase):
    def test_split_respects_telegram_utf16_limit(self) -> None:
        text = "🙂" * 3_000
        chunks = split_telegram_text(text)

        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(telegram_text_units(chunk) <= 4_000 for chunk in chunks))

    def test_extract_assistant_history_handles_structured_content(self) -> None:
        messages = [
            {"id": "u1", "role": "user", "content": "不要回傳"},
            {
                "_row_id": 42,
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "第一段"},
                    {"type": "image", "url": "ignored"},
                    {"type": "output_text", "text": "第二段"},
                ],
            },
        ]

        extracted = extract_assistant_history(messages)

        self.assertEqual(len(extracted), 1)
        self.assertEqual(extracted[0].key, "row:42")
        self.assertEqual(extracted[0].text, "第一段\n第二段")

    def test_extract_assistant_history_accepts_current_flat_text_shape(self) -> None:
        extracted = extract_assistant_history([
            {"row_id": 7, "role": "assistant", "text": "目前 Hermes 的回覆格式"},
        ])

        self.assertEqual([(item.key, item.text) for item in extracted], [
            ("row:7", "目前 Hermes 的回覆格式"),
        ])


if __name__ == "__main__":
    unittest.main()
