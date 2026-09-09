"""Telegram 訊息到 canonical Bot Chat 的橋接協調器。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

from .config import BridgeConfig
from .hermes_rpc import HermesRpcClient, RpcError
from .protocol import extract_assistant_history
from .state import BridgeState, InboundRecord


logger = logging.getLogger(__name__)


class CanonicalUnavailable(RuntimeError):
    """指定 profile 尚無可用 canonical Bot Chat。"""


@dataclass(frozen=True)
class CanonicalHandle:
    root_id: str
    runtime_id: str


RpcFactory = Callable[[str, str, float], HermesRpcClient]


def _default_rpc_factory(url: str, token: str, timeout: float) -> HermesRpcClient:
    return HermesRpcClient(url, token, timeout_seconds=timeout)


class CanonicalBridgeService:
    """只透過 Hermes 對外 JSON-RPC 操作既有 canonical Bot Chat。

    此類別刻意不建立 ``agent:main:telegram:*`` session，也不重新實作
    ``message_agent``；它只將輸入送往 Bot Mode 已管理的 Bot Chat。
    """

    def __init__(
        self,
        config: BridgeConfig,
        state: BridgeState,
        *,
        rpc_factory: RpcFactory = _default_rpc_factory,
    ) -> None:
        self.config = config
        self.state = state
        self._rpc_factory = rpc_factory
        self._operation_lock = asyncio.Lock()
        self._last_backend_error = ""

    async def startup_probe(self) -> bool:
        """建立舊歷史基準；失敗時 adapter 仍可收 Telegram 並稍後重試。"""

        try:
            await self.sync_history()
            return True
        except Exception as exc:
            self._last_backend_error = str(exc)
            logger.warning("Telegram canonical bridge startup probe failed: %s", exc)
            return False

    async def receive_text(
        self,
        *,
        update_id: str,
        chat_id: str,
        user_id: str,
        message_id: str,
        text: str,
    ) -> bool:
        """持久化一筆 Telegram 輸入後，盡快送往 Controller。"""

        if not self.state.record_inbound(
            update_id=update_id, chat_id=chat_id, user_id=user_id, message_id=message_id, text=text
        ):
            return False
        if self.config.home_chat_id and chat_id != self.config.home_chat_id:
            self.state.mark_inbound_ignored(update_id, "chat_id 不符合 home_chat_id")
            return False
        if not self.state.bind_active_route(
            controller_profile=self.config.controller_profile, chat_id=chat_id, user_id=user_id
        ):
            self.state.mark_inbound_ignored(update_id, "Controller 已綁定另一個 Telegram 私訊")
            self.state.enqueue_notice(
                chat_id=chat_id,
                dedup_key=f"route-conflict:{update_id}",
                content="此 Controller 目前已綁定另一個 Telegram 私訊；V1 不支援多使用者共用同一個 Bot Chat。",
            )
            return False
        await self.retry_due_inbound(limit=1)
        return True

    async def tick(self) -> None:
        """由 adapter 的背景工作定期呼叫：補送輸入與補讀完成結果。"""

        await self.retry_due_inbound()
        await self.sync_history()

    async def retry_due_inbound(self, *, limit: int = 8) -> int:
        submitted = 0
        async with self._operation_lock:
            for record in self.state.claim_due_inbound(limit=limit):
                try:
                    await self._submit_record(record)
                except Exception as exc:
                    if isinstance(exc, RpcError) and exc.uncertain:
                        # prompt.submit 不具 idempotency key。若 bytes 已離開 bridge
                        # 卻沒收到結果，重送可能讓主 agent 執行兩次；保留供 operator
                        # 檢查／使用者重新發送，而非偷偷複製工作。
                        self.state.mark_inbound_uncertain(record.id, error=str(exc))
                        self._last_backend_error = str(exc)
                        logger.warning(
                            "Telegram canonical bridge kept update=%s uncertain after non-idempotent submit: %s",
                            record.update_id, exc,
                        )
                        self.state.enqueue_notice(
                            chat_id=record.chat_id,
                            dedup_key=f"backend-uncertain:{record.id}",
                            content=(
                                "Controller 收到訊息的狀態無法確認；為避免重複執行，bridge 不會自動重送。"
                                "請稍候查看回覆，或以新訊息重新提出需求。"
                            ),
                        )
                        continue
                    delay = self._retry_delay(record.attempts + 1)
                    attempts, notify = self.state.defer_inbound(record.id, error=str(exc), delay_seconds=delay)
                    self._last_backend_error = str(exc)
                    logger.warning(
                        "Telegram canonical bridge deferred update=%s attempts=%s error=%s",
                        record.update_id, attempts, exc,
                    )
                    if notify:
                        self.state.enqueue_notice(
                            chat_id=record.chat_id,
                            dedup_key=f"backend-unavailable:{record.id}",
                            content="Controller 暫時無法連線；訊息已安全保留並會自動重試。",
                        )
                else:
                    self.state.mark_inbound_submitted(record.id)
                    self._last_backend_error = ""
                    submitted += 1
        return submitted

    async def sync_history(self) -> int:
        """讀取 canonical 歷史，將新 assistant 結果加入 Telegram outbox。"""

        async with self._operation_lock:
            async with self._new_rpc() as rpc:
                handle = await self._open_canonical(rpc)
                messages = await self._history(rpc, handle.runtime_id)
                assistant_messages = extract_assistant_history(messages)
                route = self.state.active_route(self.config.controller_profile)
                bootstrap_key = self._bootstrap_meta_key(handle.root_id)
                bootstrap = route is None or self.state.get_meta(bootstrap_key) != "ready"
                queued = self.state.record_history(
                    root_id=handle.root_id,
                    messages=assistant_messages,
                    chat_id=route[0] if route else None,
                    bootstrap=bootstrap,
                )
                if bootstrap:
                    self.state.set_meta(bootstrap_key, "ready")
                self._last_backend_error = ""
                return queued

    async def _submit_record(self, record: InboundRecord) -> None:
        async with self._new_rpc() as rpc:
            handle = await self._open_canonical(rpc)
            await self._bootstrap_with_rpc(rpc, handle)
            await rpc.call(
                "prompt.submit",
                {
                    "session_id": handle.runtime_id,
                    "text": record.text,
                    # Hermes 會在閒置時直接開始；忙碌時保留輸入順序，不打斷原任務。
                    "queued": True,
                },
            )

    def _new_rpc(self) -> HermesRpcClient:
        return self._rpc_factory(
            self.config.backend_url,
            self.config.backend_token,
            self.config.rpc_timeout_seconds,
        )

    async def _open_canonical(self, rpc: HermesRpcClient) -> CanonicalHandle:
        response = await rpc.call("profiles.list", {"include_sessions": True})
        profiles = response.get("profiles") if isinstance(response, dict) else None
        profile = next(
            (item for item in profiles or [] if isinstance(item, dict) and item.get("name") == self.config.controller_profile),
            None,
        )
        if profile is None:
            raise CanonicalUnavailable(f"找不到 Controller profile：{self.config.controller_profile}。")
        canonical = profile.get("canonical_session")
        if not isinstance(canonical, dict) or not str(canonical.get("id") or "").strip():
            raise CanonicalUnavailable(
                f"Controller {self.config.controller_profile} 沒有可用的 canonical Bot Chat；請先在 Hermes Desktop 開啟它。"
            )
        root_id = str(canonical["id"]).strip()
        target_id = str(canonical.get("resolved_id") or root_id).strip()
        resumed = await rpc.call(
            "session.resume",
            {
                "session_id": target_id,
                "profile": self.config.controller_profile,
                # lazy resume 是唯讀／子工作 watcher，並不會啟用正常 prompt
                # 生命週期；canonical Bot Chat 必須使用預設 cold resume 才能安全提交 turn。
                "omit_messages": True,
            },
        )
        runtime_id = str((resumed or {}).get("session_id") or target_id).strip()
        if not runtime_id:
            raise RpcError("session.resume 未回傳 runtime session_id。")
        self.state.set_canonical_binding(
            controller_profile=self.config.controller_profile,
            root_id=root_id,
            runtime_id=runtime_id,
        )
        return CanonicalHandle(root_id=root_id, runtime_id=runtime_id)

    async def _bootstrap_with_rpc(self, rpc: HermesRpcClient, handle: CanonicalHandle) -> None:
        bootstrap_key = self._bootstrap_meta_key(handle.root_id)
        if self.state.get_meta(bootstrap_key) == "ready":
            return
        messages = await self._history(rpc, handle.runtime_id)
        self.state.record_history(
            root_id=handle.root_id,
            messages=extract_assistant_history(messages),
            chat_id=None,
            bootstrap=True,
        )
        self.state.set_meta(bootstrap_key, "ready")

    async def _history(self, rpc: HermesRpcClient, runtime_id: str) -> list[dict[str, Any]]:
        response = await rpc.call("session.history", {"session_id": runtime_id})
        messages = response.get("messages") if isinstance(response, dict) else None
        return [message for message in messages if isinstance(message, dict)] if isinstance(messages, list) else []

    def _retry_delay(self, attempts: int) -> float:
        return min(self.config.retry_max_seconds, self.config.retry_base_seconds * (2 ** max(0, attempts - 1)))

    def _bootstrap_meta_key(self, root_id: str) -> str:
        return f"canonical-bootstrap:{self.config.controller_profile}:{root_id}"

    def status(self) -> dict[str, Any]:
        route = self.state.active_route(self.config.controller_profile)
        binding = self.state.canonical_binding(self.config.controller_profile)
        return {
            "controller_profile": self.config.controller_profile,
            "active_chat_id": route[0] if route else "",
            "canonical_root_id": binding[0] if binding else "",
            "canonical_runtime_id": binding[1] if binding else "",
            "last_backend_error": self._last_backend_error,
            **self.state.counts(),
        }
