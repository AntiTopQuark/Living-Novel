from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .types import UsageRecord


class UsageStore:
    def __init__(self, sqlite_path: str) -> None:
        self.sqlite_path = str(sqlite_path)
        db_path = Path(self.sqlite_path)
        if db_path.parent and str(db_path.parent) != ".":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    book_id TEXT NOT NULL DEFAULT 'default_book',
                    agent_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    input_cost REAL NOT NULL,
                    output_cost REAL NOT NULL,
                    total_cost REAL NOT NULL,
                    latency_ms REAL NOT NULL,
                    estimated INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT
                )
                """
            )
            self._ensure_column(
                conn,
                "usage_events",
                "book_id",
                "TEXT NOT NULL DEFAULT 'default_book'",
            )
            conn.execute(
                "UPDATE usage_events SET book_id = 'default_book' WHERE book_id IS NULL OR book_id = ''"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_created_at ON usage_events(created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_group ON usage_events(book_id, agent_id, provider, model)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_book_created_at ON usage_events(book_id, created_at)"
            )

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_def: str,
    ) -> None:
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name in columns:
            return
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")

    def insert(self, record: UsageRecord) -> None:
        payload = asdict(record)
        payload["created_at"] = record.created_at.astimezone(timezone.utc).isoformat()
        payload["estimated"] = 1 if record.estimated else 0

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO usage_events (
                    request_id, created_at, book_id, agent_id, provider, model, endpoint_id,
                    prompt_tokens, completion_tokens, total_tokens,
                    input_cost, output_cost, total_cost,
                    latency_ms, estimated, status, error
                )
                VALUES (
                    :request_id, :created_at, :book_id, :agent_id, :provider, :model, :endpoint_id,
                    :prompt_tokens, :completion_tokens, :total_tokens,
                    :input_cost, :output_cost, :total_cost,
                    :latency_ms, :estimated, :status, :error
                )
                """,
                payload,
            )

    def query(
        self,
        *,
        start: datetime,
        end: datetime,
        book_id: str | None = None,
        agent_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        group_by: Iterable[str] = ("agent", "provider", "model"),
    ) -> list[dict[str, object]]:
        alias_to_column = {
            "agent": "agent_id",
            "agent_id": "agent_id",
            "book": "book_id",
            "book_id": "book_id",
            "provider": "provider",
            "model": "model",
            "endpoint": "endpoint_id",
            "endpoint_id": "endpoint_id",
            "status": "status",
        }

        group_columns: list[str] = []
        for item in group_by:
            if item not in alias_to_column:
                raise ValueError(f"Unsupported group_by field: {item}")
            column = alias_to_column[item]
            if column not in group_columns:
                group_columns.append(column)

        if not group_columns:
            raise ValueError("group_by cannot be empty")

        where_parts = ["created_at >= :start", "created_at <= :end"]
        params: dict[str, object] = {
            "start": start.astimezone(timezone.utc).isoformat(),
            "end": end.astimezone(timezone.utc).isoformat(),
        }

        if book_id:
            where_parts.append("book_id = :book_id")
            params["book_id"] = book_id
        if agent_id:
            where_parts.append("agent_id = :agent_id")
            params["agent_id"] = agent_id
        if provider:
            where_parts.append("provider = :provider")
            params["provider"] = provider
        if model:
            where_parts.append("model = :model")
            params["model"] = model

        group_sql = ", ".join(group_columns)
        sql = f"""
            SELECT
                {group_sql},
                COUNT(*) AS requests,
                SUM(prompt_tokens) AS prompt_tokens,
                SUM(completion_tokens) AS completion_tokens,
                SUM(total_tokens) AS total_tokens,
                SUM(input_cost) AS input_cost,
                SUM(output_cost) AS output_cost,
                SUM(total_cost) AS total_cost,
                AVG(latency_ms) AS avg_latency_ms,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_requests,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_requests,
                SUM(estimated) AS estimated_records
            FROM usage_events
            WHERE {' AND '.join(where_parts)}
            GROUP BY {group_sql}
            ORDER BY {group_sql}
        """

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        results: list[dict[str, object]] = []
        for row in rows:
            row_dict = dict(row)
            row_dict["success_rate"] = (
                float(row_dict["success_requests"]) / float(row_dict["requests"])
                if row_dict["requests"]
                else 0.0
            )
            results.append(row_dict)
        return results
