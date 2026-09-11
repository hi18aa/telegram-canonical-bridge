"""Sidecar 的 stdlib-only 背景 runner。

runner 只負責跨程序序列化與呼叫 Hermes 公開 CLI；任務內容放在權限受限的
暫存檔，不會插入 shell command line。取消意圖由 task ledger 的 stopping
傳給原 runner，由它停止自己啟動的 child，避免依賴別的 Controller 程序中的 handle。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
TASK_ID_RE = re.compile(r"^TCB-[0-9]{8}-[A-F0-9]{6}$", re.IGNORECASE)
_PLUGIN_TOOLSETS = {"telegram_canonical_bridge"}


class TargetBusyError(TimeoutError):
    pass


class TaskCancelledError(RuntimeError):
    pass


def _filter_plugin_toolset_startup_warning(text: str) -> str:
    """移除 Hermes 啟動競態造成、且僅指向本 plugin 的已知假警告。"""

    kept: list[str] = []
    prefix = "Warning: Unknown toolsets:"
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            names = {
                item.strip()
                for item in stripped[len(prefix):].split(",")
                if item.strip()
            }
            if names and names <= _PLUGIN_TOOLSETS:
                continue
        kept.append(line)
    return "\n".join(kept).strip()


class _ProfileLock:
    def __init__(self, root: Path, profile: str, timeout_seconds: float, should_cancel=None) -> None:
        digest = hashlib.sha256(profile.lower().encode("utf-8")).hexdigest()[:20]
        self.path = root / f"{digest}.lock"
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.stream = None
        self.should_cancel = should_cancel

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.stream.write(b"\0")
            self.stream.flush()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            if self.should_cancel and self.should_cancel():
                self.stream.close()
                self.stream = None
                raise TaskCancelledError("等待 profile lock 期間收到取消要求")
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
                    raise TargetBusyError("等待同一 Bot 的前一個任務逾時")
                time.sleep(0.25)

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
    parser.add_argument("--target", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--message-file", required=True)
    parser.add_argument("--lock-root", required=True)
    parser.add_argument("--lock-timeout", type=float, default=3600.0)
    parser.add_argument("--hermes", default="")
    return parser


def _load_state(state_path: Path):
    package_root = Path(__file__).resolve().parent.parent
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    from telegram_canonical_bridge.state import BridgeState

    return BridgeState(state_path)


def _cancellation_requested(state, task_id: str) -> bool:
    task = state.task(task_id)
    if task is None:
        raise RuntimeError("找不到原 runner 的 task，拒絕執行")
    return task.status in {"stopping", "cancelled"}


def _stop_owned_process_tree(process) -> None:
    """只處理本 runner 持有的 Popen，不查全機 PID 或任意 profile。"""
    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        stopped = subprocess.run(
            [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, check=False,
        )
        if stopped.returncode != 0 and process.poll() is None:
            raise RuntimeError("無法確認原 worker 程序樹已停止")
    else:
        # child 由 start_new_session 建立自己的 process group。
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)


def _run_worker_command(command, *, state, task_id: str):
    if _cancellation_requested(state, task_id):
        raise TaskCancelledError("啟動 worker 前收到取消要求")
    # Windows 的 ``communicate(timeout=...)`` 會建立 pipe reader threads；反覆
    # timeout 後再 tree-kill，關閉 pipe 可能卡在 reader thread。用本機暫存檔
    # 捕捉輸出即可維持不限量輸出與取消語意，且 runner 不會留下背景 reader。
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        cancelled = False
        try:
            while process.poll() is None:
                if _cancellation_requested(state, task_id):
                    _stop_owned_process_tree(process)
                    cancelled = True
                    break
                time.sleep(0.25)
            return_code = process.wait(timeout=5)
        except BaseException:
            _stop_owned_process_tree(process)
            raise
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read().decode("utf-8", errors="replace")
        stderr = stderr_file.read().decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(
            command,
            130 if cancelled else int(return_code),
            stdout,
            stderr,
        ), cancelled


def _record_completion(
    state_path: Path,
    task_id: str,
    *,
    exit_code: int,
    output: str = "",
    diagnostic: str = "",
    reason: str = "",
) -> bool:
    """直接收斂 durable ledger；來源 session 的完成通知只是第二條保險。"""

    try:
        state = _load_state(state_path)
        from telegram_canonical_bridge.native_delivery import kick_native_outbox
        from telegram_canonical_bridge.task_completion import (
            completion_reason,
            settle_task_completion,
        )

        updated = settle_task_completion(
            state,
            task_id,
            exit_code=exit_code,
            output=output,
            reason=reason or completion_reason(f"{output}\n{diagnostic}"),
            origin="task runner",
        )
        if updated is not None:
            kick_native_outbox(state)
        return updated is not None
    except Exception:
        # Hermes 的來源 session completion hook 仍可補做收斂；runner 不因 ledger
        # 暫時失敗而抹掉已取得的 Bot final output。
        return False


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    target = str(args.target).strip()
    task_id = str(args.task_id).strip().upper()
    state_path = Path(args.state).resolve()
    # ``hermes chat --in ~`` 會先切換工作目錄；query-file 必須固定為絕對路徑。
    message_file = Path(args.message_file).resolve()
    if not PROFILE_RE.fullmatch(target):
        if TASK_ID_RE.fullmatch(task_id):
            _record_completion(
                state_path, task_id, exit_code=2, reason="invalid_target"
            )
        print(json.dumps({"error": "invalid target profile", "reason": "invalid_target"}))
        return 2
    if not TASK_ID_RE.fullmatch(task_id):
        print(json.dumps({"error": "invalid task id", "reason": "invalid_task"}))
        return 2
    hermes = str(args.hermes or shutil.which("hermes") or "hermes")
    # 每個任務使用自己的 Hermes conversation。這可避免 Desktop 正開著 canonical
    # ``Bot Chat`` 時，CLI 只能把訊息排入佇列卻無法真正啟動 Bot turn；也避免不同
    # Controller 或不同任務共用上下文。
    conversation = f"TCB Task {task_id}"
    command = [
        hermes,
        "-p",
        target,
        "chat",
        "--in",
        "~",
        "-c",
        conversation,
        "--create-if-missing",
        "--source",
        "tool",
        "-Q",
        "--query-file",
        str(message_file),
    ]
    try:
        state = _load_state(state_path)
        with _ProfileLock(Path(args.lock_root), target, args.lock_timeout,
                          should_cancel=lambda: _cancellation_requested(state, task_id)):
            completed, cancelled = _run_worker_command(command, state=state, task_id=task_id)
        stdout = _filter_plugin_toolset_startup_warning(completed.stdout)
        stderr = _filter_plugin_toolset_startup_warning(completed.stderr)
        busy = (
            completed.returncode != 0
            and "already has a live owner" in stderr.lower()
        )
        _record_completion(
            state_path,
            task_id,
            exit_code=int(completed.returncode),
            output=stdout,
            diagnostic=stderr,
            reason="cancelled" if cancelled else "target_busy" if busy else "",
        )
        for stream, content in ((sys.stdout, stdout), (sys.stderr, stderr)):
            if content:
                stream.write(content)
                if not content.endswith("\n"):
                    stream.write("\n")
                stream.flush()
        if busy:
            print(json.dumps({
                "error": "task conversation is open on another surface",
                "reason": "target_busy",
            }))
        return int(completed.returncode)
    except TaskCancelledError:
        _record_completion(state_path, task_id, exit_code=130, reason="cancelled")
        print(json.dumps({"reason": "cancelled", "worker_started": False}))
        return 130
    except TargetBusyError as exc:
        _record_completion(
            state_path, task_id, exit_code=75, reason="target_busy"
        )
        print(json.dumps({"error": str(exc), "reason": "target_busy"}))
        return 75
    except Exception as exc:
        _record_completion(
            state_path, task_id, exit_code=1, reason="runner_error"
        )
        print(
            json.dumps({
                "error": f"{type(exc).__name__}: {exc}",
                "reason": "runner_error",
            }),
            file=sys.stderr,
        )
        return 1
    finally:
        with contextlib.suppress(OSError):
            message_file.unlink()


if __name__ == "__main__":
    raise SystemExit(run())
