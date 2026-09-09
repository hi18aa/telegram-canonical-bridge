"""Hermes hooks 與 OT 可呼叫的任務狀態工具。

V3 不覆寫 message_agent。它只在原生呼叫前加入 opaque task marker，並以
Hermes 公開 hook／tool API 把可驗證的狀態寫入共用 SQLite ledger。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from .config import shared_state_path
from .state import BridgeState
from .task_model import extract_task_id, sanitize_progress, task_marker


logger = logging.getLogger(__name__)

TOOLSET_NAME = "telegram_canonical_bridge"
BRIDGE_TOOL_NAMES = frozenset({
    "bridge_task_update",
    "bridge_task_inbox",
    "bridge_task_status",
})
MESSAGE_AGENT_MAX_CHARS = 16_000
_STATE_CACHE: dict[Path, BridgeState] = {}
_STATE_CACHE_LOCK = threading.Lock()


def _state() -> BridgeState:
    path = shared_state_path().resolve()
    with _STATE_CACHE_LOCK:
        state = _STATE_CACHE.get(path)
        if state is None:
            state = _STATE_CACHE[path] = BridgeState(path)
        return state


def _current_profile() -> str:
    try:
        from hermes_constants import get_hermes_home, named_profile_home

        home = Path(get_hermes_home())
        named = named_profile_home(home)
        return named.name if named is not None else "default"
    except Exception:
        return "default"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _parsed_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if not isinstance(result, str):
        return {}
    try:
        parsed = json.loads(result)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_activity(tool_name: str) -> tuple[str, str] | None:
    if tool_name.startswith("browser_"):
        return "OT 正在操作瀏覽器；bridge 只確認到 browser tool 呼叫，不推測畫面結果。", "hook:browser"
    if tool_name == "computer_use":
        return "OT 正在操作電腦介面；bridge 已觀察到 computer_use 呼叫。", "hook:computer_use"
    if tool_name in {"terminal", "process_manage"}:
        return "OT 正在執行或檢查本機程序。", "hook:terminal"
    if tool_name in {"write_file", "patch"}:
        return "OT 正在修改檔案。", "hook:file-write"
    if tool_name in {"web_search", "web_extract"}:
        return "OT 正在查詢外部資料。", "hook:web"
    return None


def _before_tool(
    tool_name: str,
    args: dict[str, Any],
    session_id: str = "",
    tool_call_id: str = "",
    turn_id: str = "",
    **_: Any,
) -> dict[str, Any] | None:
    """建立派工 intent、注入 marker，或記錄 OT 的高階工具活動。"""

    try:
        state = _state()
        if tool_name != "message_agent":
            if tool_name in BRIDGE_TOOL_NAMES:
                return None
            task = state.task_for_worker(session_id, turn_id)
            activity = _tool_activity(tool_name)
            if (
                task is not None
                and not task.terminal
                and activity is not None
                and not state.latest_explicit_progress(task.id)
            ):
                # OT 的明確、使用者可見里程碑比後續泛用 tool category 更有資訊；
                # 例如回報頁面標題後關閉 browser 所用的 terminal 不應抹掉結果。
                state.transition_task(
                    task.id, status="running", progress=activity[0], evidence=activity[1]
                )
            return None

        parent = state.task_for_worker(session_id, turn_id)
        if parent is not None and parent.terminal:
            parent = None
        route = (
            (parent.origin_profile, parent.chat_id, "")
            if parent is not None
            else state.route_for_canonical_session(session_id)
        )
        if route is None:
            # 只追蹤 Telegram 綁定的 canonical Bot Chat 與它派出的工作；
            # 其他 CLI／Desktop session 的 message_agent 維持完全原生。
            return None
        target = str((args or {}).get("target") or "").strip()
        body = str((args or {}).get("message") or "")
        task, _created = state.create_task(
            chat_id=route[1],
            origin_profile=_current_profile(),
            origin_session_id=str(session_id or ""),
            origin_turn_id=str(turn_id or ""),
            origin_tool_call_id=str(tool_call_id or ""),
            target=target,
            parent_task_id=parent.id if parent else None,
        )
        marker = task_marker(task.id)
        tracked_body = body if extract_task_id(body) == task.id else f"{marker}\n{body}"
        if len(tracked_body) > MESSAGE_AGENT_MAX_CHARS:
            state.transition_task(
                task.id,
                progress="派工內容接近 message_agent 上限，未注入 OT 追蹤 marker；仍保留程序狀態。",
                evidence="hook:marker-skipped-size",
            )
            return None
        if parent is not None and not parent.terminal:
            state.transition_task(
                parent.id,
                status="running",
                progress=f"OT 又透過 message_agent 派工給 @{target.lstrip('@')}。",
                evidence="hook:nested-message_agent",
            )
        return {"action": "modify", "args": {"message": tracked_body}}
    except Exception:
        logger.warning("Telegram canonical bridge pre_tool_call failed open", exc_info=True)
        return None


def _after_tool(
    tool_name: str,
    result: Any,
    session_id: str = "",
    tool_call_id: str = "",
    turn_id: str = "",
    status: str = "",
    error_message: str | None = None,
    **_: Any,
) -> None:
    try:
        state = _state()
        if tool_name == "message_agent":
            payload = _parsed_result(result)
            process_id = str(payload.get("process_id") or "").strip()
            if payload.get("status") == "sent" and process_id:
                state.acknowledge_dispatch(
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                    process_id=process_id,
                )
                return
            error = str(
                payload.get("error") or error_message or
                ("message_agent 未回傳可辨識的 sent acknowledgement" if status != "success" else "")
            ).strip()
            if error:
                state.fail_dispatch(
                    session_id=session_id, tool_call_id=tool_call_id, error=error
                )
            return

        if tool_name in BRIDGE_TOOL_NAMES:
            return
        task = state.task_for_worker(session_id, turn_id)
        if task is not None and not task.terminal and status in {"error", "blocked"}:
            state.transition_task(
                task.id,
                status="running",
                progress=f"OT 的 {tool_name} 呼叫未成功；OT 仍可調整後繼續。",
                evidence=f"hook:{status}",
            )
    except Exception:
        logger.warning("Telegram canonical bridge post_tool_call observer failed", exc_info=True)


def _before_llm(
    session_id: str,
    user_message: str,
    turn_id: str = "",
    **_: Any,
) -> dict[str, str] | None:
    task_id = extract_task_id(user_message)
    if not task_id:
        return None
    try:
        state = _state()
        task = state.task(task_id)
        if task is None:
            return None
        bound = state.bind_worker(
            task_id,
            worker_profile=_current_profile(),
            worker_session_id=str(session_id or ""),
            worker_turn_id=str(turn_id or ""),
        )
        if bound is None:
            return None
        return {
            "context": (
                f"這是 Telegram Canonical Bridge 追蹤任務 {task_id}。"
                "只在有可驗證的新進展時呼叫 bridge_task_update；不要回報內部思考。"
                "開始時、長時間操作前後、以及送出最終答覆前，呼叫 "
                f"bridge_task_inbox(task_id=\"{task_id}\") 檢查使用者留言。"
                "若要宣稱瀏覽器或其他介面已開啟，必須先有相應工具成功的證據。"
                "完成工作後，先用 bridge_task_update(status=\"result\") 回報一則簡潔、"
                "可公開的最終里程碑，"
                "再照常回覆 Controller；bridge 會由 post_llm_call 與背景程序狀態判定完成，"
                "不需自行宣稱 completed。若漏掉明確里程碑，bridge 只會把清理後的最終答覆"
                "摘要留在 Telegram 任務時間線，不會保存內部思考或原始工具資料。"
            )
        }
    except Exception:
        logger.warning("Telegram canonical bridge pre_llm_call failed open", exc_info=True)
        return None


def _after_llm(
    session_id: str,
    turn_id: str = "",
    assistant_response: str = "",
    **_: Any,
) -> None:
    try:
        _state().complete_worker_turn(
            session_id,
            turn_id,
            assistant_response=assistant_response,
        )
    except Exception:
        logger.warning("Telegram canonical bridge post_llm_call observer failed", exc_info=True)


def _on_session_end(
    session_id: str,
    turn_id: str = "",
    failed: bool = False,
    interrupted: bool = False,
    turn_exit_reason: str = "",
    **_: Any,
) -> None:
    if not failed and not interrupted:
        return
    try:
        state = _state()
        task = state.task_for_worker(session_id, turn_id)
        if task is None or task.terminal:
            return
        reason = sanitize_progress(turn_exit_reason, limit=280)
        if failed:
            state.transition_task(
                task.id,
                status="failed",
                progress="OT turn 在產生最終回覆前失敗。",
                evidence="hook:on_session_end failed",
                last_error=reason or "worker turn failed",
            )
        else:
            state.transition_task(
                task.id,
                status="blocked",
                progress="OT turn 被中斷；任務可能需要重新派送。",
                evidence="hook:on_session_end interrupted",
                last_error=reason,
            )
    except Exception:
        logger.warning("Telegram canonical bridge session-end observer failed", exc_info=True)


def _authorized_worker_task(
    state: BridgeState, task_id: str, session_id: str
) -> tuple[Any, str | None]:
    task = state.task(task_id)
    if task is None:
        return None, "找不到 task。"
    bound = state.task_for_worker(str(session_id or ""))
    if bound is None or bound.id != task.id:
        return None, "這個 Hermes session 不是該 task 的已綁定 OT turn。"
    return task, None


def bridge_task_update(args: dict[str, Any], **kwargs: Any) -> str:
    state = _state()
    task_id = str(args.get("task_id") or "")
    task, error = _authorized_worker_task(state, task_id, str(kwargs.get("session_id") or ""))
    if error:
        return _json({"ok": False, "error": error})
    requested = str(args.get("status") or "working").strip().lower()
    mapped = {
        "working": "running",
        "waiting": "waiting",
        "blocked": "blocked",
        "result": "running",
    }.get(requested)
    message = sanitize_progress(args.get("message"))
    if mapped is None or not message:
        return _json({"ok": False, "error": "status 或 message 無效。"})
    updated = state.transition_task(
        task.id,
        status=mapped,
        progress=message,
        evidence=(
            "OT explicit bridge_task_result"
            if requested == "result"
            else "OT explicit bridge_task_update"
        ),
    )
    return _json({
        "ok": updated is not None,
        "task_id": task.id,
        "status": updated.status if updated else task.status,
        "message": "Telegram 任務進度已排入時間線。",
    })


def bridge_task_inbox(args: dict[str, Any], **kwargs: Any) -> str:
    state = _state()
    task_id = str(args.get("task_id") or "")
    task, error = _authorized_worker_task(state, task_id, str(kwargs.get("session_id") or ""))
    if error:
        return _json({"ok": False, "error": error})
    acknowledge = bool(args.get("acknowledge", True))
    updated, notes = state.read_task_notes(task.id, mark_read=acknowledge)
    return _json({
        "ok": True,
        "task_id": task.id,
        "acknowledged": acknowledge and bool(notes),
        "messages": [{"id": note.id, "text": note.text} for note in notes],
        "remaining_unread": updated.pending_notes if updated else 0,
    })


def bridge_task_status(args: dict[str, Any], **_: Any) -> str:
    state = _state()
    requested = str(args.get("task_id") or "").strip()
    tasks = [state.task(requested)] if requested else state.list_tasks(limit=10)
    rows = [
        {
            "task_id": task.id,
            "target": task.target,
            "status": task.status,
            "progress": task.progress,
            "evidence": task.evidence,
            "process_id": task.process_id,
            "exit_code": task.exit_code,
            "pending_notes": task.pending_notes,
            "updated_at": task.updated_at,
        }
        for task in tasks if task is not None
    ]
    return _json({"ok": bool(rows), "tasks": rows})


def register_task_features(ctx: Any) -> None:
    """由 root plugin 與 deferred platform tools.py 共用的註冊入口。"""

    ctx.register_tool(
        name="bridge_task_update",
        toolset=TOOLSET_NAME,
        schema={
            "name": "bridge_task_update",
            "description": (
                "Update a Telegram-tracked message_agent task only when a meaningful, "
                "verifiable milestone changed. Never report private reasoning."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "TCB task ID from injected context."},
                    "status": {
                        "type": "string",
                        "enum": ["working", "waiting", "blocked", "result"],
                        "description": "Evidence-based current state.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Concise user-facing progress, max 500 characters.",
                    },
                },
                "required": ["task_id", "status", "message"],
            },
        },
        handler=bridge_task_update,
        description="回報可驗證的 OT 任務進度",
        emoji="🧭",
    )
    ctx.register_tool(
        name="bridge_task_inbox",
        toolset=TOOLSET_NAME,
        schema={
            "name": "bridge_task_inbox",
            "description": (
                "Read follow-up messages attached by the Telegram user to this tracked task. "
                "Check at natural milestones and before the final response."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "acknowledge": {"type": "boolean", "default": True},
                },
                "required": ["task_id"],
            },
        },
        handler=bridge_task_inbox,
        description="讀取 Telegram 使用者的任務留言",
        emoji="📥",
    )
    ctx.register_tool(
        name="bridge_task_status",
        toolset=TOOLSET_NAME,
        schema={
            "name": "bridge_task_status",
            "description": "Read evidence-backed status for one or recent Telegram bridge tasks.",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": [],
            },
        },
        handler=bridge_task_status,
        description="查詢 message_agent 任務 ledger",
        emoji="📋",
    )
    ctx.register_hook("pre_tool_call", _before_tool)
    ctx.register_hook("post_tool_call", _after_tool)
    ctx.register_hook("pre_llm_call", _before_llm)
    ctx.register_hook("post_llm_call", _after_llm)
    ctx.register_hook("on_session_end", _on_session_end)
