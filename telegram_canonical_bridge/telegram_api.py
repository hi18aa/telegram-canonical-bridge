"""只使用 Python 標準函式庫的 Telegram Bot API client。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class TelegramApiError(RuntimeError):
    message: str
    error_code: int | None = None

    def __str__(self) -> str:
        return self.message


class TelegramBotApi:
    """長輪詢與純文字送信所需的最小 Telegram Bot API 面積。"""

    def __init__(self, token: str) -> None:
        self._token = token

    async def get_me(self) -> dict[str, Any]:
        result = await self._call("getMe", {}, timeout_seconds=20)
        return result if isinstance(result, dict) else {}

    async def get_updates(self, *, offset: int | None, timeout_seconds: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout_seconds,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self._call("getUpdates", payload, timeout_seconds=timeout_seconds + 15)
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    async def send_message(self, *, chat_id: str, text: str, reply_to_message_id: str | None = None) -> str:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id:
            payload["reply_parameters"] = {"message_id": int(reply_to_message_id)}
        result = await self._call("sendMessage", payload, timeout_seconds=25)
        if not isinstance(result, dict) or result.get("message_id") is None:
            raise TelegramApiError("Telegram sendMessage 沒有回傳 message_id。")
        return str(result["message_id"])

    async def edit_message_text(self, *, chat_id: str, message_id: str, text: str) -> str:
        try:
            result = await self._call(
                "editMessageText",
                {"chat_id": chat_id, "message_id": int(message_id), "text": text},
                timeout_seconds=25,
            )
        except TelegramApiError as exc:
            if exc.error_code == 400 and "message is not modified" in exc.message.lower():
                return str(message_id)
            raise
        if isinstance(result, dict) and result.get("message_id") is not None:
            return str(result["message_id"])
        if result is True:
            return str(message_id)
        raise TelegramApiError("Telegram editMessageText 沒有回傳 Message。")

    async def send_typing(self, *, chat_id: str) -> None:
        await self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout_seconds=15)

    async def _call(self, method: str, payload: dict[str, Any], *, timeout_seconds: float) -> Any:
        return await asyncio.to_thread(self._call_sync, method, payload, timeout_seconds)

    def _call_sync(self, method: str, payload: dict[str, Any], timeout_seconds: float) -> Any:
        endpoint = f"https://api.telegram.org/bot{self._token}/{method}"
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "telegram-canonical-bridge/0.2"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = self._error_detail(exc.read())
            raise TelegramApiError(detail or f"Telegram HTTP {exc.code}", error_code=exc.code) from exc
        except URLError as exc:
            raise TelegramApiError(f"Telegram 網路錯誤：{exc.reason}") from exc
        except TimeoutError as exc:
            raise TelegramApiError("Telegram API 逾時。") from exc
        except Exception as exc:
            raise TelegramApiError(f"Telegram API 呼叫失敗：{type(exc).__name__}: {exc}") from exc

        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelegramApiError("Telegram API 回傳非 JSON 資料。") from exc
        if not isinstance(envelope, dict):
            raise TelegramApiError("Telegram API 回傳格式無效。")
        if not envelope.get("ok"):
            raise TelegramApiError(
                str(envelope.get("description") or "Telegram API 拒絕請求。"),
                error_code=envelope.get("error_code") if isinstance(envelope.get("error_code"), int) else None,
            )
        return envelope.get("result")

    @staticmethod
    def _error_detail(raw: bytes) -> str:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ""
        if isinstance(payload, dict):
            return str(payload.get("description") or "")
        return ""
