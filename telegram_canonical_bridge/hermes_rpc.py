"""Hermes TUI Gateway JSON-RPC 的小型、版本隔離客戶端。"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit


@dataclass(frozen=True)
class RpcError(RuntimeError):
    """JSON-RPC 或 WebSocket 層的可呈現錯誤。"""

    message: str
    code: int | None = None
    data: Any = None
    # True = request bytes may already be accepted by Hermes, so callers must
    # not retry non-idempotent operations such as prompt.submit blindly.
    uncertain: bool = False

    def __str__(self) -> str:
        return self.message


EventCallback = Callable[[str, str, dict[str, Any]], Awaitable[None] | None]


class HermesRpcClient:
    """每個使用週期連一個 Hermes 後端，完成工作後即釋放連線。

    預設的 bridge 不常駐佔有 canonical session 的事件 transport。這在舊版 Hermes
    尚未完整 fan-out 時，能讓 Desktop 繼續成為主要的即時觀看端；bridge 透過持久化
    history 補讀完成結果。
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_seconds: float = 25,
        on_event: EventCallback | None = None,
    ) -> None:
        self.url = url
        self._token = token
        self.timeout_seconds = timeout_seconds
        self._on_event = on_event
        self._socket: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._request_ids = itertools.count(1)
        self._send_lock = asyncio.Lock()
        self._closed = False

    async def __aenter__(self) -> "HermesRpcClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._socket is not None:
            return
        try:
            import websockets
        except ImportError as exc:
            raise RpcError("Hermes 執行環境缺少 websockets，無法連接 Desktop 後端。") from exc

        # Hermes 0.21.x 的 /api/ws loopback contract 驗證 ``?token=``，而非
        # HTTP header。設定檔仍禁止 token query，避免把秘密長期寫進 config；只在
        # 實際 handshake 前於記憶體組出 URL。保留 header 是對較新後端的無害相容層。
        connection_url = self._connection_url()
        headers = {"X-Hermes-Session-Token": self._token}
        kwargs: dict[str, Any] = {
            "open_timeout": self.timeout_seconds,
            "close_timeout": min(10, self.timeout_seconds),
            "ping_interval": 20,
            "ping_timeout": 20,
            "max_size": 8 * 1024 * 1024,
        }
        parameters = inspect.signature(websockets.connect).parameters
        if "additional_headers" in parameters:
            kwargs["additional_headers"] = headers
        else:  # websockets < 14
            kwargs["extra_headers"] = headers
        hostname = (urlparse(self.url).hostname or "").lower()
        if hostname in {"localhost", "127.0.0.1", "::1"} and "proxy" in parameters:
            kwargs["proxy"] = None
        try:
            self._socket = await websockets.connect(connection_url, **kwargs)
        except Exception as exc:
            raise RpcError(f"無法連接 Hermes backend：{type(exc).__name__}: {exc}") from exc

        self._reader_task = asyncio.create_task(self._reader(), name="telegram-canonical-bridge-rpc-reader")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=min(10, self.timeout_seconds))
        except TimeoutError as exc:
            await self.close()
            raise RpcError("Hermes backend 未在期限內送出 gateway.ready。") from exc

    async def call(self, method: str, params: dict[str, Any] | None = None, *, timeout_seconds: float | None = None) -> Any:
        if self._socket is None:
            raise RpcError("Hermes backend 尚未連線。")
        request_id = f"telegram-canonical-bridge-{next(self._request_ids)}"
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        sent = False
        try:
            async with self._send_lock:
                await self._socket.send(json.dumps(request, ensure_ascii=False, separators=(",", ":")))
                sent = True
            return await asyncio.wait_for(future, timeout=timeout_seconds or self.timeout_seconds)
        except TimeoutError as exc:
            raise RpcError(f"Hermes RPC {method} 逾時。", uncertain=sent) from exc
        except RpcError:
            raise
        except Exception as exc:
            raise RpcError(
                f"Hermes RPC {method} 失敗：{type(exc).__name__}: {exc}", uncertain=sent
            ) from exc
        finally:
            self._pending.pop(request_id, None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        reader = self._reader_task
        self._reader_task = None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        socket, self._socket = self._socket, None
        if socket is not None:
            with contextlib.suppress(Exception):
                await socket.close()
        self._fail_pending(RpcError("Hermes RPC 連線已關閉。", uncertain=True))

    async def _reader(self) -> None:
        try:
            async for raw in self._socket:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    frame = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(frame, dict):
                    continue
                if "id" in frame:
                    self._resolve_response(frame)
                    continue
                if frame.get("method") == "event":
                    await self._handle_event(frame.get("params"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_pending(RpcError(
                f"Hermes RPC 讀取中斷：{type(exc).__name__}: {exc}", uncertain=True
            ))

    def _resolve_response(self, frame: dict[str, Any]) -> None:
        request_id = str(frame.get("id"))
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        error = frame.get("error")
        if isinstance(error, dict):
            future.set_exception(RpcError(
                str(error.get("message") or "Hermes RPC 回傳錯誤。"),
                code=error.get("code") if isinstance(error.get("code"), int) else None,
                data=error.get("data"),
            ))
            return
        future.set_result(frame.get("result"))

    async def _handle_event(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        event_type = str(params.get("type") or "")
        session_id = str(params.get("session_id") or "")
        payload = params.get("payload")
        if event_type == "gateway.ready":
            self._ready.set()
        if self._on_event is not None:
            result = self._on_event(event_type, session_id, payload if isinstance(payload, dict) else {})
            if inspect.isawaitable(result):
                await result

    def _fail_pending(self, error: RpcError) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    def _connection_url(self) -> str:
        """在送出 WebSocket upgrade 前附加短暫 credential，不污染 operator config。"""

        parsed = urlsplit(self.url)
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "token"]
        query.append(("token", self._token))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
