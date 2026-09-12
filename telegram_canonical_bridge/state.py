"""Agent Task Bridge 的精簡 SQLite ledger。

只有 task、event、note 與 outbound event 四種資料；沒有 Telegram inbound、
canonical history、platform route 或 Desktop RPC 狀態。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .task_model import (
    FINAL_TASK_STATUSES,
    TASK_STATUS_LABELS,
    TERMINAL_TASK_STATUSES,
    OutboxRecord,
    TaskNote,
    TaskRecord,
    new_task_id,
    normalize_task_id,
    render_task_event,
    sanitize_progress,
)


AUTOMATIC_TASK_EVENT_MIN_INTERVAL_SECONDS = 12.0
EXPLICIT_EVIDENCE = frozenset({
    "Bot explicit bridge_task_update",
    "Bot explicit bridge_task_result",
})


class BridgeState:
    """以短生命週期連線提供跨 profiles 的 durable task ledger。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._reconcile_closed_task_notes()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _initialize(self) -> None:
        with self._read_connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    delivery_target TEXT NOT NULL,
                    origin_profile TEXT NOT NULL,
                    origin_session_id TEXT NOT NULL,
                    origin_turn_id TEXT NOT NULL,
                    origin_tool_call_id TEXT NOT NULL,
                    parent_task_id TEXT,
                    target TEXT NOT NULL,
                    process_id TEXT,
                    worker_profile TEXT,
                    worker_session_id TEXT,
                    worker_turn_id TEXT,
                    status TEXT NOT NULL,
                    progress TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    exit_code INTEGER,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    finished_at REAL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY(parent_task_id) REFERENCES tasks(task_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS tasks_origin_call
                    ON tasks(origin_session_id, origin_tool_call_id)
                    WHERE origin_tool_call_id <> '';
                CREATE INDEX IF NOT EXISTS tasks_process ON tasks(process_id);
                CREATE INDEX IF NOT EXISTS tasks_worker
                    ON tasks(worker_session_id, worker_turn_id);
                CREATE INDEX IF NOT EXISTS tasks_updated
                    ON tasks(origin_profile, updated_at DESC);

                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS task_events_task
                    ON task_events(task_id, id);

                CREATE TABLE IF NOT EXISTS task_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    note_key TEXT NOT NULL UNIQUE,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    read_at REAL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS task_notes_unread
                    ON task_notes(task_id, status, id);

                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    delivery_target TEXT NOT NULL,
                    dedup_key TEXT NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    lease_until REAL,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    sent_at REAL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS outbox_due
                    ON outbox(status, next_attempt_at, lease_until);
                CREATE INDEX IF NOT EXISTS outbox_task_order
                    ON outbox(task_id, id);
                """
            )

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _task_select() -> str:
        return (
            "SELECT t.*, (SELECT COUNT(*) FROM task_notes n "
            "WHERE n.task_id = t.task_id AND n.status = 'unread') AS pending_notes "
            "FROM tasks t "
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskRecord:
        exit_code = row["exit_code"]
        finished_at = row["finished_at"]
        return TaskRecord(
            id=str(row["task_id"]),
            delivery_target=str(row["delivery_target"]),
            origin_profile=str(row["origin_profile"]),
            origin_session_id=str(row["origin_session_id"]),
            origin_turn_id=str(row["origin_turn_id"]),
            origin_tool_call_id=str(row["origin_tool_call_id"]),
            parent_task_id=str(row["parent_task_id"]) if row["parent_task_id"] else None,
            target=str(row["target"]),
            process_id=str(row["process_id"]) if row["process_id"] else None,
            worker_profile=str(row["worker_profile"]) if row["worker_profile"] else None,
            worker_session_id=str(row["worker_session_id"]) if row["worker_session_id"] else None,
            worker_turn_id=str(row["worker_turn_id"]) if row["worker_turn_id"] else None,
            status=str(row["status"]),
            progress=str(row["progress"]),
            evidence=str(row["evidence"]),
            exit_code=int(exit_code) if exit_code is not None else None,
            last_error=str(row["last_error"] or ""),
            pending_notes=int(row["pending_notes"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            finished_at=float(finished_at) if finished_at is not None else None,
            revision=int(row["revision"]),
        )

    def _task_locked(self, connection: sqlite3.Connection, task_id: str) -> TaskRecord | None:
        normalized = normalize_task_id(task_id)
        if not normalized:
            return None
        row = connection.execute(
            self._task_select() + "WHERE t.task_id = ?", (normalized,)
        ).fetchone()
        return self._task_from_row(row) if row else None

    def task(self, task_id: str) -> TaskRecord | None:
        with self._read_connection() as connection:
            return self._task_locked(connection, task_id)

    def create_task(
        self,
        *,
        delivery_target: str,
        origin_profile: str,
        origin_session_id: str,
        origin_turn_id: str,
        origin_tool_call_id: str,
        target: str,
        parent_task_id: str | None = None,
        initial_progress: str = "主 Agent 正在建立專門 Bot 派工程序。",
        initial_evidence: str = "hook:agent_task_start intent",
        queue_outbox: bool = False,
    ) -> tuple[TaskRecord, bool]:
        """建立派工 intent；相同 session/tool call 重入時回傳既有任務。"""

        clean_target = sanitize_progress(str(target or "").lstrip("@"), limit=128) or "unknown"
        clean_delivery = sanitize_progress(delivery_target, limit=280) or "local"
        now = time.time()
        with self._transaction() as connection:
            if origin_session_id and origin_tool_call_id:
                existing = connection.execute(
                    self._task_select()
                    + "WHERE t.origin_session_id = ? AND t.origin_tool_call_id = ? LIMIT 1",
                    (origin_session_id, origin_tool_call_id),
                ).fetchone()
                if existing is not None:
                    return self._task_from_row(existing), False

            normalized_parent = normalize_task_id(parent_task_id)
            if normalized_parent and connection.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (normalized_parent,)
            ).fetchone() is None:
                normalized_parent = ""

            for _attempt in range(8):
                task_id = new_task_id()
                try:
                    connection.execute(
                        "INSERT INTO tasks("
                        "task_id, delivery_target, origin_profile, origin_session_id, "
                        "origin_turn_id, origin_tool_call_id, parent_task_id, target, status, "
                        "progress, evidence, created_at, updated_at, revision) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'dispatching', ?, ?, ?, ?, 1)",
                        (
                            task_id,
                            clean_delivery,
                            origin_profile or "default",
                            origin_session_id,
                            origin_turn_id,
                            origin_tool_call_id,
                            normalized_parent or None,
                            clean_target,
                            sanitize_progress(initial_progress),
                            sanitize_progress(initial_evidence, limit=180),
                            now,
                            now,
                        ),
                    )
                    break
                except sqlite3.IntegrityError:
                    if origin_session_id and origin_tool_call_id:
                        existing = connection.execute(
                            self._task_select()
                            + "WHERE t.origin_session_id = ? AND t.origin_tool_call_id = ? LIMIT 1",
                            (origin_session_id, origin_tool_call_id),
                        ).fetchone()
                        if existing is not None:
                            return self._task_from_row(existing), False
            else:  # pragma: no cover - 隨機 ID 碰撞實務上不可達
                raise RuntimeError("無法配置 bridge task ID。")

            task = self._task_locked(connection, task_id)
            assert task is not None
            self._record_task_event_locked(connection, task)
            if queue_outbox:
                self._queue_task_event_locked(connection, task)
            return task, True

    @staticmethod
    def _record_task_event_locked(connection: sqlite3.Connection, task: TaskRecord) -> None:
        connection.execute(
            "INSERT INTO task_events(task_id, status, progress, evidence, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task.id, task.status, task.progress, task.evidence, task.updated_at),
        )

    @staticmethod
    def _queue_task_event_locked(connection: sqlite3.Connection, task: TaskRecord) -> None:
        recent = connection.execute(
            "SELECT status, evidence FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 2",
            (task.id,),
        ).fetchall()
        previous_status = str(recent[1]["status"]) if len(recent) > 1 else ""
        status_changed = not previous_status or previous_status != task.status
        explicit = task.evidence in EXPLICIT_EVIDENCE
        now = time.time()
        if not status_changed and not explicit:
            latest = connection.execute(
                "SELECT created_at FROM outbox WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task.id,),
            ).fetchone()
            if latest is not None and now - float(latest["created_at"]) < AUTOMATIC_TASK_EVENT_MIN_INTERVAL_SECONDS:
                return
        connection.execute(
            "INSERT OR IGNORE INTO outbox("
            "task_id, delivery_target, dedup_key, content, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (
                task.id,
                task.delivery_target,
                f"task-event:{task.id}:revision:{task.revision}",
                render_task_event(task),
                now,
            ),
        )

    @staticmethod
    def _mark_unread_notes_missed_locked(
        connection: sqlite3.Connection, task_id: str, *, now: float
    ) -> int:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM task_notes WHERE task_id = ? AND status = 'unread'",
            (task_id,),
        ).fetchone()
        count = int(row["n"]) if row else 0
        if count:
            connection.execute(
                "UPDATE task_notes SET status = 'missed', read_at = ? "
                "WHERE task_id = ? AND status = 'unread'",
                (now, task_id),
            )
        return count

    @staticmethod
    def _queue_missed_notes_notice_locked(
        connection: sqlite3.Connection, task: TaskRecord, count: int
    ) -> None:
        if count <= 0:
            return
        closure = (
            "已產生最終回覆"
            if task.status in {"returning", "completed"}
            else f"已進入「{TASK_STATUS_LABELS.get(task.status, task.status)}」"
        )
        connection.execute(
            "INSERT OR IGNORE INTO outbox("
            "task_id, delivery_target, dedup_key, content, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (
                task.id,
                task.delivery_target,
                f"task-notes-missed:{task.id}",
                (
                    f"⚠️ 任務 {task.id} {closure}；Bot 未及讀取 {count} 則任務留言，"
                    "內容未納入本次結果。請把需求另傳成主 Agent 的新訊息。"
                ),
                time.time(),
            ),
        )

    def _reconcile_closed_task_notes(self) -> int:
        # 可接續的 interrupted／unconfirmed 必須保留未讀指示，下一個同 task
        # continuation 才能取得；只有真正結案或 final 已產生才算 missed。
        closed = ("returning", *sorted(FINAL_TASK_STATUSES))
        placeholders = ",".join("?" for _ in closed)
        reconciled = 0
        with self._transaction() as connection:
            rows = connection.execute(
                self._task_select()
                + f"WHERE t.status IN ({placeholders}) AND EXISTS ("
                "SELECT 1 FROM task_notes n WHERE n.task_id = t.task_id AND n.status = 'unread')",
                closed,
            ).fetchall()
            for row in rows:
                task = self._task_from_row(row)
                if task.resumable:
                    continue
                now = time.time()
                missed = self._mark_unread_notes_missed_locked(connection, task.id, now=now)
                if not missed:
                    continue
                connection.execute(
                    "UPDATE tasks SET updated_at = ?, revision = revision + 1 WHERE task_id = ?",
                    (now, task.id),
                )
                updated = self._task_locked(connection, task.id)
                assert updated is not None
                self._record_task_event_locked(connection, updated)
                self._queue_task_event_locked(connection, updated)
                self._queue_missed_notes_notice_locked(connection, updated, missed)
                reconciled += missed
        return reconciled

    def transition_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        progress: str | None = None,
        evidence: str | None = None,
        process_id: str | None = None,
        worker_profile: str | None = None,
        worker_session_id: str | None = None,
        worker_turn_id: str | None = None,
        exit_code: int | None = None,
        last_error: str | None = None,
    ) -> TaskRecord | None:
        """原子更新任務；已確認終態不會被較晚 observer 降級。"""

        normalized = normalize_task_id(task_id)
        if not normalized:
            return None
        if status is not None and status not in TASK_STATUS_LABELS:
            raise ValueError(f"不支援的 task status：{status}")
        with self._transaction() as connection:
            current = self._task_locked(connection, normalized)
            if current is None:
                return None
            recovering = (
                current.resumable
                and status is not None
                and status != current.status
            )
            if current.terminal and not recovering:
                return current
            if current.status == "stopping" and status not in TERMINAL_TASK_STATUSES:
                # 取消是 Controller 的 durable 意圖；較晚的 worker 進度／final hook 不得蓋掉它。
                return current

            changes: dict[str, Any] = {}
            if status is not None and status != current.status:
                changes["status"] = status
            if progress is not None:
                clean = sanitize_progress(progress)
                if clean and clean != current.progress:
                    changes["progress"] = clean
            if evidence is not None:
                clean = sanitize_progress(evidence, limit=180)
                if clean and clean != current.evidence:
                    changes["evidence"] = clean
            for field, value, previous in (
                ("process_id", process_id, current.process_id),
                ("worker_profile", worker_profile, current.worker_profile),
                ("worker_session_id", worker_session_id, current.worker_session_id),
                ("worker_turn_id", worker_turn_id, current.worker_turn_id),
            ):
                clean = sanitize_progress(value, limit=200) if value is not None else None
                if clean and clean != previous:
                    changes[field] = clean
            if exit_code is not None and int(exit_code) != current.exit_code:
                changes["exit_code"] = int(exit_code)
            if last_error is not None:
                clean_error = sanitize_progress(last_error, limit=1000)
                if clean_error != current.last_error:
                    changes["last_error"] = clean_error
            if not changes:
                return current

            now = time.time()
            changes["updated_at"] = now
            changes["revision"] = current.revision + 1
            next_status = str(changes.get("status", current.status))
            if next_status in TERMINAL_TASK_STATUSES:
                changes["finished_at"] = now
            elif current.resumable:
                changes["finished_at"] = None
            assignments = ", ".join(f"{field} = ?" for field in changes)
            connection.execute(
                f"UPDATE tasks SET {assignments} WHERE task_id = ?",
                (*changes.values(), normalized),
            )
            updated = self._task_locked(connection, normalized)
            assert updated is not None
            missed = 0
            closes_notes = next_status == "returning" or updated.final
            if closes_notes:
                missed = self._mark_unread_notes_missed_locked(connection, normalized, now=now)
                updated = self._task_locked(connection, normalized)
                assert updated is not None
            self._record_task_event_locked(connection, updated)
            self._queue_task_event_locked(connection, updated)
            self._queue_missed_notes_notice_locked(connection, updated, missed)
            return updated

    def bind_worker(
        self,
        task_id: str,
        *,
        worker_profile: str,
        worker_session_id: str,
        worker_turn_id: str,
    ) -> TaskRecord | None:
        return self.transition_task(
            task_id,
            status="running",
            progress=f"@{worker_profile} 已開始處理這個 turn。",
            evidence="hook:pre_llm_call",
            worker_profile=worker_profile,
            worker_session_id=worker_session_id,
            worker_turn_id=worker_turn_id,
        )

    def claim_task_continuation(
        self,
        *,
        task_id: str,
        origin_profile: str,
    ) -> tuple[TaskRecord | None, bool, str]:
        """以 CAS 保留一次同 task continuation，不重送原始派工內容。

        claim 會清除上一個 runner 的即時 identity，避免其較晚 completion
        notification 誤套到新 runner；歷史狀態仍保留在 task_events。
        """

        normalized = normalize_task_id(task_id)
        if not normalized:
            return None, False, "task_id 無效。"
        with self._transaction() as connection:
            task = self._task_locked(connection, normalized)
            if task is None:
                return None, False, "找不到這個任務。"
            if task.origin_profile != str(origin_profile or "default"):
                return task, False, "只有建立任務的主 Agent profile 可以接續。"
            if not task.resumable:
                return task, False, "任務目前不是可接續狀態。"

            now = time.time()
            cursor = connection.execute(
                "UPDATE tasks SET status = 'continuing', process_id = NULL, "
                "worker_profile = NULL, worker_session_id = NULL, worker_turn_id = NULL, "
                "exit_code = NULL, last_error = '', finished_at = NULL, progress = ?, evidence = ?, "
                "updated_at = ?, revision = revision + 1 "
                "WHERE task_id = ? AND (status IN ('interrupted', 'unconfirmed') OR "
                "(status = 'failed' AND worker_session_id IS NOT NULL))",
                (
                    "主 Agent 已提供明確接續指示；正在建立同一 task 的 continuation runner。",
                    sanitize_progress(
                        "agent_task_message continuation intent; "
                        f"previous_process={task.process_id or 'none'}; "
                        f"previous_session={task.worker_session_id or 'none'}",
                        limit=180,
                    ),
                    now,
                    normalized,
                ),
            )
            if cursor.rowcount != 1:
                current = self._task_locked(connection, normalized)
                return current, False, "另一個 caller 已接管同一 task 的 continuation。"
            updated = self._task_locked(connection, normalized)
            assert updated is not None
            self._record_task_event_locked(connection, updated)
            self._queue_task_event_locked(connection, updated)
            return updated, True, "已保留同一 task 的 continuation runner。"

    def task_for_worker(self, session_id: str, turn_id: str = "") -> TaskRecord | None:
        if not session_id:
            return None
        with self._read_connection() as connection:
            params: list[Any] = [session_id]
            where = "WHERE t.worker_session_id = ?"
            if turn_id:
                where += " AND (t.worker_turn_id = ? OR t.worker_turn_id = '')"
                params.append(turn_id)
            row = connection.execute(
                self._task_select() + where + " ORDER BY t.updated_at DESC LIMIT 1",
                tuple(params),
            ).fetchone()
        return self._task_from_row(row) if row else None

    def task_by_process_id(self, process_id: str) -> TaskRecord | None:
        candidate = str(process_id or "").strip()
        if not candidate:
            return None
        with self._read_connection() as connection:
            row = connection.execute(
                self._task_select()
                + "WHERE t.process_id = ? ORDER BY t.created_at DESC LIMIT 1",
                (candidate,),
            ).fetchone()
        return self._task_from_row(row) if row else None

    def latest_explicit_progress(self, task_id: str) -> str:
        normalized = normalize_task_id(task_id)
        if not normalized:
            return ""
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT progress FROM task_events WHERE task_id = ? "
                "AND evidence IN ('Bot explicit bridge_task_update', "
                "'Bot explicit bridge_task_result') "
                "AND id > COALESCE((SELECT MAX(id) FROM task_events "
                "WHERE task_id = ? AND evidence LIKE "
                "'agent_task_message continuation intent%'), 0) "
                "ORDER BY id DESC LIMIT 1",
                (normalized, normalized),
            ).fetchone()
        return str(row["progress"]) if row else ""

    def latest_explicit_result(self, task_id: str) -> str:
        normalized = normalize_task_id(task_id)
        if not normalized:
            return ""
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT progress FROM task_events WHERE task_id = ? "
                "AND evidence = 'Bot explicit bridge_task_result' "
                "AND id > COALESCE((SELECT MAX(id) FROM task_events "
                "WHERE task_id = ? AND evidence LIKE "
                "'agent_task_message continuation intent%'), 0) "
                "ORDER BY id DESC LIMIT 1",
                (normalized, normalized),
            ).fetchone()
        return str(row["progress"]) if row else ""

    def complete_worker_turn(
        self,
        session_id: str,
        turn_id: str = "",
        *,
        assistant_response: str = "",
    ) -> TaskRecord | None:
        task = self.task_for_worker(session_id, turn_id)
        if task is None:
            return None
        explicit_result = self.latest_explicit_result(task.id)
        explicit_progress = self.latest_explicit_progress(task.id)
        final_summary = (
            sanitize_progress(assistant_response, limit=1000)
            if isinstance(assistant_response, str)
            else ""
        )
        return self.transition_task(
            task.id,
            status="returning",
            progress=(
                explicit_result
                or (f"Bot 最終回覆：{final_summary}" if final_summary else "")
                or explicit_progress
                or task.progress
                or "Bot 已產生最終回覆；等待背景程序結束。"
            ),
            evidence=(
                "hook:post_llm_call (sanitized final fallback)"
                if final_summary and not explicit_result
                else "hook:post_llm_call"
            ),
        )

    def list_tasks(
        self, *, origin_profile: str | None = None, limit: int = 10
    ) -> list[TaskRecord]:
        capped = max(1, min(int(limit), 50))
        with self._read_connection() as connection:
            if origin_profile:
                rows = connection.execute(
                    self._task_select()
                    + "WHERE t.origin_profile = ? ORDER BY t.created_at DESC LIMIT ?",
                    (origin_profile, capped),
                ).fetchall()
            else:
                rows = connection.execute(
                    self._task_select() + "ORDER BY t.created_at DESC LIMIT ?",
                    (capped,),
                ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def add_agent_task_note(
        self,
        *,
        task_id: str,
        origin_profile: str,
        text: str,
        note_id: str,
    ) -> tuple[TaskRecord | None, bool, str]:
        normalized = normalize_task_id(task_id)
        clean_text = str(text or "").strip()
        if not normalized or not clean_text:
            return None, False, "task_id 或留言內容無效。"
        with self._transaction() as connection:
            task = self._task_locked(connection, normalized)
            if task is None:
                return None, False, "找不到這個任務。"
            if task.origin_profile != str(origin_profile or "default"):
                return task, False, "只有建立任務的主 Agent profile 可以補充指示。"
            if (task.terminal and not task.resumable) or task.status == "returning":
                return task, False, "任務已結束或已產生最終回覆；請建立新任務。"
            note_key = f"agent:{note_id}"
            clean_note = clean_text[:4000]
            try:
                connection.execute(
                    "INSERT INTO task_notes(task_id, note_key, text, status, created_at) "
                    "VALUES (?, ?, ?, 'unread', ?)",
                    (normalized, note_key, clean_note, time.time()),
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    "SELECT text FROM task_notes WHERE note_key = ?",
                    (note_key,),
                ).fetchone()
                if existing is not None and str(existing["text"]) == clean_note:
                    return task, True, "這則留言已經收錄；本次未重複加入。"
                return task, False, "相同留言識別碼已有不同內容，拒絕覆寫。"
            now = time.time()
            connection.execute(
                "UPDATE tasks SET progress = ?, evidence = ?, updated_at = ?, "
                "revision = revision + 1 WHERE task_id = ?",
                ("使用者新增任務留言，等待 Bot 在檢查點讀取。", "agent_task_message", now, normalized),
            )
            updated = self._task_locked(connection, normalized)
            assert updated is not None
            self._record_task_event_locked(connection, updated)
            self._queue_task_event_locked(connection, updated)
            return updated, True, "留言已保存；Bot 會在下一個 task inbox 檢查點讀取。"

    def read_task_notes(
        self, task_id: str, *, mark_read: bool = True, limit: int = 20
    ) -> tuple[TaskRecord | None, list[TaskNote]]:
        normalized = normalize_task_id(task_id)
        if not normalized:
            return None, []
        with self._transaction() as connection:
            task = self._task_locked(connection, normalized)
            if task is None:
                return None, []
            rows = connection.execute(
                "SELECT id, task_id, text, created_at FROM task_notes "
                "WHERE task_id = ? AND status = 'unread' ORDER BY id LIMIT ?",
                (normalized, max(1, min(int(limit), 50))),
            ).fetchall()
            notes = [
                TaskNote(
                    id=int(row["id"]),
                    task_id=str(row["task_id"]),
                    text=str(row["text"]),
                    created_at=float(row["created_at"]),
                )
                for row in rows
            ]
            if mark_read and notes:
                placeholders = ",".join("?" for _ in notes)
                now = time.time()
                connection.execute(
                    f"UPDATE task_notes SET status = 'read', read_at = ? "
                    f"WHERE id IN ({placeholders})",
                    (now, *(note.id for note in notes)),
                )
                connection.execute(
                    "UPDATE tasks SET progress = ?, evidence = ?, updated_at = ?, "
                    "revision = revision + 1 WHERE task_id = ?",
                    ("Bot 已讀取最新任務留言並繼續處理。", "bridge_task_inbox", now, normalized),
                )
                task = self._task_locked(connection, normalized)
                assert task is not None
                self._record_task_event_locked(connection, task)
                self._queue_task_event_locked(connection, task)
            return task, notes

    def claim_due_outbox(
        self, *, limit: int = 8, lease_seconds: float = 60
    ) -> list[OutboxRecord]:
        """每個 task 一次只領最早事件，確保使用者看到的時間線有序。"""

        now = time.time()
        claimed: list[OutboxRecord] = []
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT o.id, o.task_id, o.delivery_target, o.content, o.attempts "
                "FROM outbox o WHERE ((o.status IN ('pending', 'retry') "
                "AND o.next_attempt_at <= ?) OR (o.status = 'sending' AND o.lease_until < ?)) "
                "AND NOT EXISTS (SELECT 1 FROM outbox prior WHERE prior.task_id = o.task_id "
                "AND prior.id < o.id AND prior.status <> 'sent') "
                "ORDER BY o.id LIMIT ?",
                (now, now, max(1, min(int(limit), 100))),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE outbox SET status = 'sending', lease_until = ? WHERE id = ?",
                    (now + lease_seconds, row["id"]),
                )
                claimed.append(OutboxRecord(
                    id=int(row["id"]),
                    task_id=str(row["task_id"]),
                    delivery_target=str(row["delivery_target"]),
                    content=str(row["content"]),
                    attempts=int(row["attempts"]),
                ))
        return claimed

    def pending_delivery_targets(self) -> set[str]:
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT delivery_target FROM outbox "
                "WHERE status IN ('pending', 'retry', 'sending')"
            ).fetchall()
        return {str(row["delivery_target"]) for row in rows}

    def next_outbox_wait(self) -> float | None:
        now = time.time()
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT MIN(CASE WHEN o.status = 'sending' "
                "THEN COALESCE(o.lease_until, 0) ELSE o.next_attempt_at END) AS ready_at "
                "FROM outbox o WHERE o.status IN ('pending', 'retry', 'sending') "
                "AND NOT EXISTS (SELECT 1 FROM outbox prior WHERE prior.task_id = o.task_id "
                "AND prior.id < o.id AND prior.status <> 'sent')"
            ).fetchone()
        if row is None or row["ready_at"] is None:
            return None
        return max(0.0, float(row["ready_at"]) - now)

    def mark_outbox_sent(self, record_id: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE outbox SET status = 'sent', sent_at = ?, lease_until = NULL, "
                "last_error = NULL WHERE id = ?",
                (time.time(), int(record_id)),
            )

    def defer_outbox(self, record_id: int, *, error: str, delay_seconds: float) -> int:
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT attempts FROM outbox WHERE id = ?", (int(record_id),)
            ).fetchone()
            if row is None:
                return 0
            attempts = int(row["attempts"]) + 1
            connection.execute(
                "UPDATE outbox SET status = 'retry', attempts = ?, next_attempt_at = ?, "
                "lease_until = NULL, last_error = ? WHERE id = ?",
                (attempts, now + delay_seconds, str(error)[:1000], int(record_id)),
            )
        return attempts

    def pending_outbox_count(self) -> int:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM outbox "
                "WHERE status IN ('pending', 'retry', 'sending')"
            ).fetchone()
        return int(row["n"]) if row else 0
