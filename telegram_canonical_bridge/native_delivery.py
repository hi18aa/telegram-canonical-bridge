"""透過 Hermes 原生 outbound CLI 傳送 sidecar 任務事件。

這裡不持有 Telegram token、不輪詢 Telegram，也不實作任何平台協定。
所有外送都交給公開的 ``hermes send``，所以檔案、權限與平台設定仍由
Hermes 原生 adapter 負責。
"""

from __future__ import annotations

import os
import shutil
import subprocess
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
    for record in state.claim_due_native_outbox(limit=limit):
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
