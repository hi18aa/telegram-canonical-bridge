"""一般 Hermes session 可用的專門 Bot 派工 sidecar。

此模組不取代 Telegram adapter，也不覆寫核心 ``message_agent``。主 Agent
透過外掛工具建立可追蹤任務，runner 再使用 Hermes 公開 CLI，為每個 task
建立一個隔離 conversation 並真正執行目標 Bot turn。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .config import hermes_machine_root, shared_state_path
from .exact_payload import (
    MAX_EXACT_PAYLOAD_BYTES,
    ExactPayloadError,
    create_exact_text_payload,
    read_exact_text_payload,
)
from .native_delivery import kick_native_outbox
from .task_model import TASK_STATUS_LABELS, normalize_task_id, sanitize_progress, task_marker


TOOLSET_NAME = "telegram_canonical_bridge"
AGENT_TASK_TOOL_NAMES = frozenset({
    "agent_task_start",
    "agent_task_status",
    "agent_task_message",
    "agent_task_cancel",
})
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
DELIVERY_TARGET_RE = re.compile(r"^(?:local|[a-z][a-z0-9_-]{0,31}(?::\S{1,256})?)$")
MESSAGE_MAX_CHARS = 16_000
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class AgentTaskSettings:
    delivery_target: str = "local"
    controller_profiles: tuple[str, ...] = ("default",)
    agent_roles: tuple[tuple[str, str], ...] = ()
    lock_timeout_seconds: float = 3600.0
    copy_attachments: bool = True

    @property
    def roles(self) -> dict[str, str]:
        return dict(self.agent_roles)


_CTX: Any = None
_SETTINGS = AgentTaskSettings()


def _bool_value(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _profile_list(value: Any) -> tuple[str, ...]:
    raw = value.split(",") if isinstance(value, str) else value
    if not isinstance(raw, (list, tuple, set)):
        return ("default",)
    profiles = tuple(
        str(item).strip() for item in raw
        if PROFILE_RE.fullmatch(str(item).strip())
    )
    return profiles or ("default",)


def _agent_roles(value: Any) -> tuple[tuple[str, str], ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(
            (str(name).strip(), "") for name in value
            if PROFILE_RE.fullmatch(str(name).strip())
        )
    if not isinstance(value, dict):
        return ()
    rows: list[tuple[str, str]] = []
    for raw_name, raw_details in value.items():
        name = str(raw_name).strip()
        if not PROFILE_RE.fullmatch(name):
            continue
        if isinstance(raw_details, dict):
            role = str(raw_details.get("role") or raw_details.get("description") or "")
        else:
            role = str(raw_details or "")
        rows.append((name, sanitize_progress(role, limit=240)))
    return tuple(rows)


def configure(ctx: Any) -> AgentTaskSettings:
    """讀取 profile-scoped plugin settings；格式錯誤時採安全預設。"""

    global _CTX, _SETTINGS
    _CTX = ctx
    target = str(ctx.get_config("delivery_target", "local") or "local").strip()
    if not DELIVERY_TARGET_RE.fullmatch(target):
        target = "local"
    try:
        lock_timeout = float(ctx.get_config("lock_timeout_seconds", 3600.0))
    except (TypeError, ValueError):
        lock_timeout = 3600.0
    _SETTINGS = AgentTaskSettings(
        delivery_target=target,
        controller_profiles=_profile_list(ctx.get_config("controller_profiles", ["default"])),
        agent_roles=_agent_roles(ctx.get_config("agents", {})),
        lock_timeout_seconds=min(max(lock_timeout, 30.0), 86_400.0),
        copy_attachments=_bool_value(ctx.get_config("copy_attachments", True), True),
    )
    return _SETTINGS


def _machine_root() -> Path:
    return hermes_machine_root().resolve()


def _profile_home(profile: str) -> Path:
    root = _machine_root()
    return root if profile == "default" else root / "profiles" / profile


def _profile_description(profile: str) -> str:
    path = _profile_home(profile) / "profile.yaml"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("description:"):
                return line.partition(":")[2].strip().strip("'\"")[:240]
    except OSError:
        pass
    return ""


def available_agents(origin_profile: str = "default") -> dict[str, str]:
    """回傳允許派工且目前存在的本機 profiles。"""

    configured = _SETTINGS.roles
    if configured:
        names = list(configured)
    else:
        root = _machine_root()
        names = ["default"] if (root / "profile.yaml").is_file() else []
        profiles_root = root / "profiles"
        if profiles_root.is_dir():
            names.extend(
                entry.name for entry in profiles_root.iterdir()
                if entry.is_dir() and PROFILE_RE.fullmatch(entry.name)
                and (entry / "profile.yaml").is_file()
            )
    result: dict[str, str] = {}
    for name in sorted(set(names), key=str.lower):
        if name == origin_profile or not _profile_home(name).is_dir():
            continue
        result[name] = configured.get(name) or _profile_description(name)
    return result


def system_prompt_section(session_info: Mapping[str, Any]) -> str:
    profile = str(session_info.get("profile_name") or "default")
    if profile not in _SETTINGS.controller_profiles:
        return ""
    # 每個正常 Controller turn 都是修復暫時性 Telegram 中斷的自然喚醒點。
    # 這只啟動非阻塞 runner；跨程序鎖會避免重複傳送。
    try:
        state, _current = _state_and_profile()
        kick_native_outbox(state)
    except Exception:
        pass
    roster = available_agents(profile)
    if not roster:
        return ""
    agents = "\n".join(
        f"- @{name}" + (f"：{role}" if role else "") for name, role in roster.items()
    )
    return (
        "# 專門 Bot 派工\n"
        "這個 session 可用 agent_task_start 把適合的工作非同步交給專門 Bot。"
        "這不會取代目前的 Telegram 對話。\n"
        f"可用 Bot：\n{agents}\n"
        "規則：sent 只代表背景 runner 已建立；Bot 開始、里程碑與完成會以任務事件提供證據。"
        "派工後簡短告知使用者 task_id，然後結束本 turn，不要阻塞輪詢。"
        "背景完成通知會喚醒同一個來源 session；收到後必須把 Bot 的實際結果轉告使用者。"
        "透過 bridge 派送需要逐字對外使用的內容時，必須把純正文放在 "
        "agent_task_start.exact_payload.text；message 只放摘要與操作要求。"
        "不要要求使用者手動建立檔案，也不要把逐字正文重複貼進 message。"
        "處理既有 task 前先用 agent_task_status 看 lifecycle：active 只補 inbox；"
        "settling 等待 runner 收斂；resumable 可用 agent_task_message 明確要求查詢、修復或 "
        "reconcile；它只接續同一 task conversation，不會重送原始任務；terminated 已結案。"
        "外部副作用結果不明時，接續指示不得要求盲目 replay。只有使用者明確要求時才呼叫 "
        "agent_task_cancel；一般新訊息不代表取消舊任務。"
    )


def prepare_start(
    state: Any,
    args: dict[str, Any],
    *,
    origin_profile: str,
    session_id: str,
    turn_id: str,
    tool_call_id: str,
) -> dict[str, Any] | None:
    """在 pre_tool_call 建立冪等 intent，並把 opaque task ID 注入 handler。"""

    target = str((args or {}).get("target") or "").strip().lstrip("@")
    message = str((args or {}).get("message") or "").strip()
    if not PROFILE_RE.fullmatch(target) or not message:
        return None
    parent = state.task_for_worker(session_id, turn_id)
    if parent is not None and parent.terminal:
        parent = None
    task, _created = state.create_task(
        delivery_target=_SETTINGS.delivery_target,
        origin_profile=origin_profile,
        origin_session_id=str(session_id or ""),
        origin_turn_id=str(turn_id or ""),
        origin_tool_call_id=str(tool_call_id or ""),
        target=target,
        parent_task_id=parent.id if parent else None,
        initial_progress="主 Agent 正在建立專門 Bot 派工程序。",
        initial_evidence="hook:agent_task_start intent",
        queue_outbox=False,
    )
    return {"action": "modify", "args": {"_bridge_task_id": task.id}}


def _state_and_profile():
    from .task_features import _current_profile, _state

    return _state(), _current_profile()


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _payload_contract_for_task(task_id: str) -> dict[str, Any] | None:
    try:
        return read_exact_text_payload(shared_state_path().parent, task_id)
    except ExactPayloadError as exc:
        return {"verified": False, "error": str(exc)}


def _task_payload(task: Any) -> dict[str, Any]:
    result = {
        "task_id": task.id,
        "target": task.target,
        "status": task.status,
        "status_label": (
            "可接續（舊版 worker 中斷）"
            if task.status == "failed" and task.resumable
            else TASK_STATUS_LABELS.get(task.status, task.status)
        ),
        "lifecycle": task.lifecycle,
        "resumable": task.resumable,
        "final": task.final,
        "progress": task.progress,
        "evidence": task.evidence,
        "process_id": task.process_id,
        "worker_started": task.worker_started,
        "final_observed": task.final_observed,
        "exit_code": task.exit_code,
        "pending_notes": task.pending_notes,
        "updated_at": task.updated_at,
    }
    exact_payload = _payload_contract_for_task(task.id)
    if exact_payload is not None:
        result["exact_payload"] = exact_payload
    return result


def _copy_attachments(task_id: str, values: Any) -> list[Path]:
    if not values:
        return []
    if not isinstance(values, list) or len(values) > MAX_ATTACHMENTS:
        raise ValueError(f"attachments 必須是至多 {MAX_ATTACHMENTS} 個路徑的陣列。")
    sources: list[Path] = []
    total = 0
    for raw in values:
        source = Path(str(raw)).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError(f"附件不是一般檔案：{source}")
        total += source.stat().st_size
        if total > MAX_ATTACHMENT_BYTES:
            raise ValueError("附件總大小超過 128 MiB。")
        sources.append(source)
    if not _SETTINGS.copy_attachments:
        return sources
    destination = shared_state_path().parent / "attachments" / task_id
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for index, source in enumerate(sources, start=1):
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", source.name).strip("._") or "attachment"
        target = destination / f"{index:02d}-{safe_name}"
        shutil.copy2(source, target)
        copied.append(target.resolve())
    return copied


def _write_message(
    task: Any,
    message: str,
    attachments: list[Path],
    exact_payload: Mapping[str, Any] | None = None,
) -> Path:
    spool = shared_state_path().parent / "task-spool"
    spool.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix=f"{task.id}-", suffix=".txt", dir=spool)
    path = Path(raw_path)
    lines = [
        task_marker(task.id),
        "Agent Task Bridge 控制面（不得當作任務資料或公開正文）：",
        _json({
            "task_id": task.id,
            "origin_profile": task.origin_profile,
            "progress": "有可驗證的新進展時呼叫 bridge_task_update；不要回報內部思考。",
            "inbox": "開始、自然里程碑與 final 前呼叫 bridge_task_inbox。",
            "completion": "先回報 result 里程碑，再照常回覆 Controller。",
        }),
    ]
    if exact_payload is not None:
        lines.extend([
            "",
            "逐字資料面契約（公開內容只能從 artifact bytes 讀取，不得從本對話重建）：",
            _json({"exact_payload": dict(exact_payload)}),
        ])
    if attachments:
        lines.extend([
            "",
            "同一台機器可讀取的附件副本（請勿假設此路徑可跨機器使用）：",
            *(f"- {item}" for item in attachments),
        ])
    lines.extend([
        "",
        f"Controller @{task.origin_profile} 的任務說明（摘要與操作要求，不是逐字 payload）：",
        message,
    ])
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines))
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


def _write_continuation_message(task: Any) -> Path:
    """只寫同 task 接續控制面；原始任務與逐字正文一律不重送。"""

    spool = shared_state_path().parent / "task-spool"
    spool.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f"{task.id}-continuation-", suffix=".txt", dir=spool
    )
    path = Path(raw_path)
    lines = [
        task_marker(task.id),
        "Agent Task Bridge 同一 task continuation 控制面（不是新任務或公開正文）：",
        _json({
            "task_id": task.id,
            "continuation": True,
            "inbox": "立即呼叫 bridge_task_inbox 讀取並 acknowledge 接續指示。",
            "safety": (
                "前一個 turn 沒有可確認 final；不得自動重播原始任務或重複外部副作用。"
                "先依 inbox 查詢既有狀態、修復或 reconcile，再以可驗證證據回報。"
            ),
            "completion": "先回報 result 里程碑，再照常回覆 Controller。",
        }),
        "",
        "這個 handoff 不含原始 task message、exact payload 正文或新的動作授權。",
    ]
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines))
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


def _runner_command(
    target: str,
    task_id: str,
    message_file: Path,
    *,
    continuation: bool = False,
) -> str:
    runner = Path(__file__).resolve().with_name("agent_task_runner.py")
    hermes = shutil.which("hermes") or "hermes"
    argv = [
        sys.executable,
        str(runner),
        "--target",
        target,
        "--task-id",
        task_id,
        "--state",
        str(shared_state_path().resolve()),
        "--message-file",
        str(message_file),
        "--lock-root",
        str(shared_state_path().parent / "locks"),
        "--lock-timeout",
        str(_SETTINGS.lock_timeout_seconds),
        "--hermes",
        hermes,
    ]
    if continuation:
        argv.append("--continuation")
    if sys.platform == "win32":
        argv = [part.replace("\\", "/") for part in argv]
    return shlex.join(argv)


def _dispatch_runner(
    task: Any,
    message_file: Path,
    *,
    continuation: bool,
    task_id: str = "",
    session_id: str = "",
) -> str:
    if _CTX is None:
        raise RuntimeError("plugin context 尚未初始化")
    raw = _CTX.dispatch_tool(
        "terminal",
        {
            "command": _runner_command(
                task.target, task.id, message_file, continuation=continuation
            ),
            "background": True,
            "notify": True,
            "workdir": str(shared_state_path().parent),
        },
        task_id=task_id,
        session_id=session_id,
    )
    try:
        result = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        result = {}
    process_id = str(result.get("session_id") or "").strip()
    error = str(result.get("error") or "").strip()
    if error or not process_id:
        raise RuntimeError(error or "terminal 未回傳 background process ID")
    return process_id


def _task_for_start(state: Any, args: dict[str, Any], profile: str, session_id: str):
    task_id = normalize_task_id(args.get("_bridge_task_id"))
    task = state.task(task_id) if task_id else None
    target = str(args.get("target") or "").strip().lstrip("@")
    if task is not None and (
        task.origin_profile == profile
        and task.origin_session_id == session_id
        and task.target == target
    ):
        return task
    # Plugin Doctor 或直接 registry 呼叫可能沒有 pre_tool_call identity。
    task, _created = state.create_task(
        delivery_target=_SETTINGS.delivery_target,
        origin_profile=profile,
        origin_session_id=session_id,
        origin_turn_id="",
        origin_tool_call_id=f"fallback-{secrets.token_hex(12)}",
        target=target,
        initial_progress="主 Agent 正在建立專門 Bot 派工程序。",
        initial_evidence="agent_task_start handler fallback",
        queue_outbox=False,
    )
    return task


def agent_task_start(args: dict[str, Any], **kwargs: Any) -> str:
    state, profile = _state_and_profile()
    target = str(args.get("target") or "").strip().lstrip("@")
    message = str(args.get("message") or "").strip()
    task = _task_for_start(state, args, profile, str(kwargs.get("session_id") or ""))
    if task.process_id:
        return _json({
            "ok": task.status not in {"failed", "cancelled", "unconfirmed"},
            "status": task.status,
            "task_id": task.id,
            "target": task.target,
            "process_id": task.process_id,
            "deduplicated": True,
            "detail": "相同的 tool call 已建立過 runner，本次未重複派工。",
            "exact_payload": _payload_contract_for_task(task.id),
        })
    if task.terminal:
        return _json({
            "ok": False,
            "task_id": task.id,
            "status": task.status,
            "error": "這個冪等派工 intent 已進入終態，不會自動重送。",
        })
    if profile not in _SETTINGS.controller_profiles:
        state.transition_task(
            task.id, status="failed", progress="這個 profile 未獲准建立 sidecar 任務。",
            evidence="agent_task_start policy", last_error="controller profile not allowed",
        )
        kick_native_outbox(state)
        return _json({"ok": False, "task_id": task.id, "error": "此 profile 未列入 controller_profiles。"})
    roster = available_agents(profile)
    if target not in roster:
        state.transition_task(
            task.id, status="failed", progress="目標不在可用 Bot roster。",
            evidence="agent_task_start validation", last_error=f"unknown target: {target}",
        )
        kick_native_outbox(state)
        return _json({"ok": False, "task_id": task.id, "error": "目標 Bot 不存在或未允許。", "agents": roster})
    if not message or len(message) > MESSAGE_MAX_CHARS:
        state.transition_task(
            task.id, status="failed", progress="派工內容無效。",
            evidence="agent_task_start validation", last_error="empty or oversized message",
        )
        kick_native_outbox(state)
        return _json({"ok": False, "task_id": task.id, "error": f"message 必須為 1–{MESSAGE_MAX_CHARS} 字元。"})
    message_file: Path | None = None
    exact_payload: dict[str, Any] | None = None
    try:
        attachments = _copy_attachments(task.id, args.get("attachments"))
        exact_payload = create_exact_text_payload(
            shared_state_path().parent,
            task.id,
            args.get("exact_payload"),
        )
        message_file = _write_message(task, message, attachments, exact_payload)
        process_id = _dispatch_runner(
            task,
            message_file,
            continuation=False,
            task_id=str(kwargs.get("task_id") or ""),
            session_id=str(kwargs.get("session_id") or ""),
        )
        # runner 已接管 payload 清理責任。
        message_file = None
        updated = state.transition_task(
            task.id,
            status="dispatched",
            process_id=process_id,
            progress=(
                f"Hermes 已建立背景 runner；等待隔離對話「TCB Task {task.id}」"
                "的 Bot turn 啟動證據。"
            ),
            evidence="agent_task_start background acknowledgement",
        )
        kick_native_outbox(state)
        response = {
            "ok": True,
            "status": "sent",
            "task_id": task.id,
            "target": target,
            "process_id": process_id,
            "detail": (
                "背景 runner 已建立；這不代表 Bot 已完成。每個 task 使用獨立對話，"
                "請把 task_id 告知使用者並結束本 turn，"
                "完成通知會喚醒同一個來源 session。"
            ),
            "task": _task_payload(updated or task),
        }
        if exact_payload is not None:
            response["exact_payload"] = exact_payload
        return _json(response)
    except Exception as exc:
        if message_file is not None:
            with contextlib.suppress(OSError):
                message_file.unlink()
        updated = state.transition_task(
            task.id,
            status="failed",
            progress="無法建立專門 Bot 背景 runner。",
            evidence="agent_task_start spawn failure",
            last_error=f"{type(exc).__name__}: {exc}",
        )
        kick_native_outbox(state)
        response = {
            "ok": False,
            "task_id": task.id,
            "error": updated.last_error if updated else str(exc),
        }
        if exact_payload is not None:
            response["exact_payload"] = exact_payload
        return _json(response)


def agent_task_status(args: dict[str, Any], **_kwargs: Any) -> str:
    state, profile = _state_and_profile()
    requested = normalize_task_id(args.get("task_id"))
    tasks = [state.task(requested)] if requested else state.list_tasks(limit=10)
    rows = [
        _task_payload(task) for task in tasks
        if task is not None and task.origin_profile == profile
    ]
    return _json({"ok": bool(rows), "tasks": rows})


def agent_task_message(args: dict[str, Any], **kwargs: Any) -> str:
    state, profile = _state_and_profile()
    task_id = normalize_task_id(args.get("task_id"))
    message = str(args.get("message") or "").strip()
    if not task_id or not message:
        return _json({"ok": False, "error": "task_id 與 message 都是必填。"})
    current = state.task(task_id)
    if current is not None and current.origin_profile == profile and current.status == "settling":
        return _json({
            "ok": False,
            "task_id": current.id,
            "status": current.status,
            "lifecycle": current.lifecycle,
            "resumable": False,
            "reason": "runner_settling",
            "detail": "原 runner 尚在收斂；等完成事件後查詢同一 task，不要另開或重送。",
        })
    task, accepted, detail = state.add_agent_task_note(
        task_id=task_id,
        origin_profile=profile,
        text=message,
        note_id=(
            f"{kwargs.get('session_id')}:{kwargs.get('tool_call_id')}"
            if kwargs.get("session_id") and kwargs.get("tool_call_id")
            else secrets.token_hex(12)
        ),
    )
    if not accepted:
        return _json({
            "ok": False,
            "task_id": task.id if task else task_id,
            "status": task.status if task else "unknown",
            "lifecycle": task.lifecycle if task else "unknown",
            "resumable": task.resumable if task else False,
            "detail": detail,
        })

    continuation_started = False
    process_id = task.process_id if task else None
    if task is not None and task.resumable:
        resume_from = task.status
        claimed, should_spawn, claim_detail = state.claim_task_continuation(
            task_id=task.id,
            origin_profile=profile,
        )
        if should_spawn and claimed is not None:
            message_file: Path | None = None
            runner_dispatched = False
            try:
                message_file = _write_continuation_message(claimed)
                process_id = _dispatch_runner(
                    claimed,
                    message_file,
                    continuation=True,
                    task_id=str(kwargs.get("task_id") or ""),
                    session_id=str(kwargs.get("session_id") or ""),
                )
                runner_dispatched = True
                # continuation runner 已接管 spool 清理責任。
                message_file = None
                task = state.transition_task(
                    claimed.id,
                    status="continuing",
                    process_id=process_id,
                    progress=(
                        "Hermes 已建立同一 task 的 continuation runner；等待既有隔離 "
                        "conversation 啟動新 turn。原始任務未重送。"
                    ),
                    evidence="agent_task_message continuation acknowledgement",
                    last_error="",
                )
                if task is None or task.process_id != process_id:
                    raise RuntimeError("continuation runner identity 未寫入 task ledger")
                continuation_started = True
                detail = (
                    "接續 runner 已建立；它只會讀取 task inbox，原始任務與逐字正文未重送。"
                )
            except Exception as exc:
                if message_file is not None:
                    with contextlib.suppress(OSError):
                        message_file.unlink()
                if runner_dispatched:
                    # background handle 已回傳後就不能把 claim 重新開放，否則 caller
                    # retry 可能建立第二個 runner。runner 本身仍會用 task_id 直寫
                    # completion；目前只保留 continuing 並要求查詢，絕不 replay。
                    task = state.task(claimed.id)
                    detail = (
                        "continuation runner 已建立，但 process identity 未能完整寫入 ledger；"
                        "請查詢同一 task，勿再次啟動或重播原任務。"
                        f"（{type(exc).__name__}: {exc}）"
                    )
                    kick_native_outbox(state)
                    return _json({
                        "ok": False,
                        "task_id": claimed.id,
                        "status": task.status if task else "continuing",
                        "lifecycle": task.lifecycle if task else "active",
                        "resumable": task.resumable if task else False,
                        "continuation_started": True,
                        "process_id": process_id,
                        "detail": detail,
                    })
                fallback_status = "interrupted" if resume_from == "failed" else resume_from
                task = state.transition_task(
                    claimed.id,
                    status=fallback_status,
                    progress=(
                        "無法建立同一 task 的 continuation runner；留言仍保留，"
                        "不會自動重試或重播原始任務。"
                    ),
                    evidence="agent_task_message continuation spawn failure",
                    last_error=f"{type(exc).__name__}: {exc}",
                )
                detail = task.last_error if task else str(exc)
                kick_native_outbox(state)
                return _json({
                    "ok": False,
                    "task_id": claimed.id,
                    "status": task.status if task else fallback_status,
                    "lifecycle": task.lifecycle if task else "resumable",
                    "resumable": task.resumable if task else True,
                    "continuation_started": False,
                    "detail": detail,
                })
        else:
            task = claimed or state.task(task.id)
            detail = claim_detail
    kick_native_outbox(state)
    return _json({
        "ok": True,
        "task_id": task.id if task else task_id,
        "status": task.status if task else "unknown",
        "lifecycle": task.lifecycle if task else "unknown",
        "resumable": task.resumable if task else False,
        "continuation_started": continuation_started,
        "process_id": task.process_id if task else process_id,
        "detail": detail,
        "delivery": (
            "已建立同一 task continuation；Bot 會從 bridge_task_inbox 取得指示。"
            if continuation_started
            else "補充內容會在 Bot 下一次 bridge_task_inbox 檢查時讀取；不是即時中斷。"
        ),
    })


def agent_task_cancel(args: dict[str, Any], **kwargs: Any) -> str:
    state, profile = _state_and_profile()
    task_id = normalize_task_id(args.get("task_id"))
    task = state.task(task_id) if task_id else None
    if task is None or task.origin_profile != profile:
        return _json({"ok": False, "error": "找不到這個主 Agent 建立的任務。"})
    if task.terminal or task.status == "returning":
        return _json({
            "ok": False,
            "task_id": task.id,
            "status": task.status,
            "reason": "too_late",
            "detail": "任務已結束或 Bot 已產生 final；取消不會回滾已完成的外部動作。",
        })
    updated = state.transition_task(
        task.id,
        status="stopping",
        progress="主 Agent 已收到明確取消要求，正在終止背景程序樹。",
        evidence="agent_task_cancel requested",
    )
    kick_native_outbox(state)
    if updated.status != "stopping":
        return _json({"ok": False, "task_id": task.id, "status": updated.status, "reason": "too_late"})
    # 同一 Controller 的 handle 仍可立即停止；跨程序則由原 runner 觀察 stopping 並確認。
    result = {"status": "unavailable"}
    try:
        if task.process_id and _CTX is not None:
            raw = _CTX.dispatch_tool(
                "process_manage",
                {"action": "kill", "session_id": task.process_id},
                task_id=kwargs.get("task_id"),
                session_id=kwargs.get("session_id"),
            )
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            result = parsed if isinstance(parsed, dict) else {"status": "unavailable"}
    except Exception as exc:
        result = {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
    result_status = str(result.get("status") or "").lower()
    if result_status == "killed":
        updated = state.transition_task(
            task.id,
            status="cancelled",
            progress="Hermes 已終止背景 runner 與其子程序樹。",
            evidence="process kill confirmed",
            exit_code=-15,
        )
        kick_native_outbox(state)
        return _json({"ok": True, "task_id": task.id, "status": updated.status,
                      "cancellationConfirmed": updated.status == "cancelled", "process": result_status})
    updated = state.task(task.id)
    return _json({
        "ok": True, "task_id": task.id, "status": updated.status,
        "cancellationConfirmed": updated.status == "cancelled",
        "reason": "awaiting-runner-confirmation" if updated.status == "stopping" else "runner-finished",
        "detail": "取消要求已保存；只有原 runner／程序管理確認停止後才算 cancelled。",
        "process": result,
    })


def _parse_tool_payload(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _render_status_command(raw: str, *, detailed: bool) -> str:
    payload = _parse_tool_payload(raw)
    tasks = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
    if not tasks:
        return "目前找不到這個 Controller 建立的任務。"
    blocks: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id") or "未知任務")
        label = str(task.get("status_label") or task.get("status") or "未知狀態")
        target = str(task.get("target") or "unknown")
        progress = str(task.get("progress") or "尚無明確進度")
        lines = [f"📋 {task_id}｜{label}", f"Bot：@{target}", f"進度：{progress}"]
        if detailed:
            lines.extend([
                f"Lifecycle：{task.get('lifecycle') or 'unknown'}",
                f"可接續：{'是' if task.get('resumable') else '否'}",
                f"Worker turn：{'已啟動' if task.get('worker_started') else '尚未觀察'}",
                f"Final：{'已觀察' if task.get('final_observed') else '尚無證據'}",
                f"Exit code：{task.get('exit_code') if task.get('exit_code') is not None else '尚無'}",
                f"待讀留言：{int(task.get('pending_notes') or 0)}",
                f"證據：{task.get('evidence') or '尚無'}",
            ])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else "目前沒有任務。"


def _render_action_command(raw: str, *, success_label: str) -> str:
    payload = _parse_tool_payload(raw)
    task_id = str(payload.get("task_id") or "")
    if payload.get("ok"):
        detail = str(payload.get("detail") or "").strip()
        return f"{success_label}{f' {task_id}' if task_id else ''}" + (f"\n{detail}" if detail else "")
    detail = str(payload.get("detail") or payload.get("error") or payload.get("reason") or "操作未完成")
    return f"⚠️ {task_id + '｜' if task_id else ''}{detail}"


def _slash_command(raw_args: str) -> str:
    parts = str(raw_args or "").strip().split(maxsplit=2)
    if not parts or parts[0].lower() in {"list", "status"} and len(parts) == 1:
        return _render_status_command(agent_task_status({}), detailed=False)
    action = parts[0].lower()
    if action == "status" and len(parts) >= 2:
        return _render_status_command(
            agent_task_status({"task_id": parts[1]}), detailed=True
        )
    if action == "cancel" and len(parts) >= 2:
        raw = agent_task_cancel({"task_id": parts[1]})
        confirmed = _parse_tool_payload(raw).get("cancellationConfirmed") is True
        return _render_action_command(
            raw, success_label="🛑 已取消" if confirmed else "⏳ 已提出取消要求"
        )
    if action == "message" and len(parts) >= 3:
        return _render_action_command(
            agent_task_message({"task_id": parts[1], "message": parts[2]}),
            success_label="✉️ 已加入任務留言",
        )
    if normalize_task_id(parts[0]):
        return _render_status_command(
            agent_task_status({"task_id": parts[0]}), detailed=True
        )
    return "用法：/agenttask [list|status <ID>|message <ID> <內容>|cancel <ID>]"


def register_agent_tasks(ctx: Any) -> None:
    configure(ctx)
    ctx.register_system_prompt_section(
        id="telegram-canonical-bridge.agent-tasks",
        content=system_prompt_section,
        position="after_memory",
        max_chars=4000,
    )
    ctx.register_tool(
        name="agent_task_start",
        toolset=TOOLSET_NAME,
        schema={
            "name": "agent_task_start",
            "description": (
                "把符合 roster 分工的工作非同步派給專門 Bot。每個 task 使用隔離 conversation，"
                "回傳 sent 只代表 runner 已建立，不代表工作完成。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "system prompt roster 中的 profile 名稱。"},
                    "message": {"type": "string", "description": "主 Agent 整理後的具體任務，最多 16000 字元。"},
                    "exact_payload": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["exact_text"],
                                "default": "exact_text",
                            },
                            "text": {
                                "type": "string",
                                "description": (
                                    "選填；需要逐字對外使用的純正文。Bridge 會原樣寫成獨立 UTF-8 "
                                    f"artifact（最多 {MAX_EXACT_PAYLOAD_BYTES} bytes），不可在 message 重複。"
                                ),
                            },
                        },
                        "required": ["text"],
                        "description": (
                            "選填；資料面契約。只在需要逐字公開／交付文字時使用，"
                            "使用者不需要手動建立檔案。"
                        ),
                    },
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": MAX_ATTACHMENTS,
                        "description": "選填；同一台機器上的既有檔案路徑。",
                    },
                },
                "required": ["target", "message"],
            },
        },
        handler=agent_task_start,
        description="派工給專門 Hermes Bot",
        emoji="🧭",
    )
    ctx.register_tool(
        name="agent_task_status",
        toolset=TOOLSET_NAME,
        schema={
            "name": "agent_task_status",
            "description": "查詢自己建立的專門 Bot 任務與可驗證狀態。",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": [],
            },
        },
        handler=agent_task_status,
        description="查詢專門 Bot 任務",
        emoji="📋",
    )
    ctx.register_tool(
        name="agent_task_message",
        toolset=TOOLSET_NAME,
        schema={
            "name": "agent_task_message",
            "description": (
                "替 active 任務加入 inbox 指示；若 task lifecycle=resumable，"
                "則以該指示啟動同一 task conversation 的 continuation。"
                "不會重送原始任務或自動 replay 外部副作用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["task_id", "message"],
            },
        },
        handler=agent_task_message,
        description="補充或安全接續專門 Bot 任務",
        emoji="✉️",
    )
    ctx.register_tool(
        name="agent_task_cancel",
        toolset=TOOLSET_NAME,
        schema={
            "name": "agent_task_cancel",
            "description": "只有使用者明確要求時取消任務。stopping 只代表要求已保存；查到 cancelled 才能宣稱已停止。不會回滾外部副作用。",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        },
        handler=agent_task_cancel,
        description="取消專門 Bot 任務",
        emoji="🛑",
    )
    ctx.register_command(
        name="agenttask",
        handler=_slash_command,
        description="查詢、補充或取消專門 Bot 任務",
        args_hint="[list|status <ID>|message <ID> <內容>|cancel <ID>]",
        argument_mode="text",
    )
