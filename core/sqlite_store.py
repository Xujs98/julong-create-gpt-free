# -*- coding: utf-8 -*-
"""SQLite-backed storage for registration state.

The application still writes its historical JSON/TXT files as compatibility
exports, but this module owns the authoritative transactional copy.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1


class SQLiteStore:
    """Small repository for ordered JSON records and singleton documents."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 10_000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            timeout=max(self.busy_timeout_ms / 1000, 1),
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS storage_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS records (
                    entity TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    record_id INTEGER,
                    email TEXT,
                    status TEXT,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (entity, position)
                );

                CREATE INDEX IF NOT EXISTS idx_records_entity_record_id
                    ON records(entity, record_id);
                CREATE INDEX IF NOT EXISTS idx_records_entity_email
                    ON records(entity, email COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_records_entity_status
                    ON records(entity, status);

                CREATE TABLE IF NOT EXISTS documents (
                    entity TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL
                );
                """
            )
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.execute(
                "INSERT INTO storage_metadata(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
        self._initialized = True

    @staticmethod
    def _payload(row: Mapping[str, Any]) -> tuple[int | None, str | None, str | None, str]:
        record_id = row.get("id")
        try:
            record_id = int(record_id) if record_id is not None else None
        except (TypeError, ValueError):
            record_id = None
        email = row.get("email")
        status = row.get("status")
        payload_json = json.dumps(dict(row), ensure_ascii=False, separators=(",", ":"))
        return (
            record_id,
            str(email) if email is not None else None,
            str(status) if status is not None else None,
            payload_json,
        )

    @classmethod
    def _replace_records(
        cls,
        conn: sqlite3.Connection,
        entity: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        conn.execute("DELETE FROM records WHERE entity=?", (entity,))
        conn.executemany(
            """
            INSERT INTO records(entity, position, record_id, email, status, payload_json)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            [
                (entity, position, *cls._payload(row))
                for position, row in enumerate(rows)
            ],
        )

    def load_records(self, entity: str) -> list[dict[str, Any]]:
        self.initialize()
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM records WHERE entity=? ORDER BY position",
                (entity,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def replace_records(self, entity: str, rows: Sequence[Mapping[str, Any]]) -> None:
        self.initialize()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._replace_records(conn, entity, rows)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def load_document(self, entity: str, default: Any) -> Any:
        self.initialize()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT payload_json FROM documents WHERE entity=?",
                (entity,),
            ).fetchone()
        return default if row is None else json.loads(row["payload_json"])

    def replace_document(self, entity: str, value: Any) -> None:
        self.initialize()
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO documents(entity, payload_json) VALUES(?, ?) "
                    "ON CONFLICT(entity) DO UPDATE SET payload_json=excluded.payload_json",
                    (entity, payload),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def replace_all(
        self,
        collections: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        documents: Mapping[str, Any] | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        """Atomically replace all supplied entities and migration metadata."""
        self.initialize()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for entity, rows in collections.items():
                    self._replace_records(conn, entity, rows)
                for entity, value in (documents or {}).items():
                    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    conn.execute(
                        "INSERT INTO documents(entity, payload_json) VALUES(?, ?) "
                        "ON CONFLICT(entity) DO UPDATE SET payload_json=excluded.payload_json",
                        (entity, payload),
                    )
                for key, value in (metadata or {}).items():
                    conn.execute(
                        "INSERT INTO storage_metadata(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def get_metadata(self, key: str) -> str | None:
        self.initialize()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value FROM storage_metadata WHERE key=?",
                (key,),
            ).fetchone()
        return None if row is None else str(row["value"])

    def counts(self) -> dict[str, int]:
        self.initialize()
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT entity, COUNT(*) AS count FROM records GROUP BY entity ORDER BY entity"
            ).fetchall()
        return {str(row["entity"]): int(row["count"]) for row in rows}

    def integrity_check(self) -> str:
        self.initialize()
        with self._connection() as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"
