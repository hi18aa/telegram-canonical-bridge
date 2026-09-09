from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from telegram_canonical_bridge.config import BridgeConfig
from telegram_canonical_bridge.hermes_rpc import RpcError
from telegram_canonical_bridge.service import CanonicalBridgeService
from telegram_canonical_bridge.state import BridgeState


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.history: list[dict[str, Any]] = [
            {"_row_id": "old", "role": "assistant", "content": "啟動前既有回覆"},
        ]
        self.prompts: list[str] = []


class FakeRpc:
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend

    async def __aenter__(self) -> "FakeRpc":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        data = params or {}
        self.backend.calls.append((method, data))
        if method == "profiles.list":
            return {
                "profiles": [{
                    "name": "default",
                    "canonical_session": {"id": "root-bot-chat", "resolved_id": "resolved-bot-chat"},
                }],
            }
        if method == "session.resume":
            return {"session_id": "runtime-bot-chat"}
        if method == "session.history":
            return {"messages": self.backend.history}
        if method == "prompt.submit":
            self.backend.prompts.append(str(data["text"]))
            return {"accepted": True}
        raise AssertionError(f"unexpected RPC method: {method}")


class UncertainPromptRpc(FakeRpc):
    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method == "prompt.submit":
            raise RpcError("connection lost after send", uncertain=True)
        return await super().call(method, params)


class CanonicalBridgeServiceTests(unittest.TestCase):
    def test_uses_existing_canonical_session_and_only_returns_new_history(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                config = BridgeConfig(
                    bot_token="telegram-token",
                    backend_token="backend-token",
                    backend_url="ws://127.0.0.1:9119/api/ws",
                    controller_profile="default",
                    allowed_user_ids=("20",),
                    state_path=Path(directory) / "bridge.sqlite3",
                    home_chat_id=None,
                    telegram_poll_timeout_seconds=40,
                    history_poll_interval_seconds=3,
                    rpc_timeout_seconds=25,
                    retry_base_seconds=1,
                    retry_max_seconds=10,
                )
                backend = FakeBackend()
                service = CanonicalBridgeService(
                    config,
                    BridgeState(config.state_path),
                    rpc_factory=lambda _url, _token, _timeout: FakeRpc(backend),  # type: ignore[arg-type]
                )

                self.assertTrue(await service.startup_probe())
                self.assertTrue(await service.receive_text(
                    update_id="1", chat_id="10", user_id="20", message_id="30", text="請派子代理"
                ))
                backend.history.append({
                    "_row_id": "new", "role": "assistant", "content": "已完成並回報。"
                })
                self.assertEqual(await service.sync_history(), 1)

                outbox = service.state.claim_due_outbox()
                self.assertEqual([record.content for record in outbox], ["已完成並回報。"])
                self.assertEqual(backend.prompts, ["請派子代理"])
                methods = [method for method, _params in backend.calls]
                self.assertIn("session.resume", methods)
                self.assertIn("prompt.submit", methods)
                self.assertNotIn("session.create", methods)

        asyncio.run(scenario())

    def test_ambiguous_prompt_submit_is_not_blindly_retried(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                config = BridgeConfig(
                    bot_token="telegram-token", backend_token="backend-token",
                    backend_url="ws://127.0.0.1:9119/api/ws", controller_profile="default",
                    allowed_user_ids=("20",), state_path=Path(directory) / "bridge.sqlite3",
                    home_chat_id=None, telegram_poll_timeout_seconds=40,
                    history_poll_interval_seconds=3, rpc_timeout_seconds=25,
                    retry_base_seconds=1, retry_max_seconds=10,
                )
                backend = FakeBackend()
                service = CanonicalBridgeService(
                    config,
                    BridgeState(config.state_path),
                    rpc_factory=lambda _url, _token, _timeout: UncertainPromptRpc(backend),  # type: ignore[arg-type]
                )

                self.assertTrue(await service.receive_text(
                    update_id="uncertain", chat_id="10", user_id="20", message_id="30", text="不可重送"
                ))
                self.assertEqual(service.status()["uncertain_inbound"], 1)
                self.assertEqual(await service.retry_due_inbound(), 0)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
