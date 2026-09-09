"""透過 Hermes 原生 outbound CLI 傳送 sidecar 任務事件。

這裡不持有 Telegram token、不輪詢 Telegram，也不實作任何平台協定。
所有外送都交給公開的 ``hermes send``，所以檔案、權限與平台設定仍由
Hermes 原生 adapter 負責。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from .state import BridgeState


NATIVE_ROUTE_PREFIX = "native:"
LOCAL_DELIVERY_TARGET = "local"


def native_route(delivery_target: str) -> str:
    return f"{NATIVE_ROUTE_PREFIX}{str(delivery_target or LOCAL_DELIVERY_TARGET).strip()}"


def delivery_target_from_route(route: str) -> str:
    value = str(route or "")
    return value[len(NATIVE_ROUTE_PREFIX):] if value.startswith(NATIVE_ROUTE_PREFIX) else ""


def _hermes_executable() -> str:
    return shutil.which("hermes") or "hermes"


def _windows_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))


def _send_text(*, profile: str, target: str, content: str, spool_dir: Path) -> tuple[bool, str]:
    spool_dir.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix="event-", suffix=".txt", dir=spool_dir)
    path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        with suppress(OSError):
            path.chmod(0o600)
        argv = [_hermes_executable()]
        if profile and profile != "default":
            argv.extend(["-p", profile])
        argv.extend(["send", "--to", target, "--file", str(path), "--quiet"])
        completed = subprocess.run(
            argv,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=_windows_creation_flags(),
        )
        if completed.returncode == 0:
            return True, ""
        detail = (completed.stderr or completed.stdout or "hermes send failed").strip()
        return False, detail[-1000:]
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        with suppress(OSError):
            path.unlink()


def flush_native_outbox(state: BridgeState, *, limit: int = 8) -> dict[str, int]:
    """嘗試送出 sidecar 任務事件；失敗保留在 durable outbox 待下次重試。"""

    sent = failed = 0
    remaining = max(0, int(limit))
    while remaining:
        # ``claim_due_native_outbox`` 會刻意只領取每個 task 最早的一筆，
        # 以保證時間線順序。每成功送出一批後必須重新 claim，否則同一個
        # 快速任務的 result／completed 會留到下一個 hook 才有機會送出。
        records = state.claim_due_native_outbox(limit=remaining)
        if not records:
            break
        remaining -= len(records)
        for record in records:
            target = delivery_target_from_route(record.chat_id)
            task = state.task(record.task_id or "") if record.task_id else None
            profile = task.origin_profile if task is not None else "default"
            if not target or target == LOCAL_DELIVERY_TARGET:
                state.mark_outbox_sent(record.id, f"local-{record.id}")
                sent += 1
                continue
            ok, detail = _send_text(
                profile=profile,
                target=target,
                content=record.content,
                spool_dir=state.path.parent / "delivery-spool",
            )
            if ok:
                state.mark_outbox_sent(record.id, f"native-{record.id}-{int(time.time())}")
                sent += 1
                continue
            delay = min(300.0, 5.0 * (2 ** min(record.attempts, 6)))
            state.defer_outbox(record.id, error=detail, delay_seconds=delay)
            failed += 1
    return {"sent": sent, "failed": failed}


def kick_native_outbox(state: BridgeState) -> bool:
    """喚醒短生命週期 delivery runner；不讓 Agent hook 等待 Telegram CLI。

    本機測試路由沒有網路 I/O，直接同步確認即可。其他路由交給獨立程序；
    runner 會持續處理 retry 時間，避免最後一筆 completion 因暫時失敗永遠卡住。
    """

    routes = state.pending_native_routes()
    if not routes:
        return False
    if all(delivery_target_from_route(route) == LOCAL_DELIVERY_TARGET for route in routes):
        flush_native_outbox(state, limit=32)
        return True

    package_root = Path(__file__).resolve().parent.parent
    argv = [
        sys.executable,
        "-m",
        "telegram_canonical_bridge.native_delivery_runner",
        "--state",
        str(state.path.resolve()),
    ]
    kwargs: dict[str, object] = {
        "cwd": str(package_root),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "creationflags": _windows_creation_flags(),
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(argv, **kwargs)
        return True
    except OSError:
        # 啟動失敗時仍嘗試一次同步傳送；outbox 會保留失敗紀錄供下次 hook 重試。
        flush_native_outbox(state, limit=8)
        return False
