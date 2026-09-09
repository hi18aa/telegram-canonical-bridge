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
    RECOVERABLE_TERMINAL_TASK_STATUSES,
    TASK_STATUS_LABELS,
    TERMINAL_TASK_STATUSES,
    TaskNote,
    TaskRecord,
    new_task_id,
    normalize_task_id,
    render_task_event,
    sanitize_progress,
)


AUTOMATIC_TASK_EVENT_MIN_INTERVAL_SECONDS = 15.0
ACTIVE_TYPING_TASK_STATUSES = ("dispatching", "dispatched", "running", "returning")


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
    reply_to_message_id: str | None = None
    silent: bool = False


class BridgeState:
    """每個公開操作都使用短生命週期 SQLite 連線，避免重連工作互相持鎖。"""

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
                    submitted_at REAL,
                    responded_at REAL,
                    response_pending INTEGER NOT NULL DEFAULT 0
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
                    reply_to_message_id TEXT,
                    silent INTEGER NOT NULL DEFAULT 0,
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
            self._ensure_column(connection, "outbox", "reply_to_message_id", "TEXT")
            self._ensure_column(connection, "outbox", "silent", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "inbound", "responded_at", "REAL")
            self._ensure_column(
                connection, "inbound", "response_pending", "INTEGER NOT NULL DEFAULT 0"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS outbox_task_status "
                "ON outbox(task_id, kind, status)"
            )
            # v0.3.x 把缺乏 final／exit 證據的 runner 寫成 ``finished``；新語意
            # 改為明確的 ``unconfirmed``，避免 API 使用者誤認成成功終態。
            connection.execute(
                "UPDATE bridge_tasks SET status = 'unconfirmed' WHERE status = 'finished'"
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
                "WHERE ((status IN ('received', 'retry') AND next_attempt_at <= ? "
                "AND (lease_until IS NULL OR lease_until < ?)) "
                "OR (status = 'processing' AND lease_until < ?)) ORDER BY id LIMIT ?",
                (now, now, now, limit),
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
                "UPDATE inbound SET status = 'submitted', submitted_at = ?, response_pending = 1, "
                "responded_at = NULL, lease_until = NULL, last_error = NULL "
                "WHERE id = ?",
                (time.time(), record_id),
            )

    def mark_inbound_uncertain(self, record_id: int, *, error: str) -> None:
        """保留可能已送達的非冪等請求，避免在不明狀態下重送 prompt。"""

        with self._transaction() as connection:
            connection.execute(
                "UPDATE inbound SET status = 'uncertain', response_pending = 0, "
                "lease_until = NULL, last_error = ? WHERE id = ?",
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
                "response_pending = 0, last_error = ?, "
                "failure_notified = CASE WHEN ? THEN 1 ELSE failure_notified END WHERE id = ?",
                (attempts, now + delay_seconds, error[:1000], int(notify), record_id),
            )
        return attempts, notify

    def active_chat_ids(self, *, max_inbound_age_seconds: float = 21_600) -> list[str]:
        """回傳目前有證據正在處理的 chat，供 Telegram typing heartbeat 使用。"""

        cutoff = time.time() - max(60.0, float(max_inbound_age_seconds))
        now = time.time()
        placeholders = ",".join("?" for _ in ACTIVE_TYPING_TASK_STATUSES)
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT chat_id FROM inbound WHERE "
                "((status = 'processing' AND lease_until >= ?) OR "
                "(status = 'submitted' AND response_pending = 1 "
                "AND submitted_at >= ?)) "
                "UNION SELECT chat_id FROM bridge_tasks "
                f"WHERE status IN ({placeholders}) ORDER BY chat_id",
                (now, cutoff, *ACTIVE_TYPING_TASK_STATUSES),
            ).fetchall()
        return [str(row["chat_id"]) for row in rows]

    def chat_has_active_work(
        self, chat_id: str, *, max_inbound_age_seconds: float = 21_600
    ) -> bool:
        return str(chat_id) in set(
            self.active_chat_ids(max_inbound_age_seconds=max_inbound_age_seconds)
        )

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

    def latest_explicit_progress(self, task_id: str) -> str:
        """回傳 OT 最近一次明確回報，供自動 lifecycle transition 保留。"""

        normalized = normalize_task_id(task_id)
        if not normalized:
            return ""
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT progress FROM bridge_task_events "
                "WHERE task_id = ? AND evidence IN "
                "('OT explicit bridge_task_update', 'OT explicit bridge_task_result') "
                "ORDER BY id DESC LIMIT 1",
                (normalized,),
            ).fetchone()
        return str(row["progress"]) if row else ""

    def latest_explicit_result(self, task_id: str) -> str:
        """回傳 OT 以 ``status=result`` 標示的最終公開里程碑。"""

        normalized = normalize_task_id(task_id)
        if not normalized:
            return ""
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT progress FROM bridge_task_events "
                "WHERE task_id = ? AND evidence = 'OT explicit bridge_task_result' "
                "ORDER BY id DESC LIMIT 1",
                (normalized,),
            ).fetchone()
        return str(row["progress"]) if row else ""

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
            self._queue_task_event_locked(connection, task)
            return task, True

    @staticmethod
    def _record_task_event_locked(connection: sqlite3.Connection, task: TaskRecord) -> None:
        connection.execute(
            "INSERT INTO bridge_task_events(task_id, status, progress, evidence, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task.id, task.status, task.progress, task.evidence, task.updated_at),
        )

    @staticmethod
    def _queue_task_event_locked(connection: sqlite3.Connection, task: TaskRecord) -> None:
        """建立不可變時間線事件；高頻自動工具活動會節流，明確里程碑不合併。"""

        recent_events = connection.execute(
            "SELECT status, evidence FROM bridge_task_events WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 2",
            (task.id,),
        ).fetchall()
        previous_status = str(recent_events[1]["status"]) if len(recent_events) > 1 else ""
        status_changed = not previous_status or previous_status != task.status
        explicit = task.evidence in {
            "OT explicit bridge_task_update",
            "OT explicit bridge_task_result",
        }
        now = time.time()
        if not status_changed and not explicit:
            latest = connection.execute(
                "SELECT created_at FROM outbox WHERE task_id = ? AND kind = 'task_status' "
                "ORDER BY id DESC LIMIT 1",
                (task.id,),
            ).fetchone()
            if latest is not None and now - float(latest["created_at"]) < AUTOMATIC_TASK_EVENT_MIN_INTERVAL_SECONDS:
                return

        content = render_task_event(task)
        dedup_key = f"task-event:{task.id}:revision:{task.revision}"
        silent = int(
            task.status
            not in {"blocked", "unconfirmed", "failed", "cancelled", "finished", "completed"}
        )
        connection.execute(
            "INSERT OR IGNORE INTO outbox("
            "chat_id, dedup_key, content, kind, task_id, status, silent, created_at) "
            "VALUES (?, ?, ?, 'task_status', ?, 'pending', ?, ?)",
            (task.chat_id, dedup_key, content, task.id, silent, now),
        )

    @staticmethod
    def _mark_unread_notes_missed_locked(
        connection: sqlite3.Connection, task_id: str, *, now: float
    ) -> int:
        """結束任務時封存 OT 未讀留言，避免完成卡持續顯示「待讀」。"""

        row = connection.execute(
            "SELECT COUNT(*) AS n FROM bridge_task_notes "
            "WHERE task_id = ? AND status = 'unread'",
            (task_id,),
        ).fetchone()
        count = int(row["n"]) if row else 0
        if count:
            connection.execute(
                "UPDATE bridge_task_notes SET status = 'missed', read_at = ? "
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
            "INSERT OR IGNORE INTO outbox(chat_id, dedup_key, content, kind, status, created_at) "
            "VALUES (?, ?, ?, 'notice', 'pending', ?)",
            (
                task.chat_id,
                f"task-notes-missed:{task.id}",
                (
                    f"⚠️ 任務 {task.id} {closure}；OT 未及讀取 {count} 則任務留言，"
                    "內容未納入本次結果。請把需求另傳成 Controller 的新訊息。"
                ),
                time.time(),
            ),
        )

    def _reconcile_closed_task_notes(self) -> int:
        """升級或重啟時結清舊版遺留在 closed task 的未讀留言。"""

        closed_statuses = ("returning", *sorted(TERMINAL_TASK_STATUSES))
        placeholders = ",".join("?" for _ in closed_statuses)
        reconciled = 0
        with self._transaction() as connection:
            rows = connection.execute(
                self._task_select()
                + f"WHERE t.status IN ({placeholders}) AND EXISTS ("
                "SELECT 1 FROM bridge_task_notes n "
                "WHERE n.task_id = t.task_id AND n.status = 'unread')",
                closed_statuses,
            ).fetchall()
            for row in rows:
                task = self._task_from_row(row)
                now = time.time()
                missed = self._mark_unread_notes_missed_locked(
                    connection, task.id, now=now
                )
                if not missed:
                    continue
                connection.execute(
                    "UPDATE bridge_tasks SET updated_at = ?, revision = revision + 1 "
                    "WHERE task_id = ?",
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
        """原子更新任務與狀態卡；確定終態不會被較晚的 observer 降級。

        ``unconfirmed``／舊版 ``finished`` 不是成功或失敗證明。若稍後才收到
        worker hook 或 Hermes completion notification，允許它恢復成可判定狀態。
        """

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
                current.status in RECOVERABLE_TERMINAL_TASK_STATUSES
                and status is not None
                and status != current.status
            )
            if current.terminal and not recovering:
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
            elif current.status in RECOVERABLE_TERMINAL_TASK_STATUSES:
                changes["finished_at"] = None
            assignments = ", ".join(f"{field} = ?" for field in changes)
            connection.execute(
                f"UPDATE bridge_tasks SET {assignments} WHERE task_id = ?",
                (*changes.values(), normalized),
            )
            missed_notes = 0
            if next_status == "returning" or next_status in TERMINAL_TASK_STATUSES:
                missed_notes = self._mark_unread_notes_missed_locked(
                    connection, normalized, now=now
                )
            updated = self._task_locked(connection, normalized)
            assert updated is not None
            self._record_task_event_locked(connection, updated)
            self._queue_task_event_locked(connection, updated)
            self._queue_missed_notes_notice_locked(connection, updated, missed_notes)
            return updated

    def acknowledge_dispatch(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        process_id: str,
        delivery_status: str = "sent",
    ) -> TaskRecord | None:
        task = self.find_task_by_origin_call(session_id, tool_call_id)
        if task is None:
            return None
        queued = str(delivery_status or "").lower() == "queued"
        return self.transition_task(
            task.id,
            status="waiting" if queued else "dispatched",
            process_id=process_id,
            progress=(
                "Hermes 已持久化收件並排入目標 Bot Chat；尚未證明 OT turn 已啟動。"
                if queued
                else "Hermes 已接受派工，背景 runner 已建立；尚未證明 OT turn 已啟動。"
            ),
            evidence=(
                "message_agent queued acknowledgement"
                if queued
                else "message_agent sent acknowledgement"
            ),
        )

    def acknowledge_without_process(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        delivery_status: str,
    ) -> TaskRecord | None:
        """處理新版 queued 回條或無 handle 的不完整 acknowledgement。"""

        task = self.find_task_by_origin_call(session_id, tool_call_id)
        if task is None:
            return None
        queued = str(delivery_status or "").lower() == "queued"
        return self.transition_task(
            task.id,
            status="waiting" if queued else "unconfirmed",
            progress=(
                "Hermes 已持久化收件並排入目標 Bot Chat；尚未觀察 OT turn 啟動。"
                if queued
                else "message_agent 回傳成功形狀，但沒有 process handle；bridge 無法追蹤 runner。"
            ),
            evidence=(
                "message_agent queued acknowledgement (no process handle)"
                if queued
                else "message_agent acknowledgement missing process handle"
            ),
            last_error=(
                "" if queued else "派工結果未確認；等待較晚的 OT worker hook。"
            ),
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

    def task_by_process_id(self, process_id: str) -> TaskRecord | None:
        """依 Hermes opaque background handle 找到最近一筆追蹤任務。"""

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
        if task is None or (
            task.terminal and task.status not in RECOVERABLE_TERMINAL_TASK_STATUSES
        ):
            return task
        normalized_status = str(process_status or "").lower()
        if normalized_status == "running":
            if task.status not in {"dispatching", "dispatched"}:
                return task
            return self.transition_task(
                task.id,
                progress=(
                    "Hermes 背景派工 runner 仍在執行；尚未觀察到 OT turn 啟動，"
                    "也不代表任何 UI 已開啟。"
                ),
                evidence="agents.list: runner running (worker not observed)",
            )
        if normalized_status == "exited":
            if (
                task.evidence == "Hermes completion notification: live delivery queued"
                and exit_code in {None, 0}
            ):
                # runner 的工作只到 durable admission；真正 OT turn 可能仍排在
                # live Bot Chat 佇列後方，不能用 runner exited 覆寫這份較強證據。
                return task
            if task.status == "returning":
                return self.transition_task(
                    task.id,
                    status="completed",
                    progress=task.progress or "OT 已產生最終回覆，且背景 runner 已結束。",
                    evidence="post_llm_call + agents.list: exited",
                    exit_code=exit_code,
                )
            if exit_code is not None and int(exit_code) != 0:
                return self.transition_task(
                    task.id,
                    status="failed",
                    progress="背景派工 runner 以非零 exit code 結束；OT turn 未完成。",
                    evidence="process telemetry: exited",
                    exit_code=int(exit_code),
                    last_error=f"background process exit code {int(exit_code)}",
                )
            return self.transition_task(
                task.id,
                status="waiting",
                progress=(
                    "背景派工 runner 已結束，但 bridge 尚未觀察到 OT final；"
                    "正在等待 Hermes 的正式完成通知判定結果。"
                    if task.worker_session_id
                    else
                    "背景派工 runner 已結束，但尚未觀察到 OT turn 啟動；"
                    "正在等待 Hermes 的正式完成通知判定結果。"
                ),
                evidence="agents.list: exited; awaiting completion notification",
                exit_code=exit_code,
            )
        if normalized_status == "absent_after_final" and task.status == "returning":
            return self.transition_task(
                task.id,
                status="completed",
                progress=task.progress or "OT 已產生最終回覆。",
                evidence="post_llm_call + agents.list: absent after grace",
            )
        if normalized_status in {"unconfirmed_after_grace", "absent_without_final"}:
            return self.transition_task(
                task.id,
                status="unconfirmed",
                progress=(
                    "未觀察到 OT turn 啟動或最終回覆；runner 已結束或不再出現在 Hermes 摘要中。"
                    if not task.worker_session_id
                    else
                    "曾觀察到 OT turn 啟動，但沒有 final hook；runner 已結束或不再出現在 Hermes 摘要中。"
                ),
                evidence=(
                    "runner absent/exited after grace; no worker final evidence"
                ),
                last_error=(
                    "執行結果未知；不要把這個狀態當成完成，也不要盲目重派。"
                ),
            )
        return task

    def complete_worker_turn(
        self,
        session_id: str,
        turn_id: str = "",
        *,
        assistant_response: str = "",
    ) -> TaskRecord | None:
        """記錄 OT 已產生最終答覆，並保留最有價值的公開結果。

        明確的 ``bridge_task_update`` 永遠優先；只有 OT 漏掉明確回報時，
        才保存 ``post_llm_call`` 提供、經單行清理的 final assistant response。
        """

        task = self.task_for_worker(session_id, turn_id)
        if task is None:
            return None
        explicit_result = self.latest_explicit_result(task.id)
        explicit_progress = self.latest_explicit_progress(task.id)
        final_summary = ""
        if isinstance(assistant_response, str):
            final_summary = sanitize_progress(assistant_response, limit=1000)
        return self.transition_task(
            task.id,
            status="returning",
            progress=(
                explicit_result
                or (f"OT 最終回覆：{final_summary}" if final_summary else "")
                or explicit_progress
                or task.progress
                or "OT 已產生最終回覆，但 hook 未提供可顯示文字；等待背景程序結束。"
            ),
            evidence=(
                "hook:post_llm_call (sanitized final fallback)"
                if final_summary and not explicit_result
                else "hook:post_llm_call"
            ),
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
                + "WHERE t.chat_id = ? AND (t.telegram_message_id = ? OR EXISTS ("
                "SELECT 1 FROM outbox o WHERE o.task_id = t.task_id AND o.chat_id = ? "
                "AND o.telegram_message_id = ?)) ORDER BY t.created_at DESC LIMIT 1",
                (chat_id, telegram_message_id, chat_id, telegram_message_id),
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
            self._queue_task_event_locked(connection, updated)
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
                self._queue_task_event_locked(connection, task)
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
                reply_to_message_id = self._complete_oldest_inbound_locked(
                    connection, chat_id=chat_id, now=now
                )
                for part_index, chunk in enumerate(split_telegram_text(message.text), start=1):
                    dedup_key = f"canonical:{root_id}:{message.key}:part:{part_index}"
                    outbox_inserted = connection.execute(
                        "INSERT OR IGNORE INTO outbox("
                        "chat_id, dedup_key, content, kind, status, reply_to_message_id, created_at) "
                        "VALUES (?, ?, ?, 'assistant', 'pending', ?, ?)",
                        (
                            chat_id, dedup_key, chunk,
                            reply_to_message_id if part_index == 1 else None,
                            now,
                        ),
                    ).rowcount
                    queued += int(bool(outbox_inserted))
        return queued

    @staticmethod
    def _complete_oldest_inbound_locked(
        connection: sqlite3.Connection, *, chat_id: str, now: float
    ) -> str | None:
        row = connection.execute(
            "SELECT id, message_id FROM inbound WHERE chat_id = ? AND status = 'submitted' "
            "AND response_pending = 1 ORDER BY id LIMIT 1",
            (chat_id,),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            "UPDATE inbound SET status = 'responded', response_pending = 0, responded_at = ? "
            "WHERE id = ?",
            (now, int(row["id"])),
        )
        return str(row["message_id"])

    def enqueue_notice(
        self,
        *,
        chat_id: str,
        dedup_key: str,
        content: str,
        reply_to_message_id: str | None = None,
        silent: bool = False,
    ) -> bool:
        with self._transaction() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO outbox("
                "chat_id, dedup_key, content, kind, status, reply_to_message_id, silent, created_at) "
                "VALUES (?, ?, ?, 'notice', 'pending', ?, ?, ?)",
                (
                    chat_id, dedup_key, content, reply_to_message_id,
                    int(bool(silent)), time.time(),
                ),
            ).rowcount
        return bool(inserted)

    def claim_due_outbox(self, *, limit: int = 8, lease_seconds: float = 60) -> list[OutboxRecord]:
        now = time.time()
        claimed: list[OutboxRecord] = []
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT o.id, o.chat_id, o.content, o.attempts, o.kind, o.task_id, "
                "o.reply_to_message_id, o.silent FROM outbox o "
                "WHERE ((o.status IN ('pending', 'retry') AND o.next_attempt_at <= ?) "
                "OR (o.status = 'sending' AND o.lease_until < ?)) "
                "AND (o.task_id IS NULL OR NOT EXISTS ("
                "SELECT 1 FROM outbox prior WHERE prior.task_id = o.task_id "
                "AND prior.id < o.id AND prior.status <> 'sent')) "
                "ORDER BY o.id LIMIT ?",
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
                    reply_to_message_id=(
                        str(row["reply_to_message_id"]) if row["reply_to_message_id"] else None
                    ),
                    silent=bool(row["silent"]),
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
        """時間線錨點被刪除或 compact 卡不可編輯時，解除舊 ID 以便重建。"""

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
