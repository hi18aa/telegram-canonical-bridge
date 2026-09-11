"""背景 runner 與 Controller completion hook 共用的任務收斂規則。"""

from __future__ import annotations

import json
import re
from typing import Any

from .task_model import sanitize_progress


_REASON_RE = re.compile(r"\[reason:\s*([a-z0-9_-]+)\]", re.IGNORECASE)

_FAILURE_MESSAGES = {
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


def completion_reason(output: object) -> str:
    """從受控 runner output 擷取類型化原因；不把原文當成公開錯誤。"""

    text = str(output or "")
    match = _REASON_RE.search(text)
    if match:
        return match.group(1).lower()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict) and payload.get("reason"):
        return str(payload["reason"]).strip().lower()
    for candidate in reversed([line.strip() for line in text.splitlines() if line.strip()]):
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("reason"):
            return str(payload["reason"]).strip().lower()
    if "already has a live owner" in text.lower():
        return "target_busy"
    return ""


def completion_reply_summary(output: object) -> str:
    """只擷取 ``hermes chat -Q`` 的可公開 final reply。"""

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


def settle_task_completion(
    state: Any,
    task_id: str,
    *,
    exit_code: int | None,
    output: object = "",
    reason: str = "",
    origin: str,
):
    """以相同規則收斂 runner 直寫與來源 session 收到的完成通知。"""

    task = state.task(task_id)
    if task is None or task.terminal:
        return task
    clean_reason = str(reason or completion_reason(output) or "unknown").strip().lower()

    if task.status == "stopping" and clean_reason == "cancelled" and exit_code is not None:
        return state.transition_task(
            task.id,
            status="cancelled",
            progress="原 runner 已確認工作子程序停止；已發生的外部副作用不會回滾。",
            evidence=f"{origin}: owned worker stopped",
            exit_code=exit_code,
            last_error="",
        )
    if task.status == "stopping" and exit_code is None:
        return task

    if exit_code is not None and exit_code != 0:
        # Worker final hook 已出現時，後續 CLI cleanup 非零不能抹掉既有結果。
        if task.status == "returning":
            return state.transition_task(
                task.id,
                status="completed",
                evidence=f"hook:post_llm_call + {origin} (exit {exit_code})",
                exit_code=exit_code,
            )
        progress, detail = _FAILURE_MESSAGES.get(
            clean_reason, _FAILURE_MESSAGES["unknown"]
        )
        return state.transition_task(
            task.id,
            status="failed",
            progress=progress,
            evidence=f"{origin}: exit {exit_code}; reason={clean_reason}",
            exit_code=exit_code,
            last_error=detail,
        )

    if exit_code == 0:
        summary = completion_reply_summary(output)
        if task.status == "returning" or summary:
            return state.transition_task(
                task.id,
                status="completed",
                progress=(
                    task.progress
                    if task.status == "returning"
                    else f"Bot runner 回覆：{summary}"
                ),
                evidence=f"{origin}: exit 0 with reply",
                exit_code=0,
            )
        return state.transition_task(
            task.id,
            status="unconfirmed",
            progress=(
                "背景 runner 回報 exit 0，但沒有 Bot final hook 或可辨識回覆；"
                "bridge 無法宣稱任務完成。"
            ),
            evidence=f"{origin}: exit 0 without reply",
            exit_code=0,
        )

    return state.transition_task(
        task.id,
        status="unconfirmed",
        progress=(
            "Hermes 已送來背景 runner 完成通知，但沒有 exit code；"
            "尚無足夠證據判定 Bot 成功或失敗。"
        ),
        evidence=f"{origin}: exit code unavailable",
    )
