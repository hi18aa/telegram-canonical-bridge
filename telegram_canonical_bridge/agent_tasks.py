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
        "使用 agent_task_status 查詢、agent_task_message 補充指示；只有使用者明確要求時才呼叫 "
        "agent_task_cancel。一般新訊息不代表取消舊任務。"
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


def _task_payload(task: Any) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "target": task.target,
        "status": task.status,
        "status_label": TASK_STATUS_LABELS.get(task.status, task.status),
        "progress": task.progress,
        "evidence": task.evidence,
        "process_id": task.process_id,
        "worker_started": task.worker_started,
        "final_observed": task.final_observed,
        "exit_code": task.exit_code,
        "pending_notes": task.pending_notes,
        "updated_at": task.updated_at,
    }


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


def _write_message(task: Any, message: str, attachments: list[Path]) -> Path:
    spool = shared_state_path().parent / "task-spool"
    spool.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix=f"{task.id}-", suffix=".txt", dir=spool)
    path = Path(raw_path)
    lines = [
        task_marker(task.id),
        f"Message from main Agent @{task.origin_profile}：",
        message,
    ]
    if attachments:
        lines.extend([
            "",
            "同一台機器可讀取的附件副本（請勿假設此路徑可跨機器使用）：",
            *(f"- {item}" for item in attachments),
        ])
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines))
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


def _runner_command(target: str, task_id: str, message_file: Path) -> str:
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
    if sys.platform == "win32":
        argv = [part.replace("\\", "/") for part in argv]
    return shlex.join(argv)


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
    try:
        attachments = _copy_attachments(task.id, args.get("attachments"))
        message_file = _write_message(task, message, attachments)
        if _CTX is None:
            raise RuntimeError("plugin context 尚未初始化")
        raw = _CTX.dispatch_tool(
            "terminal",
            {
                "command": _runner_command(target, task.id, message_file),
                "background": True,
                "notify": True,
                "workdir": str(shared_state_path().parent),
            },
            task_id=kwargs.get("task_id"),
            session_id=kwargs.get("session_id"),
        )
        try:
            result = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, ValueError):
            result = {}
        process_id = str(result.get("session_id") or "").strip()
        error = str(result.get("error") or "").strip()
        if error or not process_id:
            raise RuntimeError(error or "terminal 未回傳 background process ID")
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
        return _json({
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
        })
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
        return _json({"ok": False, "task_id": task.id, "error": updated.last_error if updated else str(exc)})


def agent_task_status(args: dict[str, Any], **_kwargs: Any) -> str:
    state, profile = _state_and_profile()
    requested = normalize_task_id(args.get("task_id"))
    tasks = [state.task(requested)] if requested else state.list_tasks(limit=10)
    rows = [
        _task_payload(task) for task in tasks
        if task is not None and task.origin_profile == profile
    ]
    return _json({"ok": bool(rows), "tasks": rows})


def agent_task_message(args: dict[str, Any], **_kwargs: Any) -> str:
    state, profile = _state_and_profile()
    task_id = normalize_task_id(args.get("task_id"))
    message = str(args.get("message") or "").strip()
    if not task_id or not message:
        return _json({"ok": False, "error": "task_id 與 message 都是必填。"})
    task, accepted, detail = state.add_agent_task_note(
        task_id=task_id,
        origin_profile=profile,
        text=message,
        note_id=secrets.token_hex(12),
    )
    kick_native_outbox(state)
    return _json({
        "ok": accepted,
        "task_id": task.id if task else task_id,
        "status": task.status if task else "unknown",
        "detail": detail,
        "delivery": "補充內容會在 Bot 下一次 bridge_task_inbox 檢查時讀取；不是即時中斷。",
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
    if not task.process_id or _CTX is None:
        updated = state.transition_task(
            task.id,
            status="unconfirmed",
            progress="收到取消要求，但找不到可終止的背景 handle。",
            evidence="agent_task_cancel missing process",
            last_error="cancellation could not be confirmed",
        )
        kick_native_outbox(state)
        return _json({"ok": False, "task_id": task.id, "status": updated.status, "reason": "unconfirmed"})
    state.transition_task(
        task.id,
        status="stopping",
        progress="主 Agent 已收到明確取消要求，正在終止背景程序樹。",
        evidence="agent_task_cancel requested",
    )
    kick_native_outbox(state)
    raw = _CTX.dispatch_tool(
        "process_manage",
        {"action": "kill", "session_id": task.process_id},
        task_id=kwargs.get("task_id"),
        session_id=kwargs.get("session_id"),
    )
    try:
        result = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        result = {}
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
        return _json({"ok": True, "task_id": task.id, "status": updated.status, "process": result_status})
    updated = state.transition_task(
        task.id,
        status="unconfirmed",
        progress="取消要求已送出，但背景程序可能已先結束；結果需要重新確認。",
        evidence="agent_task_cancel unconfirmed",
        last_error=str(result.get("error") or result_status or "unknown process result"),
    )
    kick_native_outbox(state)
    return _json({"ok": False, "task_id": task.id, "status": updated.status, "reason": "unconfirmed", "process": result})


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
        return _render_action_command(
            agent_task_cancel({"task_id": parts[1]}), success_label="🛑 已取消"
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
            "description": "替執行中的任務加入補充指示；Bot 會在下一個 inbox 檢查點讀取。",
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
        description="補充專門 Bot 任務指示",
        emoji="✉️",
    )
    ctx.register_tool(
        name="agent_task_cancel",
        toolset=TOOLSET_NAME,
        schema={
            "name": "agent_task_cancel",
            "description": "只有使用者明確要求時，終止任務的背景程序樹；不會回滾外部副作用。",
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
