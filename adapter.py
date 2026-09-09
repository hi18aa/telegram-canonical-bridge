"""Hermes 平台外掛適配器。

這個 adapter 刻意不把 Telegram update 送入 ``BasePlatformAdapter.handle_message``。
後者會建立一般平台 session；本外掛唯一的輸入目標是既存的 canonical
``Bot Chat``，如此才保留 Hermes 原生的 Bot Mode 與 ``message_agent``。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from typing import Any, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

from .telegram_canonical_bridge.config import (
    BACKEND_TOKEN_ENV,
    BOT_TOKEN_ENV,
    BridgeConfig,
    BridgeConfigurationError,
    check_secrets_present,
)
from .telegram_canonical_bridge.protocol import split_telegram_text, telegram_text_units
from .telegram_canonical_bridge.service import CanonicalBridgeService
from .telegram_canonical_bridge.state import BridgeState
from .telegram_canonical_bridge.task_model import (
    normalize_task_id,
    render_task_card,
    render_task_list,
)
from .telegram_canonical_bridge.telegram_api import TelegramApiError, TelegramBotApi


logger = logging.getLogger(__name__)

PLATFORM_NAME = "telegram_canonical_bridge"
LOCK_SCOPE = "telegram-canonical-bridge"
UPDATE_OFFSET_META_KEY = "telegram-update-offset"


class TelegramCanonicalBridgeAdapter(BasePlatformAdapter):
    """Telegram 私訊與單一 Controller canonical Bot Chat 之間的耐久橋接器。"""

    MAX_MESSAGE_LENGTH = 4000
    splits_long_messages = True
    supports_async_delivery = True

    def __init__(self, config: PlatformConfig) -> None:
        super().__init__(config, Platform(PLATFORM_NAME))
        self.bridge_config = BridgeConfig.from_platform_config(config)
        self._state = BridgeState(self.bridge_config.state_path)
        self._service = CanonicalBridgeService(self.bridge_config, self._state)
        self._telegram = TelegramBotApi(self.bridge_config.bot_token)
        self._tasks: set[asyncio.Task[None]] = set()
        self._poll_confirmed = False
        self._identity = hashlib.sha256(self.bridge_config.bot_token.encode("utf-8")).hexdigest()

    @property
    def message_len_fn(self):
        return telegram_text_units

    @property
    def send_path_degraded(self) -> bool:
        """直到成功完成一次 getUpdates，才宣告 Telegram 收訊路徑健康。"""

        return not self._poll_confirmed

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self._running:
            return True
        if not self._acquire_platform_lock(LOCK_SCOPE, self._identity, "Telegram Bot token"):
            return False
        try:
            identity = await self._telegram.get_me()
        except TelegramApiError as exc:
            self._release_platform_lock()
            retryable = exc.error_code not in {401, 404}
            self._set_fatal_error("telegram_auth", f"Telegram Bot 驗證失敗：{exc}", retryable=retryable)
            logger.error("[%s] Telegram getMe failed: %s", PLATFORM_NAME, exc)
            return False
        except Exception as exc:
            self._release_platform_lock()
            self._set_fatal_error("telegram_connect", f"Telegram 連線失敗：{exc}", retryable=True)
            logger.exception("[%s] Telegram getMe failed", PLATFORM_NAME)
            return False

        username = str(identity.get("username") or identity.get("id") or "unknown")
        logger.info("[%s] Connected to Telegram bot %s", PLATFORM_NAME, username)
        self._mark_connected()
        self._start_task(self._poll_updates_loop(), "telegram-canonical-bridge-poll")
        self._start_task(self._bridge_tick_loop(), "telegram-canonical-bridge-history")
        self._start_task(self._outbox_loop(), "telegram-canonical-bridge-outbox")
        # Hermes backend 暫時不可達時，輸入仍會先保存在 SQLite，故不阻止 Telegram 啟動。
        await self._service.startup_probe()
        return True

    async def disconnect(self) -> None:
        self._running = False
        await self._cancel_tasks()
        self._release_platform_lock()
        self._mark_disconnected()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """提供 Hermes 標準送信介面；bridge 自己的回覆仍走 durable outbox。"""

        chunks = split_telegram_text(content)
        if not chunks:
            return SendResult(success=True)
        message_ids: list[str] = []
        try:
            for index, chunk in enumerate(chunks):
                message_ids.append(await self._telegram.send_message(
                    chat_id=str(chat_id),
                    text=chunk,
                    reply_to_message_id=reply_to if index == 0 else None,
                ))
        except TelegramApiError as exc:
            return SendResult(
                success=False,
                error=str(exc),
                retryable=exc.error_code not in {400, 401, 403, 404},
            )
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        return SendResult(
            success=True,
            message_id=message_ids[-1],
            continuation_message_ids=tuple(message_ids),
        )

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": str(chat_id), "type": "dm"}

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        with contextlib.suppress(Exception):
            await self._telegram.send_typing(chat_id=str(chat_id))

    def _start_task(self, coroutine: Any, name: str) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _cancel_tasks(self) -> None:
        current_task = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current_task and not task.done()]
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_updates_loop(self) -> None:
        retry_delay = self.bridge_config.retry_base_seconds
        while self._running:
            try:
                offset_raw = self._state.get_meta(UPDATE_OFFSET_META_KEY)
                offset = int(offset_raw) if offset_raw and offset_raw.lstrip("-").isdigit() else None
                updates = await self._telegram.get_updates(
                    offset=offset,
                    timeout_seconds=self.bridge_config.telegram_poll_timeout_seconds,
                )
                retry_delay = self.bridge_config.retry_base_seconds
                if not self._poll_confirmed:
                    self._poll_confirmed = True
                    self._mark_connected()
                for update in updates:
                    update_id = update.get("update_id")
                    if not isinstance(update_id, int):
                        continue
                    # 成功持久化、忽略，或已去重後才推進 offset；處理拋錯則保留原 offset。
                    await self._handle_update(update, update_id)
                    self._state.set_meta(UPDATE_OFFSET_META_KEY, str(update_id + 1))
            except asyncio.CancelledError:
                raise
            except TelegramApiError as exc:
                if exc.error_code in {401, 404}:
                    await self._fatal("telegram_auth", f"Telegram Bot 已失效：{exc}", retryable=False)
                    return
                logger.warning("[%s] Telegram getUpdates failed: %s", PLATFORM_NAME, exc)
                await asyncio.sleep(retry_delay)
                retry_delay = min(self.bridge_config.retry_max_seconds, retry_delay * 2)
            except Exception as exc:
                logger.exception("[%s] Telegram polling failed", PLATFORM_NAME)
                await asyncio.sleep(retry_delay)
                retry_delay = min(self.bridge_config.retry_max_seconds, retry_delay * 2)

    async def _handle_update(self, update: dict[str, Any], update_id: int) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return
        if str(chat.get("type") or "") != "private":
            return
        chat_id = str(chat.get("id") or "").strip()
        user_id = str(sender.get("id") or "").strip()
        message_id = str(message.get("message_id") or "").strip()
        if not chat_id or not user_id or not message_id or not self.bridge_config.is_allowed_user(user_id):
            return

        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            self._state.enqueue_notice(
                chat_id=chat_id,
                dedup_key=f"unsupported-input:{update_id}",
                content="目前僅支援 Telegram 純文字訊息。",
            )
            return
        stripped = text.strip()
        command_token, _separator, command_args = stripped.partition(" ")
        command = command_token.split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            self._state.enqueue_notice(
                chat_id=chat_id,
                dedup_key=f"help:{update_id}",
                content=(
                    "此 Bot 會將你的文字送到 Hermes Controller 的 canonical Bot Chat。\n"
                    "可用指令：/status、/tasks、/task <ID>、/tell <ID> <留言>、/help\n"
                    "也可直接回覆任務卡來留言。一般文字會保留 Bot Mode，"
                    "因此 Controller 可原生使用 message_agent。"
                ),
            )
            return
        if command == "/status":
            status = self._service.status()
            backend = "正常" if not status["last_backend_error"] else "暫時無法連線"
            self._state.enqueue_notice(
                chat_id=chat_id,
                dedup_key=f"status:{update_id}",
                content=(
                    f"Controller：{status['controller_profile']}\n"
                    f"Backend：{backend}\n"
                    f"待送輸入：{status['pending_inbound']}（未確認送達：{status['uncertain_inbound']}）；"
                    f"待送回覆：{status['pending_outbox']}"
                ),
            )
            return
        if command == "/tasks":
            self._state.enqueue_notice(
                chat_id=chat_id,
                dedup_key=f"tasks:{update_id}",
                content=render_task_list(self._state.list_tasks(chat_id=chat_id, limit=10)),
            )
            return
        if command == "/task":
            task_id = normalize_task_id(command_args)
            task = self._state.task(task_id) if task_id else None
            content = (
                render_task_card(task)
                if task is not None and task.chat_id == chat_id
                else "找不到任務。用法：/task TCB-YYYYMMDD-XXXXXX"
            )
            self._state.enqueue_notice(
                chat_id=chat_id,
                dedup_key=f"task:{update_id}",
                content=content,
            )
            return
        if command == "/tell":
            task_token, separator, note = command_args.strip().partition(" ")
            await self._record_task_note(
                update_id=update_id,
                chat_id=chat_id,
                user_id=user_id,
                message_id=message_id,
                task_id=task_token if separator else "",
                text=note if separator else "",
            )
            return

        replied = message.get("reply_to_message")
        if isinstance(replied, dict) and replied.get("message_id") is not None:
            task = self._state.task_by_telegram_message(
                chat_id=chat_id,
                telegram_message_id=str(replied["message_id"]),
            )
            if task is not None:
                await self._record_task_note(
                    update_id=update_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    message_id=message_id,
                    task_id=task.id,
                    text=text,
                )
                return

        await self.send_typing(chat_id)
        await self._service.receive_text(
            update_id=str(update_id),
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            text=text,
        )

    async def _record_task_note(
        self,
        *,
        update_id: int,
        chat_id: str,
        user_id: str,
        message_id: str,
        task_id: str,
        text: str,
    ) -> None:
        _task, accepted, detail = self._state.add_task_note(
            task_id=task_id,
            chat_id=chat_id,
            user_id=user_id,
            telegram_message_id=message_id,
            text=text,
        )
        self._state.enqueue_notice(
            chat_id=chat_id,
            dedup_key=f"task-note-result:{update_id}",
            content=("✅ " if accepted else "⚠️ ") + detail,
        )

    async def _bridge_tick_loop(self) -> None:
        while self._running:
            try:
                await self._service.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Service 已對每筆輸入做 durable retry；此處只保護背景 loop 不因一次失敗停止。
                logger.warning("[%s] Hermes bridge tick failed: %s", PLATFORM_NAME, exc)
            await asyncio.sleep(self.bridge_config.history_poll_interval_seconds)

    async def _outbox_loop(self) -> None:
        while self._running:
            records = self._state.claim_due_outbox()
            if not records:
                await asyncio.sleep(0.5)
                continue
            for record in records:
                try:
                    task_message_id = (
                        self._state.task_telegram_message_id(record.task_id)
                        if record.kind == "task_status" and record.task_id
                        else None
                    )
                    if task_message_id:
                        try:
                            message_id = await self._telegram.edit_message_text(
                                chat_id=record.chat_id,
                                message_id=task_message_id,
                                text=record.content,
                            )
                        except TelegramApiError as exc:
                            detail = exc.message.lower()
                            recoverable_card = exc.error_code == 400 and any(
                                phrase in detail for phrase in (
                                    "message to edit not found",
                                    "message can't be edited",
                                    "message can not be edited",
                                )
                            )
                            if not recoverable_card or not record.task_id:
                                raise
                            self._state.clear_task_telegram_message(
                                record.task_id, expected_message_id=task_message_id
                            )
                            message_id = await self._telegram.send_message(
                                chat_id=record.chat_id, text=record.content
                            )
                    else:
                        message_id = await self._telegram.send_message(
                            chat_id=record.chat_id, text=record.content
                        )
                except asyncio.CancelledError:
                    raise
                except TelegramApiError as exc:
                    if exc.error_code in {401, 404}:
                        await self._fatal("telegram_auth", f"Telegram Bot 已失效：{exc}", retryable=False)
                        return
                    attempts = self._state.defer_outbox(
                        record.id,
                        error=str(exc),
                        delay_seconds=self._retry_delay(record.attempts + 1),
                    )
                    logger.warning("[%s] Telegram outbox retry id=%s attempts=%s: %s", PLATFORM_NAME, record.id, attempts, exc)
                except Exception as exc:
                    attempts = self._state.defer_outbox(
                        record.id,
                        error=str(exc),
                        delay_seconds=self._retry_delay(record.attempts + 1),
                    )
                    logger.warning("[%s] Telegram outbox retry id=%s attempts=%s: %s", PLATFORM_NAME, record.id, attempts, exc)
                else:
                    self._state.mark_outbox_sent(record.id, message_id)

    def _retry_delay(self, attempts: int) -> float:
        return min(
            self.bridge_config.retry_max_seconds,
            self.bridge_config.retry_base_seconds * (2 ** max(0, attempts - 1)),
        )

    async def _fatal(self, code: str, message: str, *, retryable: bool) -> None:
        self._set_fatal_error(code, message, retryable=retryable)
        await self._notify_fatal_error()


def check_requirements() -> bool:
    """僅做無副作用檢查；狀態頁會反覆呼叫它。"""

    if not check_secrets_present():
        return False
    try:
        import websockets  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config: PlatformConfig) -> bool:
    try:
        BridgeConfig.from_platform_config(config)
    except (BridgeConfigurationError, TypeError, ValueError):
        return False
    return True


def register(ctx: Any) -> None:
    """Hermes plugin loader 的入口。"""

    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Telegram Canonical Bridge",
        adapter_factory=TelegramCanonicalBridgeAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=[BOT_TOKEN_ENV, BACKEND_TOKEN_ENV],
        install_hint="使用 Python 標準函式庫；請設定 Telegram token、Hermes backend token 與 extra 設定。",
        max_message_length=4000,
        pii_safe=False,
        emoji="✈️",
        allow_update_command=False,
        platform_hint="Telegram 訊息會直接轉送到既有 canonical Bot Chat，不建立一般 Telegram session。",
    )
