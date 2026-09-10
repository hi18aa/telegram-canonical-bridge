"""原生 sidecar outbox 的短生命週期背景傳送器。

它只呼叫公開的 ``hermes send``，不讀 Telegram token，也不輪詢 Telegram。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import time
from pathlib import Path

from .native_delivery import flush_native_outbox
from .state import BridgeState


class _DeliveryLock:
    def __init__(self, path: Path, timeout: float = 3.0) -> None:
        self.path = path
        self.timeout = max(0.0, float(timeout))
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.stream.write(b"\0")
            self.stream.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    self.stream.close()
                    self.stream = None
                    raise TimeoutError("已有 native delivery runner 正在處理 outbox")
                time.sleep(0.1)

    def __exit__(self, _exc_type, _exc, _traceback):
        if self.stream is None:
            return False
        with contextlib.suppress(OSError):
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()
        return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state", required=True)
    parser.add_argument("--max-runtime", type=float, default=600.0)
    parser.add_argument("--idle-grace", type=float, default=1.5)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    state_path = Path(args.state).resolve()
    state = BridgeState(state_path)
    deadline = time.monotonic() + max(5.0, float(args.max_runtime))
    idle_since: float | None = None
    lock_path = state_path.parent / "native-delivery.lock"

    try:
        with _DeliveryLock(lock_path):
            while time.monotonic() < deadline:
                flush_native_outbox(state, limit=32)
                wait = state.next_outbox_wait()
                if wait is None:
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since >= max(0.0, float(args.idle_grace)):
                        return 0
                    time.sleep(0.1)
                    continue
                idle_since = None
                time.sleep(min(2.0, max(0.1, wait)))
    except TimeoutError:
        # 另一個 runner 已持有鎖並會觀察 durable outbox；這不是傳送失敗。
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
