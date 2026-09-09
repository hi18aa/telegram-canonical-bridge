"""Telegram 與 Hermes 之間的資料正規化。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable


TELEGRAM_SAFE_TEXT_LIMIT = 4000


@dataclass(frozen=True)
class AssistantHistoryMessage:
    """一筆已正規化、可去重的 canonical assistant 訊息。"""

    key: str
    content_digest: str
    text: str


def telegram_text_units(text: str) -> int:
    """Telegram 的文字長度採 UTF-16 code unit；emoji 可能占兩個單位。"""

    return len(text.encode("utf-16-le")) // 2


def _prefix_within_telegram_limit(text: str, limit: int) -> str:
    """取得不超過 UTF-16 預算的最長字首，且不切斷 Python code point。"""

    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if telegram_text_units(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def split_telegram_text(text: str, *, limit: int = TELEGRAM_SAFE_TEXT_LIMIT) -> list[str]:
    """在 Telegram 4096 UTF-16 單位限制前，優先於段落與空白切割。"""

    value = str(text or "")
    if not value:
        return []
    if limit < 32:
        raise ValueError("limit 過小，無法安全切割 Telegram 訊息。")

    chunks: list[str] = []
    remaining = value
    while telegram_text_units(remaining) > limit:
        candidate = _prefix_within_telegram_limit(remaining, limit)
        split_at = max(candidate.rfind("\n"), candidate.rfind(" "))
        if split_at < max(1, len(candidate) // 3):
            split_at = len(candidate)
        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = candidate
            split_at = len(chunk)
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip("\n ")
    if remaining:
        chunks.append(remaining)
    return chunks


def content_to_text(content: Any) -> str:
    """容忍 Hermes 目前與未來常見的訊息 content 形狀。"""

    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        for key in ("text", "content", "value"):
            if key in content:
                return content_to_text(content[key])
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") not in {None, "text", "output_text"}:
                continue
            text = content_to_text(item)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()
    return ""


def _stable_message_id(message: dict[str, Any]) -> str | None:
    for field in ("_row_id", "row_id", "id", "message_id"):
        value = message.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def extract_assistant_history(messages: Iterable[Any]) -> list[AssistantHistoryMessage]:
    """從 ``session.history`` 取出可傳給 Telegram 的 assistant 結果。"""

    extracted: list[AssistantHistoryMessage] = []
    fallback_occurrences: dict[str, int] = {}
    for message in messages:
        if not isinstance(message, dict) or str(message.get("role") or "").lower() != "assistant":
            continue
        # Hermes 0.21.x 的 ``session.history`` 使用扁平 ``text``；部分較新
        # transport 則使用 OpenAI 風格 ``content``。兩者皆需保留，避免升級時
        # background completion 被悄悄忽略。
        raw_content = message.get("content")
        if raw_content is None:
            raw_content = message.get("text")
        text = content_to_text(raw_content)
        if not text:
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        row_id = _stable_message_id(message)
        if row_id is not None:
            key = f"row:{row_id}"
        else:
            occurrence = fallback_occurrences.get(digest, 0)
            fallback_occurrences[digest] = occurrence + 1
            key = f"content:{digest}:{occurrence}"
        extracted.append(AssistantHistoryMessage(key=key, content_digest=digest, text=text))
    return extracted
