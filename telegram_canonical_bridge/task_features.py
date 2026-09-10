"""Hermes hooks 與專門 Bot 可呼叫的任務狀態工具。

外掛只觀察自己的 ``agent_task_*`` 工作流程，不包裝或覆寫內建
``message_agent``。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent_tasks import AGENT_TASK_TOOL_NAMES, prepare_start, system_prompt_section
from .config import shared_state_path
from .native_delivery import kick_native_outbox
from .state import BridgeState
from .task_model import extract_task_id, sanitize_progress


logger = logging.getLogger(__name__)

TOOLSET_NAME = "telegram_canonical_bridge"
BRIDGE_TOOL_NAMES = frozenset({
    "bridge_task_update",
    "bridge_task_inbox",
    "bridge_task_status",
}) | AGENT_TASK_TOOL_NAMES
_STATE_CACHE: dict[Path, BridgeState] = {}
_STATE_CACHE_LOCK = threading.Lock()


def _transition_task(state: BridgeState, task_id: str, **changes: Any):
    """更新 ledger 後，順手嘗試送出原生平台時間線；失敗仍留在 outbox。"""

    updated = state.transition_task(task_id, **changes)
    kick_native_outbox(state)
    return updated


@dataclass(frozen=True)
class _ProcessCompletion:
    process_id: str
    exit_code: int | None
    output: str
    reason: str


_CURRENT_COMPLETION_RE = re.compile(
    r"^\s*\[IMPORTANT:\s*Background process\s+"
    r"(?P<process_id>[A-Za-z0-9_.:-]+)\s+.*?"
    r"\(exit code\s+(?P<exit_code>-?\d+|\?)(?:,[^)]+)?\)\.",
    re.IGNORECASE | re.DOTALL,
)
_REASON_RE = re.compile(r"\[reason:\s*([a-z0-9_-]+)\]", re.IGNORECASE)


def _parse_process_completion(value: object) -> _ProcessCompletion | None:
    """讀取 Hermes 公開的背景完成通知；格式不符時完全忽略。

    解析器只用 opaque process ID 做 ledger 關聯，不讀 Command 欄位。
    """

    text = str(value or "")
    match = _CURRENT_COMPLETION_RE.match(text)
    if match is None:
        return None
    raw_exit = match.group("exit_code")
    exit_code = int(raw_exit) if raw_exit != "?" else None
    output = ""
    delimiter = "\nOutput:\n"
    position = text.find(delimiter, match.end())
    if position >= 0:
        output = text[position + len(delimiter):].rstrip()
        if output.endswith("]"):
            output = output[:-1].rstrip()
    reason_match = _REASON_RE.search(output)
    reason = reason_match.group(1).lower() if reason_match else ""
    if not reason:
        try:
            payload = json.loads(output)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            reason = str(payload.get("reason") or "").strip().lower()
    if not reason and "already has a live owner" in output.lower():
        reason = "target_busy"
    return _ProcessCompletion(
        process_id=match.group("process_id"),
        exit_code=exit_code,
        output=output,
        reason=reason,
    )


def _completion_reply_summary(output: str) -> str:
    """擷取 runner stdout 中預期可回給原提問者的 agent 答覆。

    非零 exit 的任意輸出不會走到此函式；錯誤只以類型化原因呈現，避免把
    command、stack trace 或意外秘密複製到使用者訊息平台。
    """

    text = str(output or "").strip()
    if not text or text == "(empty reply)":
        return ""
    reply_match = re.search(r"(?:^|\n)Reply from [^\n]+:\s*\n(?P<reply>[\s\S]+)$", text)
    if reply_match:
        text = reply_match.group("reply").strip()
    lines = [
        line
        for line in text.splitlines()
        if not line.strip().lower().startswith(
            ("warning: unknown toolsets:", "resuming session:", "session id:")
        )
    ]
    return sanitize_progress("\n".join(lines), limit=1000)


def _completion_failure_message(completion: _ProcessCompletion) -> tuple[str, str]:
    reason = completion.reason or "unknown"
    messages = {
        "target_busy": (
            "這個任務的隔離對話被其他 surface 持有，背景 runner 未能啟動 Bot turn。",
            "請關閉同名 task conversation 後再建立新任務；不要把 sent 當成已交付。",
        ),
        "runtime_offline": (
            "目標 Hermes runtime 離線，背景派工未完成。",
            "目標 runtime 離線。",
        ),
        "delivery_timeout": (
            "等待目標回覆逾時，bridge 無法確認 Bot 是否完成。",
            "delivery timeout；不要盲目重派。",
        ),
        "queued_expired": (
            "Hermes 的排隊派工已過期，Bot turn 未完成。",
            "queued delivery expired。",
        ),
        "provider_auth_or_access": (
            "Bot provider 驗證或存取失敗，沒有完成回覆。",
            "provider authentication/access failure。",
        ),
        "provider_quota_limit": (
            "Bot provider 額度不足，沒有完成回覆。",
            "provider quota limit。",
        ),
        "provider_rate_limit": (
            "Bot provider 暫時限流，沒有完成回覆。",
            "provider rate limit。",
        ),
        "provider_server_error": (
            "Bot provider 發生伺服器錯誤，沒有完成回覆。",
            "provider server error。",
        ),
        "missing_config": (
            "Bot 缺少執行所需設定，沒有開始或完成 turn。",
            "target profile configuration is incomplete。",
        ),
        "model_unavailable": (
            "Bot 指定模型不可用，沒有完成回覆。",
            "target model unavailable。",
        ),
        "context_overflow": (
            "Bot 對話 context overflow，重試後仍未完成。",
            "target context overflow。",
        ),
        "unknown": (
            "背景派工 runner 失敗，且 Hermes 未提供可分類原因。",
            "runner failed；請查看目標 Hermes log 與 Controller 的完成通知。",
        ),
    }
    return messages.get(reason, messages["unknown"])


def _observe_process_completion(state: BridgeState, value: object) -> bool:
    completion = _parse_process_completion(value)
    if completion is None:
        return False
    task = state.task_by_process_id(completion.process_id)
    if task is None:
        return True

    if completion.exit_code is not None and completion.exit_code != 0:
        if task.status == "returning":
            _transition_task(
                state,
                task.id,
                status="completed",
                evidence=(
                    "hook:post_llm_call + Hermes completion notification "
                    f"(exit {completion.exit_code})"
                ),
                exit_code=completion.exit_code,
            )
            return True
        progress, detail = _completion_failure_message(completion)
        _transition_task(
            state,
            task.id,
            status="failed",
            progress=progress,
            evidence=(
                "Hermes completion notification: exit "
                f"{completion.exit_code}; reason={completion.reason or 'unknown'}"
            ),
            exit_code=completion.exit_code,
            last_error=detail,
        )
        return True

    if completion.exit_code == 0:
        summary = _completion_reply_summary(completion.output)
        if task.status == "returning" or summary:
            _transition_task(
                state,
                task.id,
                status="completed",
                progress=(
                    task.progress
                    if task.status == "returning"
                    else f"Bot runner 回覆：{summary}"
                ),
                evidence="Hermes completion notification: exit 0 with reply",
                exit_code=0,
            )
        else:
            _transition_task(
                state,
                task.id,
                status="unconfirmed",
                progress=(
                    "背景 runner 回報 exit 0，但沒有 Bot final hook 或可辨識回覆；"
                    "bridge 無法宣稱任務完成。"
                ),
                evidence="Hermes completion notification: exit 0 without reply",
                exit_code=0,
            )
        return True

    _transition_task(
        state,
        task.id,
        status="unconfirmed",
        progress=(
            "Hermes 已送來背景 runner 完成通知，但沒有 exit code；"
            "尚無足夠證據判定 Bot 成功或失敗。"
        ),
        evidence="Hermes completion notification: exit code unavailable",
    )
    return True

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


def _tool_activity(tool_name: str) -> tuple[str, str] | None:
    if tool_name.startswith("browser_"):
        return "Bot 正在操作瀏覽器；bridge 只確認到 browser tool 呼叫，不推測畫面結果。", "hook:browser"
    if tool_name == "computer_use":
        return "Bot 正在操作電腦介面；bridge 已觀察到 computer_use 呼叫。", "hook:computer_use"
    if tool_name in {"terminal", "process_manage"}:
        return "Bot 正在執行或檢查本機程序。", "hook:terminal"
    if tool_name in {"write_file", "patch"}:
        return "Bot 正在修改檔案。", "hook:file-write"
    if tool_name in {"web_search", "web_extract"}:
        return "Bot 正在查詢外部資料。", "hook:web"
    return None


def _before_tool(
    tool_name: str,
    args: dict[str, Any],
    session_id: str = "",
    tool_call_id: str = "",
    turn_id: str = "",
    **_: Any,
) -> dict[str, Any] | None:
    """建立派工 intent，或記錄已綁定 worker 的高階工具活動。"""

    try:
        state = _state()
        if tool_name == "agent_task_start":
            return prepare_start(
                state,
                args or {},
                origin_profile=_current_profile(),
                session_id=str(session_id or ""),
                turn_id=str(turn_id or ""),
                tool_call_id=str(tool_call_id or ""),
            )
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
            # Bot 的明確、使用者可見里程碑比後續泛用 tool category 更有資訊。
            _transition_task(
                state,
                status="running",
                task_id=task.id,
                progress=activity[0],
                evidence=activity[1],
            )
        return None
    except Exception:
        logger.warning("Agent Task Bridge pre_tool_call failed open", exc_info=True)
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
        if tool_name in BRIDGE_TOOL_NAMES:
            return
        task = state.task_for_worker(session_id, turn_id)
        if (
            task is not None
            and not task.terminal
            and status in {"error", "blocked"}
            and not state.latest_explicit_progress(task.id)
        ):
            _transition_task(
                state,
                task.id,
                status="running",
                progress=f"Bot 的 {tool_name} 呼叫未成功；Bot 仍可調整後繼續。",
                evidence=f"hook:{status}",
            )
    except Exception:
        logger.warning("Agent Task Bridge post_tool_call observer failed", exc_info=True)


def _before_llm(
    session_id: str,
    user_message: str,
    turn_id: str = "",
    **_: Any,
) -> dict[str, str] | None:
    try:
        state = _state()
        # 背景 terminal 的完成通知會以合成 user turn 回到派工者。先依 process
        # ID 收斂狀態，且不要把 output 內的 task marker 誤綁成 Controller 自己
        # 的 worker turn。
        if _observe_process_completion(state, user_message):
            return None
        task_id = extract_task_id(user_message)
        if not task_id:
            context = system_prompt_section({"profile_name": _current_profile()})
            return {"context": context} if context else None
        task = state.task(task_id)
        if task is None:
            return None
        worker_profile = _current_profile()
        if task.target != worker_profile:
            # marker 不是授權；只有任務指定的 profile 可以接手這個 worker turn。
            return None
        bound = state.bind_worker(
            task_id,
            worker_profile=worker_profile,
            worker_session_id=str(session_id or ""),
            worker_turn_id=str(turn_id or ""),
        )
        if bound is None:
            return None
        kick_native_outbox(state)
        return {
            "context": (
                f"這是 Agent Task Bridge 追蹤任務 {task_id}。"
                "只在有可驗證的新進展時呼叫 bridge_task_update；不要回報內部思考。"
                "開始時、長時間操作前後、以及送出最終答覆前，呼叫 "
                f"bridge_task_inbox(task_id=\"{task_id}\") 檢查使用者留言。"
                "若要宣稱瀏覽器或其他介面已開啟，必須先有相應工具成功的證據。"
                "完成工作後，先用 bridge_task_update(status=\"result\") 回報一則簡潔、"
                "可公開的最終里程碑，"
                "再照常回覆 Controller；bridge 會由 post_llm_call 與背景程序狀態判定完成，"
                "不需自行宣稱 completed。若漏掉明確里程碑，bridge 只會把清理後的最終答覆"
                "摘要留在原生訊息平台的任務時間線，不會保存內部思考或原始工具資料。"
            )
        }
    except Exception:
        logger.warning("Agent Task Bridge pre_llm_call failed open", exc_info=True)
        return None


def _after_llm(
    session_id: str,
    turn_id: str = "",
    assistant_response: str = "",
    **_: Any,
) -> None:
    try:
        state = _state()
        state.complete_worker_turn(
            session_id,
            turn_id,
            assistant_response=assistant_response,
        )
        kick_native_outbox(state)
    except Exception:
        logger.warning("Agent Task Bridge post_llm_call observer failed", exc_info=True)


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
            _transition_task(
                state,
                task.id,
                status="failed",
                progress="Bot turn 在產生最終回覆前失敗。",
                evidence="hook:on_session_end failed",
                last_error=reason or "worker turn failed",
            )
        else:
            _transition_task(
                state,
                task.id,
                status="blocked",
                progress="Bot turn 被中斷；任務可能需要重新派送。",
                evidence="hook:on_session_end interrupted",
                last_error=reason,
            )
    except Exception:
        logger.warning("Agent Task Bridge session-end observer failed", exc_info=True)


def _authorized_worker_task(
    state: BridgeState, task_id: str, session_id: str
) -> tuple[Any, str | None]:
    task = state.task(task_id)
    if task is None:
        return None, "找不到 task。"
    bound = state.task_for_worker(str(session_id or ""))
    if bound is None or bound.id != task.id:
        return None, "這個 Hermes session 不是該 task 的已綁定 Bot turn。"
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
    updated = _transition_task(
        state,
        task.id,
        status=mapped,
        progress=message,
        evidence=(
            "Bot explicit bridge_task_result"
            if requested == "result"
            else "Bot explicit bridge_task_update"
        ),
    )
    return _json({
        "ok": updated is not None,
        "task_id": task.id,
        "status": updated.status if updated else task.status,
        "message": "任務進度已排入原生訊息平台時間線。",
    })


def bridge_task_inbox(args: dict[str, Any], **kwargs: Any) -> str:
    state = _state()
    task_id = str(args.get("task_id") or "")
    task, error = _authorized_worker_task(state, task_id, str(kwargs.get("session_id") or ""))
    if error:
        return _json({"ok": False, "error": error})
    acknowledge = bool(args.get("acknowledge", True))
    updated, notes = state.read_task_notes(task.id, mark_read=acknowledge)
    kick_native_outbox(state)
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
            "worker_started": task.worker_started,
            "final_observed": task.final_observed,
            "worker_profile": task.worker_profile,
            "pending_notes": task.pending_notes,
            "updated_at": task.updated_at,
        }
        for task in tasks if task is not None
    ]
    return _json({"ok": bool(rows), "tasks": rows})


def register_task_features(ctx: Any) -> None:
    """註冊 worker 狀態工具與任務生命週期 hooks。"""

    ctx.register_tool(
        name="bridge_task_update",
        toolset=TOOLSET_NAME,
        schema={
            "name": "bridge_task_update",
            "description": (
                "Update a tracked Agent Task only when a meaningful, "
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
        description="回報可驗證的 Bot 任務進度",
        emoji="🧭",
    )
    ctx.register_tool(
        name="bridge_task_inbox",
        toolset=TOOLSET_NAME,
        schema={
            "name": "bridge_task_inbox",
            "description": (
                "Read follow-up messages attached by the controller to this tracked task. "
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
        description="讀取主 Agent 的任務留言",
        emoji="📥",
    )
    ctx.register_tool(
        name="bridge_task_status",
        toolset=TOOLSET_NAME,
        schema={
            "name": "bridge_task_status",
            "description": "Read evidence-backed status for one or recent Agent Task Bridge tasks.",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": [],
            },
        },
        handler=bridge_task_status,
        description="查詢 Agent Task Bridge ledger",
        emoji="📋",
    )
    ctx.register_hook("pre_tool_call", _before_tool)
    ctx.register_hook("post_tool_call", _after_tool)
    ctx.register_hook("pre_llm_call", _before_llm)
    ctx.register_hook("post_llm_call", _after_llm)
    ctx.register_hook("on_session_end", _on_session_end)
