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
from typing import Iterable, Iterator

from .protocol import AssistantHistoryMessage, split_telegram_text


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
                """
            )

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
        """V1 僅允許一個 Telegram 私訊綁定同一 Controller。"""

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
                "SELECT id, chat_id, content, attempts FROM outbox "
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
                    attempts=int(row["attempts"]),
                ))
        return claimed

    def mark_outbox_sent(self, record_id: int, telegram_message_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE outbox SET status = 'sent', sent_at = ?, lease_until = NULL, last_error = NULL, "
                "telegram_message_id = ? WHERE id = ?",
                (time.time(), telegram_message_id, record_id),
            )

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
