"""Sidecar 的 stdlib-only 背景 runner。

runner 只負責跨程序序列化與呼叫 Hermes 公開 CLI；任務內容放在權限受限的
暫存檔，不會插入 shell command line。Hermes 的背景程序 registry 會持有並
tree-kill 這個 runner 及其子程序，因此取消可終止整棵工作程序樹。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
TASK_ID_RE = re.compile(r"^TCB-[0-9]{8}-[A-F0-9]{6}$", re.IGNORECASE)
_PLUGIN_TOOLSETS = {"telegram_canonical_bridge"}


class TargetBusyError(TimeoutError):
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
    def __init__(self, root: Path, profile: str, timeout_seconds: float) -> None:
        digest = hashlib.sha256(profile.lower().encode("utf-8")).hexdigest()[:20]
        self.path = root / f"{digest}.lock"
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.stream.write(b"\0")
            self.stream.flush()
        deadline = time.monotonic() + self.timeout_seconds
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
    parser.add_argument("--message-file", required=True)
    parser.add_argument("--lock-root", required=True)
    parser.add_argument("--lock-timeout", type=float, default=3600.0)
    parser.add_argument("--hermes", default="")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    target = str(args.target).strip()
    task_id = str(args.task_id).strip().upper()
    # ``hermes chat --in ~`` 會先切換工作目錄；query-file 必須固定為絕對路徑。
    message_file = Path(args.message_file).resolve()
    if not PROFILE_RE.fullmatch(target):
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
        with _ProfileLock(Path(args.lock_root), target, args.lock_timeout):
            completed = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        stdout = _filter_plugin_toolset_startup_warning(completed.stdout)
        stderr = _filter_plugin_toolset_startup_warning(completed.stderr)
        for stream, content in ((sys.stdout, stdout), (sys.stderr, stderr)):
            if content:
                stream.write(content)
                if not content.endswith("\n"):
                    stream.write("\n")
                stream.flush()
        if completed.returncode != 0 and "already has a live owner" in stderr.lower():
            print(json.dumps({
                "error": "task conversation is open on another surface",
                "reason": "target_busy",
            }))
        return int(completed.returncode)
    except TargetBusyError as exc:
        print(json.dumps({"error": str(exc), "reason": "target_busy"}))
        return 75
    except Exception as exc:
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
