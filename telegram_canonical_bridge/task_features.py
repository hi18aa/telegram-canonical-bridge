"""Hermes hooks 與 OT 可呼叫的任務狀態工具。

V4 不覆寫 message_agent。它只在原生呼叫前加入 opaque task marker，並以
Hermes 公開 hook／tool API 把可驗證的狀態寫入共用 SQLite ledger。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
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
_LEGACY_COMPLETION_RE = re.compile(
    r"^\s*\[Background process\s+(?P<process_id>[A-Za-z0-9_.:-]+)\s+"
    r"finished with exit code\s+(?P<exit_code>-?\d+|\?)",
    re.IGNORECASE | re.DOTALL,
)
_REASON_RE = re.compile(r"\[reason:\s*([a-z0-9_-]+)\]", re.IGNORECASE)


def _parse_process_completion(value: object) -> _ProcessCompletion | None:
    """讀取 Hermes 公開的背景完成通知；格式不符時完全忽略。

    同時接受目前的 ``[IMPORTANT: ... Output:]`` 與舊版 gateway 的
    ``[Background process ... Here's the final output:]`` 形狀。解析器只用
    opaque process ID 做 ledger 關聯，不讀 Command 欄位。
    """

    text = str(value or "")
    match = _CURRENT_COMPLETION_RE.match(text) or _LEGACY_COMPLETION_RE.match(text)
    if match is None:
        return None
    raw_exit = match.group("exit_code")
    exit_code = int(raw_exit) if raw_exit != "?" else None
    output = ""
    for delimiter in ("\nOutput:\n", "Here's the final output:\n"):
        position = text.find(delimiter, match.end())
        if position >= 0:
            output = text[position + len(delimiter):].rstrip()
            if output.endswith("]"):
                output = output[:-1].rstrip()
            break
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


def _is_live_delivery_ack(output: str) -> bool:
    lowered = output.lower()
    if "open bot chat" in lowered and "reply will appear there" in lowered:
        return True
    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and str(payload.get("status") or "").lower() == "queued"


def _completion_reply_summary(output: str) -> str:
    """擷取 runner stdout 中預期可回給原提問者的 agent 答覆。

    非零 exit 的任意輸出不會走到此函式；錯誤只以類型化原因呈現，避免把
    command、stack trace 或意外秘密複製到 Telegram。
    """

    text = str(output or "").strip()
    if not text or text == "(empty reply)" or _is_live_delivery_ack(text):
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
            "目標 Bot Chat 被其他 surface 持有，背景 runner 未能啟動 OT turn。",
            "請升級並重啟目標 Hermes backend；不要把 sent acknowledgement 當成已交付。",
        ),
        "runtime_offline": (
            "目標 Hermes runtime 離線，背景派工未完成。",
            "目標 runtime 離線。",
        ),
        "delivery_timeout": (
            "等待目標回覆逾時，bridge 無法確認 OT 是否完成。",
            "delivery timeout；不要盲目重派。",
        ),
        "queued_expired": (
            "Hermes 的排隊派工已過期，OT turn 未完成。",
            "queued delivery expired。",
        ),
        "provider_auth_or_access": (
            "OT provider 驗證或存取失敗，沒有完成回覆。",
            "provider authentication/access failure。",
        ),
        "provider_quota_limit": (
            "OT provider 額度不足，沒有完成回覆。",
            "provider quota limit。",
        ),
        "provider_rate_limit": (
            "OT provider 暫時限流，沒有完成回覆。",
            "provider rate limit。",
        ),
        "provider_server_error": (
            "OT provider 發生伺服器錯誤，沒有完成回覆。",
            "provider server error。",
        ),
        "missing_config": (
            "OT 缺少執行所需設定，沒有開始或完成 turn。",
            "target profile configuration is incomplete。",
        ),
        "model_unavailable": (
            "OT 指定模型不可用，沒有完成回覆。",
            "target model unavailable。",
        ),
        "context_overflow": (
            "OT 對話 context overflow，重試後仍未完成。",
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
            state.transition_task(
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
        state.transition_task(
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

    if completion.exit_code == 0 and _is_live_delivery_ack(completion.output):
        state.transition_task(
            task.id,
            status="waiting",
            progress=(
                "Hermes 已把訊息排入目標的 live Bot Chat；這只是持久化收件回條，"
                "尚未證明 OT turn 已啟動或完成。"
            ),
            evidence="Hermes completion notification: live delivery queued",
            exit_code=0,
        )
        return True

    if completion.exit_code == 0:
        summary = _completion_reply_summary(completion.output)
        if task.status == "returning" or summary:
            state.transition_task(
                task.id,
                status="completed",
                progress=(
                    task.progress
                    if task.status == "returning"
                    else f"OT runner 回覆：{summary}"
                ),
                evidence="Hermes completion notification: exit 0 with reply",
                exit_code=0,
            )
        else:
            state.transition_task(
                task.id,
                status="waiting",
                progress=(
                    "背景 runner 回報 exit 0，但沒有 OT final hook 或可辨識回覆；"
                    "bridge 暫不宣稱任務完成。"
                ),
                evidence="Hermes completion notification: exit 0 without reply",
                exit_code=0,
            )
        return True

    state.transition_task(
        task.id,
        status="waiting",
        progress=(
            "Hermes 已送來背景 runner 完成通知，但沒有 exit code；"
            "尚無足夠證據判定 OT 成功或失敗。"
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
            delivery_status = str(payload.get("status") or "").strip().lower()
            if delivery_status in {"sent", "queued"}:
                if process_id:
                    state.acknowledge_dispatch(
                        session_id=session_id,
                        tool_call_id=tool_call_id,
                        process_id=process_id,
                        delivery_status=delivery_status,
                    )
                else:
                    state.acknowledge_without_process(
                        session_id=session_id,
                        tool_call_id=tool_call_id,
                        delivery_status=delivery_status,
                    )
                return
            error = str(
                payload.get("error") or error_message or
                "message_agent 未回傳可辨識的 sent／queued acknowledgement"
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
    try:
        state = _state()
        # message_agent 的 notify_on_complete 會以合成 user turn 回到派工者。
        # 先依 process ID 收斂狀態，且不要把通知 output 中可能出現的 task
        # marker 誤綁成 Controller 自己的 worker turn。
        if _observe_process_completion(state, user_message):
            return None
        task_id = extract_task_id(user_message)
        if not task_id:
            return None
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
