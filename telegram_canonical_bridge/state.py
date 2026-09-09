"""Bridge 的可回復 SQLite 狀態。

Telegram update、Hermes assistant 歷史與 Telegram outbox 分開持久化，讓任一端
短暫失敗時都能先查狀態再重試，而不是盲目重新執行 Agent 任務。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from .protocol import AssistantHistoryMessage, split_telegram_text
from .task_model import (
    TASK_STATUS_LABELS,
    TERMINAL_TASK_STATUSES,
    TaskNote,
    TaskRecord,
    new_task_id,
    normalize_task_id,
    render_task_card,
    sanitize_progress,
)


@dataclass(frozen=True)
class InboundRecord:
    id: int
    update_id: str
    chat_id: str
    user_id: str
    message_id: str
    text: str
    attempts: int


@dataclass(frozen=True)
class OutboxRecord:
    id: int
    chat_id: str
    content: str
    attempts: int
    kind: str = "notice"
    task_id: str | None = None


class BridgeState:
    """每個公開操作都使用短生命週期 SQLite 連線，避免重連工作互相持鎖。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

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
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inbound (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    update_id TEXT NOT NULL UNIQUE,
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    lease_until REAL,
                    last_error TEXT,
                    failure_notified INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    submitted_at REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS inbound_chat_message
                    ON inbound(chat_id, message_id);
                CREATE INDEX IF NOT EXISTS inbound_due
                    ON inbound(status, next_attempt_at, lease_until);
                CREATE TABLE IF NOT EXISTS active_routes (
                    controller_profile TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    bound_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS canonical_bindings (
                    controller_profile TEXT PRIMARY KEY,
                    root_id TEXT NOT NULL,
                    runtime_id TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS canonical_seen (
                    root_id TEXT NOT NULL,
                    message_key TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    seen_at REAL NOT NULL,
                    PRIMARY KEY(root_id, message_key)
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    dedup_key TEXT NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    lease_until REAL,
                    last_error TEXT,
                    telegram_message_id TEXT,
                    created_at REAL NOT NULL,
                    sent_at REAL
                );
                CREATE INDEX IF NOT EXISTS outbox_due
                    ON outbox(status, next_attempt_at, lease_until);
                CREATE TABLE IF NOT EXISTS bridge_tasks (
                    task_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
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
                    telegram_message_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    finished_at REAL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY(parent_task_id) REFERENCES bridge_tasks(task_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS bridge_tasks_origin_call
                    ON bridge_tasks(origin_session_id, origin_tool_call_id)
                    WHERE origin_tool_call_id <> '';
                CREATE INDEX IF NOT EXISTS bridge_tasks_chat_updated
                    ON bridge_tasks(chat_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS bridge_tasks_process
                    ON bridge_tasks(process_id);
                CREATE INDEX IF NOT EXISTS bridge_tasks_worker
                    ON bridge_tasks(worker_session_id, worker_turn_id);
                CREATE TABLE IF NOT EXISTS bridge_task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES bridge_tasks(task_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS bridge_task_events_task
                    ON bridge_task_events(task_id, id);
                CREATE TABLE IF NOT EXISTS bridge_task_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    telegram_message_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    read_at REAL,
                    UNIQUE(chat_id, telegram_message_id),
                    FOREIGN KEY(task_id) REFERENCES bridge_tasks(task_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS bridge_task_notes_unread
                    ON bridge_task_notes(task_id, status, id);
                """
            )
            self._ensure_column(connection, "outbox", "task_id", "TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS outbox_task_status "
                "ON outbox(task_id, kind, status)"
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            try:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            except sqlite3.OperationalError as exc:
                # Controller 與 OT profile 可能在升級後同時首次開啟共用 DB。
                # 兩者都通過 PRAGMA 檢查後，只有一方能先完成 ALTER；另一方
                # 收到 duplicate column 時代表 migration 已由同伴完成。
                if "duplicate column name" not in str(exc).lower():
                    raise

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        """供唯讀或自動提交初始化使用，結束時確實釋放 SQLite handle。"""

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

    def get_meta(self, key: str) -> str | None:
        with self._read_connection() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def record_inbound(self, *, update_id: str, chat_id: str, user_id: str, message_id: str, text: str) -> bool:
        now = time.time()
        with self._transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO inbound(update_id, chat_id, user_id, message_id, text, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'received', ?)",
                    (update_id, chat_id, user_id, message_id, text, now),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def mark_inbound_ignored(self, update_id: str, reason: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE inbound SET status = 'ignored', lease_until = NULL, last_error = ? WHERE update_id = ?",
                (reason[:1000], update_id),
            )

    def claim_due_inbound(self, *, limit: int = 8, lease_seconds: float = 60) -> list[InboundRecord]:
        now = time.time()
        claimed: list[InboundRecord] = []
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT id, update_id, chat_id, user_id, message_id, text, attempts FROM inbound "
                "WHERE status IN ('received', 'retry') AND next_attempt_at <= ? "
                "AND (lease_until IS NULL OR lease_until < ?) ORDER BY id LIMIT ?",
                (now, now, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE inbound SET status = 'processing', lease_until = ? WHERE id = ?",
                    (now + lease_seconds, row["id"]),
                )
                claimed.append(InboundRecord(
                    id=int(row["id"]), update_id=str(row["update_id"]), chat_id=str(row["chat_id"]),
                    user_id=str(row["user_id"]), message_id=str(row["message_id"]), text=str(row["text"]),
                    attempts=int(row["attempts"]),
                ))
        return claimed

    def mark_inbound_submitted(self, record_id: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE inbound SET status = 'submitted', submitted_at = ?, lease_until = NULL, last_error = NULL "
                "WHERE id = ?",
                (time.time(), record_id),
            )

    def mark_inbound_uncertain(self, record_id: int, *, error: str) -> None:
        """保留可能已送達的非冪等請求，避免在不明狀態下重送 prompt。"""

        with self._transaction() as connection:
            connection.execute(
                "UPDATE inbound SET status = 'uncertain', lease_until = NULL, last_error = ? WHERE id = ?",
                (error[:1000], record_id),
            )

    def defer_inbound(self, record_id: int, *, error: str, delay_seconds: float) -> tuple[int, bool]:
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT attempts, failure_notified FROM inbound WHERE id = ?", (record_id,)
            ).fetchone()
            if row is None:
                return 0, False
            attempts = int(row["attempts"]) + 1
            notify = not bool(row["failure_notified"])
            connection.execute(
                "UPDATE inbound SET status = 'retry', attempts = ?, next_attempt_at = ?, lease_until = NULL, "
                "last_error = ?, failure_notified = CASE WHEN ? THEN 1 ELSE failure_notified END WHERE id = ?",
                (attempts, now + delay_seconds, error[:1000], int(notify), record_id),
            )
        return attempts, notify

    def bind_active_route(self, *, controller_profile: str, chat_id: str, user_id: str) -> bool:
        """目前僅允許一個 Telegram 私訊綁定同一 Controller。"""

        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT chat_id, user_id FROM active_routes WHERE controller_profile = ?", (controller_profile,)
            ).fetchone()
            if row and (str(row["chat_id"]) != chat_id or str(row["user_id"]) != user_id):
                return False
            connection.execute(
                "INSERT INTO active_routes(controller_profile, chat_id, user_id, bound_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(controller_profile) DO UPDATE SET bound_at = excluded.bound_at",
                (controller_profile, chat_id, user_id, now),
            )
        return True

    def active_route(self, controller_profile: str) -> tuple[str, str] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT chat_id, user_id FROM active_routes WHERE controller_profile = ?", (controller_profile,)
            ).fetchone()
        return (str(row["chat_id"]), str(row["user_id"])) if row else None

    def route_for_canonical_session(self, session_id: str) -> tuple[str, str, str] | None:
        """只在 session 是已綁定 Controller 的 canonical 對話時回傳 route。"""

        candidate = str(session_id or "").strip()
        if not candidate:
            return None
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT r.controller_profile, r.chat_id, r.user_id "
                "FROM active_routes r JOIN canonical_bindings c "
                "ON c.controller_profile = r.controller_profile "
                "WHERE c.runtime_id = ? OR c.root_id = ? "
                "ORDER BY r.bound_at DESC LIMIT 1",
                (candidate, candidate),
            ).fetchone()
        if row is None:
            return None
        return str(row["controller_profile"]), str(row["chat_id"]), str(row["user_id"])

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskRecord:
        keys = set(row.keys())
        exit_code = row["exit_code"]
        finished_at = row["finished_at"]
        return TaskRecord(
            id=str(row["task_id"]),
            chat_id=str(row["chat_id"]),
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
            telegram_message_id=(
                str(row["telegram_message_id"]) if row["telegram_message_id"] else None
            ),
            pending_notes=int(row["pending_notes"]) if "pending_notes" in keys else 0,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            finished_at=float(finished_at) if finished_at is not None else None,
            revision=int(row["revision"]),
        )

    @staticmethod
    def _task_select() -> str:
        return (
            "SELECT t.*, (SELECT COUNT(*) FROM bridge_task_notes n "
            "WHERE n.task_id = t.task_id AND n.status = 'unread') AS pending_notes "
            "FROM bridge_tasks t "
        )

    def _task_locked(self, connection: sqlite3.Connection, task_id: str) -> TaskRecord | None:
        row = connection.execute(
            self._task_select() + "WHERE t.task_id = ?", (normalize_task_id(task_id),)
        ).fetchone()
        return self._task_from_row(row) if row else None

    def task(self, task_id: str) -> TaskRecord | None:
        with self._read_connection() as connection:
            return self._task_locked(connection, task_id)

    def find_task_by_origin_call(self, session_id: str, tool_call_id: str) -> TaskRecord | None:
        if not session_id or not tool_call_id:
            return None
        with self._read_connection() as connection:
            row = connection.execute(
                self._task_select()
                + "WHERE t.origin_session_id = ? AND t.origin_tool_call_id = ? ORDER BY t.created_at DESC LIMIT 1",
                (session_id, tool_call_id),
            ).fetchone()
        return self._task_from_row(row) if row else None

    def create_task(
        self,
        *,
        chat_id: str,
        origin_profile: str,
        origin_session_id: str,
        origin_turn_id: str,
        origin_tool_call_id: str,
        target: str,
        parent_task_id: str | None = None,
    ) -> tuple[TaskRecord, bool]:
        """建立派工 intent；同一 Hermes tool call 重入時回傳既有任務。"""

        clean_target = sanitize_progress(str(target or "").lstrip("@"), limit=128) or "unknown"
        now = time.time()
        with self._transaction() as connection:
            if origin_session_id and origin_tool_call_id:
                row = connection.execute(
                    self._task_select()
                    + "WHERE t.origin_session_id = ? AND t.origin_tool_call_id = ? LIMIT 1",
                    (origin_session_id, origin_tool_call_id),
                ).fetchone()
                if row is not None:
                    return self._task_from_row(row), False

            normalized_parent = normalize_task_id(parent_task_id)
            if normalized_parent and connection.execute(
                "SELECT 1 FROM bridge_tasks WHERE task_id = ?", (normalized_parent,)
            ).fetchone() is None:
                normalized_parent = ""

            for _attempt in range(8):
                task_id = new_task_id()
                try:
                    connection.execute(
                        "INSERT INTO bridge_tasks("
                        "task_id, chat_id, origin_profile, origin_session_id, origin_turn_id, "
                        "origin_tool_call_id, parent_task_id, target, status, progress, evidence, "
                        "created_at, updated_at, revision) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'dispatching', ?, ?, ?, ?, 1)",
                        (
                            task_id, chat_id, origin_profile or "default", origin_session_id,
                            origin_turn_id, origin_tool_call_id, normalized_parent or None,
                            clean_target, "Controller 正在呼叫原生 message_agent。",
                            "hook:pre_tool_call", now, now,
                        ),
                    )
                    break
                except sqlite3.IntegrityError:
                    if origin_session_id and origin_tool_call_id:
                        row = connection.execute(
                            self._task_select()
                            + "WHERE t.origin_session_id = ? AND t.origin_tool_call_id = ? LIMIT 1",
                            (origin_session_id, origin_tool_call_id),
                        ).fetchone()
                        if row is not None:
                            return self._task_from_row(row), False
            else:  # pragma: no cover - 48-bit random suffix collision is practically unreachable
                raise RuntimeError("無法配置 bridge task ID。")

            task = self._task_locked(connection, task_id)
            assert task is not None
            self._record_task_event_locked(connection, task)
            self._queue_task_card_locked(connection, task)
            return task, True

    @staticmethod
    def _record_task_event_locked(connection: sqlite3.Connection, task: TaskRecord) -> None:
        connection.execute(
            "INSERT INTO bridge_task_events(task_id, status, progress, evidence, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task.id, task.status, task.progress, task.evidence, task.updated_at),
        )

    @staticmethod
    def _queue_task_card_locked(connection: sqlite3.Connection, task: TaskRecord) -> None:
        """合併尚未送出的 revision，避免每個工具事件都新增 Telegram 訊息。"""

        content = render_task_card(task)
        dedup_key = f"task-card:{task.id}:revision:{task.revision}"
        queued = connection.execute(
            "SELECT id FROM outbox WHERE task_id = ? AND kind = 'task_status' "
            "AND status IN ('pending', 'retry') ORDER BY id DESC LIMIT 1",
            (task.id,),
        ).fetchone()
        if queued is not None:
            connection.execute(
                "UPDATE outbox SET dedup_key = ?, content = ?, status = 'pending', "
                "next_attempt_at = 0, lease_until = NULL, last_error = NULL WHERE id = ?",
                (dedup_key, content, int(queued["id"])),
            )
            return
        connection.execute(
            "INSERT OR IGNORE INTO outbox(chat_id, dedup_key, content, kind, task_id, status, created_at) "
            "VALUES (?, ?, ?, 'task_status', ?, 'pending', ?)",
            (task.chat_id, dedup_key, content, task.id, time.time()),
        )

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
        """原子更新任務與狀態卡；終態不會被較晚的 observer 降級。"""

        normalized = normalize_task_id(task_id)
        if not normalized:
            return None
        if status is not None and status not in TASK_STATUS_LABELS:
            raise ValueError(f"不支援的 task status：{status}")
        with self._transaction() as connection:
            current = self._task_locked(connection, normalized)
            if current is None:
                return None
            if current.terminal:
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
            for field, value, prior in (
                ("process_id", process_id, current.process_id),
                ("worker_profile", worker_profile, current.worker_profile),
                ("worker_session_id", worker_session_id, current.worker_session_id),
                ("worker_turn_id", worker_turn_id, current.worker_turn_id),
            ):
                clean = sanitize_progress(value, limit=200) if value is not None else None
                if clean and clean != prior:
                    changes[field] = clean
            if exit_code is not None and exit_code != current.exit_code:
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
            assignments = ", ".join(f"{field} = ?" for field in changes)
            connection.execute(
                f"UPDATE bridge_tasks SET {assignments} WHERE task_id = ?",
                (*changes.values(), normalized),
            )
            updated = self._task_locked(connection, normalized)
            assert updated is not None
            self._record_task_event_locked(connection, updated)
            self._queue_task_card_locked(connection, updated)
            return updated

    def acknowledge_dispatch(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        process_id: str,
    ) -> TaskRecord | None:
        task = self.find_task_by_origin_call(session_id, tool_call_id)
        if task is None:
            return None
        return self.transition_task(
            task.id,
            status="dispatched",
            process_id=process_id,
            progress="Hermes 已接受派工，背景程序已建立。",
            evidence="message_agent acknowledgement",
        )

    def fail_dispatch(
        self, *, session_id: str, tool_call_id: str, error: str
    ) -> TaskRecord | None:
        task = self.find_task_by_origin_call(session_id, tool_call_id)
        if task is None:
            return None
        return self.transition_task(
            task.id,
            status="failed",
            progress="message_agent 未能建立背景派工程序。",
            evidence="message_agent error",
            last_error=error,
        )

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

    def tasks_requiring_process_poll(self, *, limit: int = 32) -> list[TaskRecord]:
        placeholders = ",".join("?" for _ in TERMINAL_TASK_STATUSES)
        with self._read_connection() as connection:
            rows = connection.execute(
                self._task_select()
                + f"WHERE t.process_id IS NOT NULL AND t.status NOT IN ({placeholders}) "
                "ORDER BY t.updated_at LIMIT ?",
                (*sorted(TERMINAL_TASK_STATUSES), limit),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def observe_process(
        self, task_id: str, *, process_status: str, exit_code: int | None
    ) -> TaskRecord | None:
        task = self.task(task_id)
        if task is None or task.terminal:
            return task
        normalized_status = str(process_status or "").lower()
        if normalized_status == "running":
            if task.status not in {"dispatching", "dispatched"}:
                return task
            return self.transition_task(
                task.id,
                status="running",
                progress="Hermes 背景派工程序仍在執行；這不代表特定 UI 已開啟。",
                evidence="process.list: running",
            )
        if normalized_status == "exited" and exit_code is not None:
            if int(exit_code) == 0:
                return self.transition_task(
                    task.id,
                    status="completed",
                    progress="OT turn 的背景程序已成功結束；實際結果會由 Controller 回覆。",
                    evidence="process.list: exited (exit 0)",
                    exit_code=0,
                )
            return self.transition_task(
                task.id,
                status="failed",
                progress="OT turn 的背景程序以非零 exit code 結束。",
                evidence="process.list: exited",
                exit_code=int(exit_code),
                last_error=f"background process exit code {int(exit_code)}",
            )
        return task

    def complete_worker_turn(self, session_id: str, turn_id: str = "") -> TaskRecord | None:
        task = self.task_for_worker(session_id, turn_id)
        if task is None:
            return None
        return self.transition_task(
            task.id,
            status="returning",
            progress="OT 已產生最終回覆，背景程序正在把結果送回 Controller。",
            evidence="hook:post_llm_call",
        )

    def list_tasks(self, *, chat_id: str | None = None, limit: int = 10) -> list[TaskRecord]:
        with self._read_connection() as connection:
            if chat_id:
                rows = connection.execute(
                    self._task_select() + "WHERE t.chat_id = ? ORDER BY t.created_at DESC LIMIT ?",
                    (chat_id, max(1, min(limit, 50))),
                ).fetchall()
            else:
                rows = connection.execute(
                    self._task_select() + "ORDER BY t.created_at DESC LIMIT ?",
                    (max(1, min(limit, 50)),),
                ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def task_by_telegram_message(
        self, *, chat_id: str, telegram_message_id: str
    ) -> TaskRecord | None:
        with self._read_connection() as connection:
            row = connection.execute(
                self._task_select()
                + "WHERE t.chat_id = ? AND t.telegram_message_id = ? ORDER BY t.created_at DESC LIMIT 1",
                (chat_id, telegram_message_id),
            ).fetchone()
        return self._task_from_row(row) if row else None

    def add_task_note(
        self,
        *,
        task_id: str,
        chat_id: str,
        user_id: str,
        telegram_message_id: str,
        text: str,
    ) -> tuple[TaskRecord | None, bool, str]:
        normalized = normalize_task_id(task_id)
        clean_text = str(text or "").strip()
        if not normalized or not clean_text:
            return None, False, "task_id 或留言內容無效。"
        with self._transaction() as connection:
            task = self._task_locked(connection, normalized)
            if task is None or task.chat_id != chat_id:
                return task, False, "找不到這個任務。"
            if task.terminal:
                return task, False, "任務已結束；請把新需求直接傳給 Controller。"
            if task.status == "returning":
                return task, False, "OT 已產生最終回覆；請把新需求直接傳給 Controller。"
            try:
                connection.execute(
                    "INSERT INTO bridge_task_notes("
                    "task_id, chat_id, user_id, telegram_message_id, text, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'unread', ?)",
                    (
                        normalized, chat_id, user_id, telegram_message_id,
                        clean_text[:4000], time.time(),
                    ),
                )
            except sqlite3.IntegrityError:
                return task, False, "這則留言已經收錄。"
            now = time.time()
            connection.execute(
                "UPDATE bridge_tasks SET progress = ?, evidence = ?, updated_at = ?, "
                "revision = revision + 1 WHERE task_id = ?",
                ("使用者新增任務留言，等待 OT 在檢查點讀取。", "Telegram task note", now, normalized),
            )
            updated = self._task_locked(connection, normalized)
            assert updated is not None
            self._record_task_event_locked(connection, updated)
            self._queue_task_card_locked(connection, updated)
            return updated, True, "留言已保存；OT 會在下一個 task inbox 檢查點讀取。"

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
                "SELECT id, task_id, text, created_at FROM bridge_task_notes "
                "WHERE task_id = ? AND status = 'unread' ORDER BY id LIMIT ?",
                (normalized, max(1, min(limit, 50))),
            ).fetchall()
            notes = [
                TaskNote(
                    id=int(row["id"]), task_id=str(row["task_id"]),
                    text=str(row["text"]), created_at=float(row["created_at"]),
                )
                for row in rows
            ]
            if mark_read and notes:
                placeholders = ",".join("?" for _ in notes)
                now = time.time()
                connection.execute(
                    f"UPDATE bridge_task_notes SET status = 'read', read_at = ? "
                    f"WHERE id IN ({placeholders})",
                    (now, *(note.id for note in notes)),
                )
                connection.execute(
                    "UPDATE bridge_tasks SET progress = ?, evidence = ?, updated_at = ?, "
                    "revision = revision + 1 WHERE task_id = ?",
                    ("OT 已讀取最新任務留言並繼續處理。", "bridge_task_inbox", now, normalized),
                )
                task = self._task_locked(connection, normalized)
                assert task is not None
                self._record_task_event_locked(connection, task)
                self._queue_task_card_locked(connection, task)
            return task, notes

    def set_canonical_binding(self, *, controller_profile: str, root_id: str, runtime_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO canonical_bindings(controller_profile, root_id, runtime_id, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(controller_profile) DO UPDATE SET root_id = excluded.root_id, "
                "runtime_id = excluded.runtime_id, updated_at = excluded.updated_at",
                (controller_profile, root_id, runtime_id, time.time()),
            )

    def canonical_binding(self, controller_profile: str) -> tuple[str, str] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT root_id, runtime_id FROM canonical_bindings WHERE controller_profile = ?", (controller_profile,)
            ).fetchone()
        return (str(row["root_id"]), str(row["runtime_id"])) if row else None

    def record_history(
        self,
        *,
        root_id: str,
        messages: Iterable[AssistantHistoryMessage],
        chat_id: str | None,
        bootstrap: bool,
    ) -> int:
        """原子地記錄歷史與新增待送結果；bootstrap 永遠不回放舊對話。"""

        queued = 0
        now = time.time()
        with self._transaction() as connection:
            for message in messages:
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO canonical_seen(root_id, message_key, content_digest, seen_at) "
                    "VALUES (?, ?, ?, ?)",
                    (root_id, message.key, message.content_digest, now),
                ).rowcount
                if not inserted or bootstrap or not chat_id:
                    continue
                for part_index, chunk in enumerate(split_telegram_text(message.text), start=1):
                    dedup_key = f"canonical:{root_id}:{message.key}:part:{part_index}"
                    outbox_inserted = connection.execute(
                        "INSERT OR IGNORE INTO outbox(chat_id, dedup_key, content, kind, status, created_at) "
                        "VALUES (?, ?, ?, 'assistant', 'pending', ?)",
                        (chat_id, dedup_key, chunk, now),
                    ).rowcount
                    queued += int(bool(outbox_inserted))
        return queued

    def enqueue_notice(self, *, chat_id: str, dedup_key: str, content: str) -> bool:
        with self._transaction() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO outbox(chat_id, dedup_key, content, kind, status, created_at) "
                "VALUES (?, ?, ?, 'notice', 'pending', ?)",
                (chat_id, dedup_key, content, time.time()),
            ).rowcount
        return bool(inserted)

    def claim_due_outbox(self, *, limit: int = 8, lease_seconds: float = 60) -> list[OutboxRecord]:
        now = time.time()
        claimed: list[OutboxRecord] = []
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT id, chat_id, content, attempts, kind, task_id FROM outbox "
                "WHERE ((status IN ('pending', 'retry') AND next_attempt_at <= ?) "
                "OR (status = 'sending' AND lease_until < ?)) ORDER BY id LIMIT ?",
                (now, now, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE outbox SET status = 'sending', lease_until = ? WHERE id = ?",
                    (now + lease_seconds, row["id"]),
                )
                claimed.append(OutboxRecord(
                    id=int(row["id"]), chat_id=str(row["chat_id"]), content=str(row["content"]),
                    attempts=int(row["attempts"]), kind=str(row["kind"]),
                    task_id=str(row["task_id"]) if row["task_id"] else None,
                ))
        return claimed

    def mark_outbox_sent(self, record_id: int, telegram_message_id: str) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT task_id FROM outbox WHERE id = ?", (record_id,)
            ).fetchone()
            connection.execute(
                "UPDATE outbox SET status = 'sent', sent_at = ?, lease_until = NULL, last_error = NULL, "
                "telegram_message_id = ? WHERE id = ?",
                (time.time(), telegram_message_id, record_id),
            )
            if row is not None and row["task_id"]:
                connection.execute(
                    "UPDATE bridge_tasks SET telegram_message_id = COALESCE(telegram_message_id, ?) "
                    "WHERE task_id = ?",
                    (telegram_message_id, str(row["task_id"])),
                )

    def task_telegram_message_id(self, task_id: str) -> str | None:
        task = self.task(task_id)
        return task.telegram_message_id if task else None

    def clear_task_telegram_message(self, task_id: str, *, expected_message_id: str) -> bool:
        """狀態卡被刪除或不可編輯時，解除舊 ID 以便安全建立新卡。"""

        normalized = normalize_task_id(task_id)
        if not normalized or not expected_message_id:
            return False
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE bridge_tasks SET telegram_message_id = NULL "
                "WHERE task_id = ? AND telegram_message_id = ?",
                (normalized, str(expected_message_id)),
            ).rowcount
        return bool(changed)

    def defer_outbox(self, record_id: int, *, error: str, delay_seconds: float) -> int:
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute("SELECT attempts FROM outbox WHERE id = ?", (record_id,)).fetchone()
            if row is None:
                return 0
            attempts = int(row["attempts"]) + 1
            connection.execute(
                "UPDATE outbox SET status = 'retry', attempts = ?, next_attempt_at = ?, lease_until = NULL, "
                "last_error = ? WHERE id = ?",
                (attempts, now + delay_seconds, error[:1000], record_id),
            )
        return attempts

    def counts(self) -> dict[str, int]:
        with self._read_connection() as connection:
            inbound = connection.execute(
                "SELECT COUNT(*) AS n FROM inbound WHERE status IN ('received', 'retry', 'processing', 'uncertain')"
            ).fetchone()
            uncertain = connection.execute(
                "SELECT COUNT(*) AS n FROM inbound WHERE status = 'uncertain'"
            ).fetchone()
            outbox = connection.execute(
                "SELECT COUNT(*) AS n FROM outbox WHERE status IN ('pending', 'retry', 'sending')"
            ).fetchone()
        return {
            "pending_inbound": int(inbound["n"]),
            "uncertain_inbound": int(uncertain["n"]),
            "pending_outbox": int(outbox["n"]),
        }
