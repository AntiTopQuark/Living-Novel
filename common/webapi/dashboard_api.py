from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from common.agents import (
    AgentAction,
    AgentFactory,
    CharacterStateUpdate,
    DirectorDecision,
    MemoryEvent,
    SceneInput,
    TurnLog,
)
from common.agents.auto_character import AutoCharacterService, AutoRoleGenerationResult


DEFAULT_BOOK_ID = "default_book"
DEFAULT_URLS_CONFIG = "config/llm_urls.yaml"
DEFAULT_RUNTIME_CONFIG = "config/llm_runtime.yaml"
DEFAULT_FACTORY_CONFIG = "config/agent_factory.yaml"
BOOK_PROFILE_FIELDS: tuple[tuple[str, str], ...] = (
    ("background", "背景"),
    ("worldview", "世界观"),
    ("era_setting", "时代设定"),
    ("genre", "题材类型"),
    ("protagonist", "主角"),
    ("protagonist_goal", "主角目标"),
    ("core_conflict", "核心冲突"),
    ("narrative_style", "叙事风格"),
)
BOOK_PROFILE_FIELD_KEYS = tuple(key for key, _ in BOOK_PROFILE_FIELDS)


class BookProfilePayload(BaseModel):
    background: str = Field(min_length=1)
    worldview: str = Field(min_length=1)
    era_setting: str = Field(min_length=1)
    genre: str = Field(min_length=1)
    protagonist: str = Field(min_length=1)
    protagonist_goal: str = Field(min_length=1)
    core_conflict: str = Field(min_length=1)
    narrative_style: str = Field(min_length=1)


class BookCreateRequest(BaseModel):
    book_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    profile: BookProfilePayload | None = None


class BookProfilePatchRequest(BaseModel):
    background: str | None = Field(default=None, min_length=1)
    worldview: str | None = Field(default=None, min_length=1)
    era_setting: str | None = Field(default=None, min_length=1)
    genre: str | None = Field(default=None, min_length=1)
    protagonist: str | None = Field(default=None, min_length=1)
    protagonist_goal: str | None = Field(default=None, min_length=1)
    core_conflict: str | None = Field(default=None, min_length=1)
    narrative_style: str | None = Field(default=None, min_length=1)


class SceneStartRequest(BaseModel):
    book_id: str = Field(min_length=1)
    scene_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    participants: list[str] = Field(min_length=1)
    context: str = ""
    state: dict[str, Any] = Field(default_factory=dict)
    max_turns: int | None = Field(default=None, ge=1, le=200)


class SceneControlRequest(BaseModel):
    book_id: str = Field(min_length=1)
    message: str | None = None


class SceneInterruptRequest(BaseModel):
    book_id: str = Field(min_length=1)
    idea: str = Field(min_length=1)


class DecisionSelectRequest(BaseModel):
    book_id: str = Field(min_length=1)
    selected_option: str = Field(min_length=1)


class InteractiveSettingsPatchRequest(BaseModel):
    uncertainty_enabled: bool | None = None
    decision_timeout_seconds: int | None = Field(default=None, ge=5, le=3600)


class DashboardRepository:
    def __init__(self, sqlite_path: str) -> None:
        self.sqlite_path = sqlite_path
        db_path = Path(sqlite_path)
        if db_path.parent and str(db_path.parent) != ".":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            self._migrate_scene_controls(conn)

            for table in (
                "scene_turn_logs",
                "scene_state_snapshots",
                "agent_memory_events",
                "usage_events",
            ):
                self._ensure_book_column(conn, table)

            self._create_books_table(conn)
            self._create_book_profiles_table(conn)
            self._ensure_default_book(conn)
            self._create_runtime_settings_table(conn)
            self._create_run_sessions_table(conn)
            self._create_interventions_table(conn)
            self._create_decision_requests_table(conn)
            self._create_auto_role_events_table(conn)
            self._create_character_state_tables(conn)

            if self._table_exists(conn, "scene_turn_logs"):
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_scene_turn_logs_book_scene_turn ON scene_turn_logs(book_id, scene_id, turn)"
                )
            if self._table_exists(conn, "scene_state_snapshots"):
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_scene_snapshots_book_scene_turn ON scene_state_snapshots(book_id, scene_id, turn)"
                )
            if self._table_exists(conn, "agent_memory_events"):
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memory_events_book_agent_scene ON agent_memory_events(book_id, agent_id, scene_id)"
                )
            if self._table_exists(conn, "usage_events"):
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_usage_events_book_created_at ON usage_events(book_id, created_at)"
                )

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table_name: str) -> list[sqlite3.Row]:
        return conn.execute(f"PRAGMA table_info({table_name})").fetchall()

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_def: str,
    ) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
        if column_name in columns:
            return
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")

    def _ensure_book_column(self, conn: sqlite3.Connection, table_name: str) -> None:
        if not self._table_exists(conn, table_name):
            return
        self._ensure_column(
            conn,
            table_name,
            "book_id",
            f"TEXT NOT NULL DEFAULT '{DEFAULT_BOOK_ID}'",
        )
        conn.execute(
            f"UPDATE {table_name} SET book_id = ? WHERE book_id IS NULL OR book_id = ''",
            (DEFAULT_BOOK_ID,),
        )

    def _migrate_scene_controls(self, conn: sqlite3.Connection) -> None:
        target_columns = {"book_id", "scene_id", "status", "updated_at", "message"}
        target_pk = ["book_id", "scene_id"]

        if not self._table_exists(conn, "scene_controls"):
            self._create_scene_controls_table(conn)
            return

        columns = self._table_columns(conn, "scene_controls")
        current_columns = {row["name"] for row in columns}
        pk_columns = [row["name"] for row in sorted(columns, key=lambda item: item["pk"]) if row["pk"] > 0]

        if current_columns == target_columns and pk_columns == target_pk:
            return

        legacy_rows = [dict(row) for row in conn.execute("SELECT * FROM scene_controls").fetchall()]
        conn.execute("DROP TABLE IF EXISTS scene_controls_legacy")
        conn.execute("ALTER TABLE scene_controls RENAME TO scene_controls_legacy")

        self._create_scene_controls_table(conn)

        now_iso = datetime.now(timezone.utc).isoformat()
        for row in legacy_rows:
            book_id = str(row.get("book_id") or DEFAULT_BOOK_ID).strip() or DEFAULT_BOOK_ID
            scene_id = str(row.get("scene_id") or "").strip()
            status = str(row.get("status") or "ready").strip() or "ready"
            updated_at = str(row.get("updated_at") or now_iso)
            message = row.get("message")
            if not scene_id:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO scene_controls(book_id, scene_id, status, updated_at, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (book_id, scene_id, status, updated_at, message),
            )

        conn.execute("DROP TABLE scene_controls_legacy")

    @staticmethod
    def _create_scene_controls_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scene_controls (
                book_id TEXT NOT NULL,
                scene_id TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                message TEXT,
                PRIMARY KEY (book_id, scene_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scene_controls_status ON scene_controls(book_id, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scene_controls_updated_at ON scene_controls(book_id, updated_at)"
        )

    @staticmethod
    def _create_books_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS books (
                book_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_books_status ON books(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_books_updated_at ON books(updated_at)")

    @staticmethod
    def _create_book_profiles_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS book_profiles (
                book_id TEXT PRIMARY KEY,
                background TEXT NOT NULL,
                worldview TEXT NOT NULL,
                era_setting TEXT NOT NULL,
                genre TEXT NOT NULL,
                protagonist TEXT NOT NULL,
                protagonist_goal TEXT NOT NULL,
                core_conflict TEXT NOT NULL,
                narrative_style TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_book_profiles_updated_at ON book_profiles(updated_at)"
        )

    @staticmethod
    def _ensure_default_book(conn: sqlite3.Connection) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT OR IGNORE INTO books(book_id, title, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (DEFAULT_BOOK_ID, "Default Book", "active", now, now),
        )

    @staticmethod
    def _create_runtime_settings_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS book_runtime_settings (
                book_id TEXT PRIMARY KEY,
                uncertainty_enabled INTEGER NOT NULL DEFAULT 0,
                decision_timeout_seconds INTEGER NOT NULL DEFAULT 60,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _create_run_sessions_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scene_run_sessions (
                run_id TEXT PRIMARY KEY,
                book_id TEXT NOT NULL,
                scene_id TEXT NOT NULL,
                status TEXT NOT NULL,
                current_turn INTEGER NOT NULL,
                target_turns INTEGER NOT NULL,
                summary_json TEXT NOT NULL,
                last_error TEXT,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scene_run_sessions_book_scene ON scene_run_sessions(book_id, scene_id, updated_at DESC)"
        )

    @staticmethod
    def _create_interventions_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scene_interventions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                book_id TEXT NOT NULL,
                scene_id TEXT NOT NULL,
                turn INTEGER NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scene_interventions_book_scene ON scene_interventions(book_id, scene_id, created_at DESC)"
        )

    @staticmethod
    def _create_decision_requests_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scene_decision_requests (
                request_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                book_id TEXT NOT NULL,
                scene_id TEXT NOT NULL,
                turn INTEGER NOT NULL,
                question TEXT NOT NULL,
                options_json TEXT NOT NULL,
                recommended_option TEXT NOT NULL,
                status TEXT NOT NULL,
                selected_option TEXT,
                selected_source TEXT,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                resolved_at TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scene_decision_book_scene_status ON scene_decision_requests(book_id, scene_id, status, created_at DESC)"
        )

    @staticmethod
    def _create_auto_role_events_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auto_role_generation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id TEXT NOT NULL,
                trigger TEXT NOT NULL,
                scene_id TEXT,
                created_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                created_json TEXT NOT NULL,
                skipped_json TEXT NOT NULL,
                failed_json TEXT NOT NULL,
                duration_ms REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_auto_role_events_book_created ON auto_role_generation_events(book_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_auto_role_events_trigger ON auto_role_generation_events(trigger, created_at DESC)"
        )

    @staticmethod
    def _create_character_state_tables(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_runtime_states (
                book_id TEXT NOT NULL DEFAULT 'default_book',
                agent_id TEXT NOT NULL,
                age INTEGER,
                personality_traits_json TEXT NOT NULL,
                inventory_json TEXT NOT NULL,
                level INTEGER,
                abilities_json TEXT NOT NULL,
                extras_json TEXT NOT NULL,
                updated_turn INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (book_id, agent_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_state_change_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id TEXT NOT NULL DEFAULT 'default_book',
                scene_id TEXT NOT NULL,
                turn INTEGER NOT NULL,
                agent_id TEXT NOT NULL,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL,
                changes_json TEXT NOT NULL,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,
                applied_status TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runtime_states_book_agent ON agent_runtime_states(book_id, agent_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_state_change_events_book_agent ON agent_state_change_events(book_id, agent_id, created_at DESC)"
        )

    def list_books(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    b.book_id,
                    b.title,
                    b.status,
                    b.created_at,
                    b.updated_at,
                    CASE WHEN bp.book_id IS NULL THEN 0 ELSE 1 END AS profile_completed
                FROM books b
                LEFT JOIN book_profiles bp ON bp.book_id = b.book_id
                ORDER BY
                    CASE WHEN b.status = 'active' THEN 0 ELSE 1 END,
                    b.updated_at DESC,
                    b.book_id ASC
                """
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["profile_completed"] = bool(item.get("profile_completed"))
            items.append(item)
        return items

    def get_book(self, book_id: str) -> dict[str, Any] | None:
        normalized_book_id = book_id.strip()
        if not normalized_book_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT book_id, title, status, created_at, updated_at FROM books WHERE book_id = ?",
                (normalized_book_id,),
            ).fetchone()
        return dict(row) if row else None

    def ensure_book(self, book_id: str, title: str | None = None) -> dict[str, Any]:
        normalized_book_id = book_id.strip()
        if not normalized_book_id:
            raise ValueError("book_id cannot be empty")

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT book_id, title, status, created_at, updated_at FROM books WHERE book_id = ?",
                (normalized_book_id,),
            ).fetchone()
            if row:
                existing = dict(row)
                if title and title.strip() and title.strip() != existing["title"]:
                    conn.execute(
                        "UPDATE books SET title = ?, updated_at = ? WHERE book_id = ?",
                        (title.strip(), now, normalized_book_id),
                    )
                refreshed = conn.execute(
                    "SELECT book_id, title, status, created_at, updated_at FROM books WHERE book_id = ?",
                    (normalized_book_id,),
                ).fetchone()
                return dict(refreshed) if refreshed else existing

            resolved_title = title.strip() if title and title.strip() else normalized_book_id
            conn.execute(
                """
                INSERT INTO books(book_id, title, status, created_at, updated_at)
                VALUES (?, ?, 'idle', ?, ?)
                """,
                (normalized_book_id, resolved_title, now, now),
            )
            created = conn.execute(
                "SELECT book_id, title, status, created_at, updated_at FROM books WHERE book_id = ?",
                (normalized_book_id,),
            ).fetchone()
            return dict(created)

    def activate_book(self, book_id: str) -> dict[str, Any]:
        normalized_book_id = book_id.strip()
        if not normalized_book_id:
            raise ValueError("book_id cannot be empty")

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT book_id, title FROM books WHERE book_id = ?",
                (normalized_book_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO books(book_id, title, status, created_at, updated_at)
                    VALUES (?, ?, 'active', ?, ?)
                    """,
                    (normalized_book_id, normalized_book_id, now, now),
                )
            conn.execute(
                "UPDATE books SET status = 'idle', updated_at = ? WHERE book_id <> ? AND status = 'active'",
                (now, normalized_book_id),
            )
            conn.execute(
                "UPDATE books SET status = 'active', updated_at = ? WHERE book_id = ?",
                (now, normalized_book_id),
            )
            result = conn.execute(
                "SELECT book_id, title, status, created_at, updated_at FROM books WHERE book_id = ?",
                (normalized_book_id,),
            ).fetchone()
        if result is None:
            raise ValueError(f"Failed to activate book `{normalized_book_id}`")
        return dict(result)

    def get_book_profile(self, book_id: str) -> dict[str, Any] | None:
        normalized_book_id = book_id.strip()
        if not normalized_book_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    book_id,
                    background,
                    worldview,
                    era_setting,
                    genre,
                    protagonist,
                    protagonist_goal,
                    core_conflict,
                    narrative_style,
                    created_at,
                    updated_at
                FROM book_profiles
                WHERE book_id = ?
                """,
                (normalized_book_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_book_profile_view(self, book_id: str) -> dict[str, Any]:
        book = self.ensure_book(book_id)
        profile = self.get_book_profile(book["book_id"])
        if profile is None:
            return {
                "book_id": book["book_id"],
                "completed": False,
                "created_at": None,
                "updated_at": None,
                **{key: "" for key in BOOK_PROFILE_FIELD_KEYS},
            }
        return {
            **profile,
            "completed": True,
        }

    def upsert_book_profile(self, book_id: str, profile: dict[str, Any]) -> dict[str, Any]:
        book = self.ensure_book(book_id)
        normalized = _normalize_profile_payload(profile, require_complete=True)
        now = datetime.now(timezone.utc).isoformat()
        existing = self.get_book_profile(book["book_id"])
        created_at = existing["created_at"] if existing else now
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO book_profiles(
                    book_id, background, worldview, era_setting, genre, protagonist,
                    protagonist_goal, core_conflict, narrative_style, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(book_id) DO UPDATE SET
                    background = excluded.background,
                    worldview = excluded.worldview,
                    era_setting = excluded.era_setting,
                    genre = excluded.genre,
                    protagonist = excluded.protagonist,
                    protagonist_goal = excluded.protagonist_goal,
                    core_conflict = excluded.core_conflict,
                    narrative_style = excluded.narrative_style,
                    updated_at = excluded.updated_at
                """,
                (
                    book["book_id"],
                    normalized["background"],
                    normalized["worldview"],
                    normalized["era_setting"],
                    normalized["genre"],
                    normalized["protagonist"],
                    normalized["protagonist_goal"],
                    normalized["core_conflict"],
                    normalized["narrative_style"],
                    created_at,
                    now,
                ),
            )
        result = self.get_book_profile(book["book_id"])
        if result is None:
            raise ValueError(f"Failed to save profile for book `{book_id}`")
        return {
            **result,
            "completed": True,
        }

    def patch_book_profile(self, book_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        normalized_updates = _normalize_profile_payload(updates, require_complete=False)
        if not normalized_updates:
            raise ValueError("No profile fields provided")

        existing = self.get_book_profile(book_id)
        if existing is None:
            missing = [key for key in BOOK_PROFILE_FIELD_KEYS if key not in normalized_updates]
            if missing:
                missing_text = ", ".join(missing)
                raise ValueError(
                    f"Profile does not exist yet; complete fields required: {missing_text}"
                )
            return self.upsert_book_profile(book_id, normalized_updates)

        merged = {key: existing[key] for key in BOOK_PROFILE_FIELD_KEYS}
        merged.update(normalized_updates)
        return self.upsert_book_profile(book_id, merged)

    def get_runtime_settings(self, book_id: str) -> dict[str, Any]:
        book = self.ensure_book(book_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO book_runtime_settings(
                    book_id, uncertainty_enabled, decision_timeout_seconds, created_at, updated_at
                ) VALUES (?, 0, 60, ?, ?)
                """,
                (book["book_id"], now, now),
            )
            row = conn.execute(
                """
                SELECT book_id, uncertainty_enabled, decision_timeout_seconds, created_at, updated_at
                FROM book_runtime_settings WHERE book_id = ?
                """,
                (book["book_id"],),
            ).fetchone()
        if row is None:
            raise ValueError(f"Failed to load runtime settings for book `{book_id}`")
        return {
            "book_id": row["book_id"],
            "uncertainty_enabled": bool(row["uncertainty_enabled"]),
            "decision_timeout_seconds": int(row["decision_timeout_seconds"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def update_runtime_settings(
        self,
        book_id: str,
        *,
        uncertainty_enabled: bool | None,
        decision_timeout_seconds: int | None,
    ) -> dict[str, Any]:
        current = self.get_runtime_settings(book_id)
        next_enabled = current["uncertainty_enabled"] if uncertainty_enabled is None else bool(uncertainty_enabled)
        next_timeout = (
            current["decision_timeout_seconds"]
            if decision_timeout_seconds is None
            else int(decision_timeout_seconds)
        )
        next_timeout = max(5, min(next_timeout, 3600))

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE book_runtime_settings
                SET uncertainty_enabled = ?, decision_timeout_seconds = ?, updated_at = ?
                WHERE book_id = ?
                """,
                (1 if next_enabled else 0, next_timeout, now, book_id),
            )

        return self.get_runtime_settings(book_id)

    def get_scene_control(self, book_id: str, scene_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT book_id, scene_id, status, updated_at, message
                FROM scene_controls
                WHERE book_id = ? AND scene_id = ?
                """,
                (book_id, scene_id),
            ).fetchone()
        return dict(row) if row else None

    def upsert_scene_control(
        self,
        book_id: str,
        scene_id: str,
        status: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scene_controls(book_id, scene_id, status, updated_at, message)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(book_id, scene_id) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    message = excluded.message
                """,
                (book_id, scene_id, status, updated_at, message),
            )
        return {
            "book_id": book_id,
            "scene_id": scene_id,
            "status": status,
            "updated_at": updated_at,
            "message": message,
        }

    def list_scene_ids(self, book_id: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT scene_id FROM scene_turn_logs WHERE book_id = ?
                UNION
                SELECT scene_id FROM scene_controls WHERE book_id = ?
                ORDER BY scene_id
                """,
                (book_id, book_id),
            ).fetchall()
        return [str(row["scene_id"]) for row in rows]

    def get_scene_stats(self, book_id: str, scene_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            turn_stats = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_turns,
                    COUNT(DISTINCT actor) AS active_agents,
                    MAX(turn) AS latest_turn,
                    MAX(created_at) AS last_updated
                FROM scene_turn_logs
                WHERE book_id = ? AND scene_id = ?
                """,
                (book_id, scene_id),
            ).fetchone()

            latest_turn_row = conn.execute(
                """
                SELECT actor, action_json, decision_json, state_delta, created_at
                FROM scene_turn_logs
                WHERE book_id = ? AND scene_id = ?
                ORDER BY turn DESC, id DESC
                LIMIT 1
                """,
                (book_id, scene_id),
            ).fetchone()

            latest_snapshot = conn.execute(
                """
                SELECT turn, state_json, created_at
                FROM scene_state_snapshots
                WHERE book_id = ? AND scene_id = ?
                ORDER BY turn DESC, id DESC
                LIMIT 1
                """,
                (book_id, scene_id),
            ).fetchone()

            control = conn.execute(
                """
                SELECT status, updated_at, message
                FROM scene_controls
                WHERE book_id = ? AND scene_id = ?
                """,
                (book_id, scene_id),
            ).fetchone()

        stats = dict(turn_stats) if turn_stats else {}
        total_turns = int(stats.get("total_turns") or 0)
        active_agents = int(stats.get("active_agents") or 0)
        latest_turn = int(stats.get("latest_turn") or 0)
        last_updated = stats.get("last_updated")

        last_actor = None
        last_action = None
        last_goal_progress = None
        last_confidence = None
        if latest_turn_row:
            last_actor = latest_turn_row["actor"]
            action_payload = _load_json(latest_turn_row["action_json"])
            decision_payload = _load_json(latest_turn_row["decision_json"])
            if isinstance(action_payload, dict):
                last_action = action_payload.get("action")
                last_goal_progress = action_payload.get("goal_progress")
            if isinstance(decision_payload, dict):
                confidence = decision_payload.get("confidence")
                try:
                    last_confidence = float(confidence)
                except (TypeError, ValueError):
                    last_confidence = None
            if not last_updated:
                last_updated = latest_turn_row["created_at"]

        objective_achieved = False
        unresolved_conflicts: list[str] = []
        if latest_snapshot:
            snapshot_payload = _load_json(latest_snapshot["state_json"])
            if isinstance(snapshot_payload, dict):
                objective_achieved = _objective_achieved(snapshot_payload)
                unresolved_conflicts = _normalize_conflicts(snapshot_payload.get("unresolved_conflicts"))
            if not last_updated:
                last_updated = latest_snapshot["created_at"]

        control_status = control["status"] if control else "ready"
        control_updated_at = control["updated_at"] if control else None
        control_message = control["message"] if control else None

        return {
            "book_id": book_id,
            "scene_id": scene_id,
            "status": control_status,
            "total_turns": total_turns,
            "active_agents": active_agents,
            "latest_turn": latest_turn,
            "last_updated": last_updated or control_updated_at,
            "objective_achieved": objective_achieved,
            "unresolved_conflicts": unresolved_conflicts,
            "last_actor": last_actor,
            "last_action": last_action,
            "last_goal_progress": last_goal_progress,
            "last_confidence": last_confidence,
            "control_updated_at": control_updated_at,
            "control_message": control_message,
        }

    def list_scene_turns(self, book_id: str, scene_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT turn, actor, action_json, decision_json, state_delta, created_at
                FROM scene_turn_logs
                WHERE book_id = ? AND scene_id = ?
                ORDER BY turn ASC, id ASC
                """,
                (book_id, scene_id),
            ).fetchall()

        return [
            {
                "book_id": book_id,
                "turn": int(row["turn"]),
                "actor": row["actor"],
                "action": _load_json(row["action_json"]),
                "decision": _load_json(row["decision_json"]),
                "state_delta": _load_json(row["state_delta"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_agents_progress(self, book_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            agg_rows = conn.execute(
                """
                SELECT
                    actor AS agent_id,
                    COUNT(*) AS turn_count,
                    MAX(created_at) AS last_active_at
                FROM scene_turn_logs
                WHERE book_id = ?
                GROUP BY actor
                ORDER BY turn_count DESC, agent_id ASC
                """,
                (book_id,),
            ).fetchall()

        results: list[dict[str, Any]] = []
        for row in agg_rows:
            agent_id = str(row["agent_id"])
            latest_log = self._get_latest_action_for_agent(book_id, agent_id)
            memory_summary = self._get_latest_memory_for_agent(book_id, agent_id)
            runtime_summary = self._get_runtime_state_summary(book_id, agent_id)
            results.append(
                {
                    "book_id": book_id,
                    "agent_id": agent_id,
                    "turn_count": int(row["turn_count"] or 0),
                    "last_active_at": row["last_active_at"],
                    "last_action": latest_log.get("action"),
                    "last_speech": latest_log.get("speech"),
                    "last_goal_progress": latest_log.get("goal_progress"),
                    "memory_events": memory_summary.get("event_count", 0),
                    "memory_last_content": memory_summary.get("last_content"),
                    "memory_last_at": memory_summary.get("last_created_at"),
                    "age": runtime_summary.get("age"),
                    "level": runtime_summary.get("level"),
                    "inventory_count": runtime_summary.get("inventory_count", 0),
                    "abilities_count": runtime_summary.get("abilities_count", 0),
                    "last_state_update_at": runtime_summary.get("updated_at"),
                }
            )
        return results

    def get_agent_runtime_state(self, *, book_id: str, agent_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            state_row = conn.execute(
                """
                SELECT
                    book_id,
                    agent_id,
                    age,
                    personality_traits_json,
                    inventory_json,
                    level,
                    abilities_json,
                    extras_json,
                    updated_turn,
                    updated_at
                FROM agent_runtime_states
                WHERE book_id = ? AND agent_id = ?
                """,
                (book_id, agent_id),
            ).fetchone()

        if state_row is None:
            state_payload = {
                "book_id": book_id,
                "agent_id": agent_id,
                "age": None,
                "personality_traits": [],
                "inventory": [],
                "level": None,
                "abilities": [],
                "extras": {},
                "updated_turn": 0,
                "updated_at": None,
            }
        else:
            state_payload = {
                "book_id": state_row["book_id"] or book_id,
                "agent_id": state_row["agent_id"] or agent_id,
                "age": int(state_row["age"]) if state_row["age"] is not None else None,
                "personality_traits": _load_json(state_row["personality_traits_json"]) or [],
                "inventory": _load_json(state_row["inventory_json"]) or [],
                "level": int(state_row["level"]) if state_row["level"] is not None else None,
                "abilities": _load_json(state_row["abilities_json"]) or [],
                "extras": _load_json(state_row["extras_json"]) or {},
                "updated_turn": int(state_row["updated_turn"] or 0),
                "updated_at": state_row["updated_at"],
            }

        state_payload["change_events"] = self.list_agent_state_changes(
            book_id=book_id,
            agent_id=agent_id,
            limit=30,
        )
        return state_payload

    def list_agent_state_changes(
        self,
        *,
        book_id: str,
        agent_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    id,
                    book_id,
                    scene_id,
                    turn,
                    agent_id,
                    confidence,
                    reason,
                    changes_json,
                    before_json,
                    after_json,
                    applied_status,
                    source,
                    created_at
                FROM agent_state_change_events
                WHERE book_id = ? AND agent_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (book_id, agent_id, max(1, int(limit))),
            ).fetchall()

        events: list[dict[str, Any]] = []
        for row in rows:
            events.append(
                {
                    "id": int(row["id"]),
                    "book_id": row["book_id"],
                    "scene_id": row["scene_id"],
                    "turn": int(row["turn"] or 0),
                    "agent_id": row["agent_id"],
                    "confidence": float(row["confidence"] or 0.0),
                    "reason": row["reason"],
                    "changes": _load_json(row["changes_json"]) or {},
                    "before_state": _load_json(row["before_json"]) or {},
                    "after_state": _load_json(row["after_json"]) or {},
                    "applied_status": row["applied_status"],
                    "source": row["source"],
                    "created_at": row["created_at"],
                }
            )
        return events

    def _get_latest_action_for_agent(self, book_id: str, agent_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT action_json
                FROM scene_turn_logs
                WHERE book_id = ? AND actor = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (book_id, agent_id),
            ).fetchone()

        if not row:
            return {}
        action_payload = _load_json(row["action_json"])
        return action_payload if isinstance(action_payload, dict) else {}

    def _get_latest_memory_for_agent(self, book_id: str, agent_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            summary = conn.execute(
                """
                SELECT COUNT(*) AS event_count, MAX(created_at) AS last_created_at
                FROM agent_memory_events
                WHERE book_id = ? AND agent_id = ?
                """,
                (book_id, agent_id),
            ).fetchone()
            latest = conn.execute(
                """
                SELECT content
                FROM agent_memory_events
                WHERE book_id = ? AND agent_id = ?
                ORDER BY turn DESC, id DESC
                LIMIT 1
                """,
                (book_id, agent_id),
            ).fetchone()

        event_count = int(summary["event_count"] or 0) if summary else 0
        return {
            "event_count": event_count,
            "last_created_at": summary["last_created_at"] if summary else None,
            "last_content": latest["content"] if latest else None,
        }

    def _get_runtime_state_summary(self, book_id: str, agent_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT age, level, inventory_json, abilities_json, updated_at
                FROM agent_runtime_states
                WHERE book_id = ? AND agent_id = ?
                """,
                (book_id, agent_id),
            ).fetchone()
        if row is None:
            return {
                "age": None,
                "level": None,
                "inventory_count": 0,
                "abilities_count": 0,
                "updated_at": None,
            }

        inventory = _load_json(row["inventory_json"])
        abilities = _load_json(row["abilities_json"])
        inventory_count = len(inventory) if isinstance(inventory, list) else 0
        abilities_count = len(abilities) if isinstance(abilities, list) else 0
        return {
            "age": int(row["age"]) if row["age"] is not None else None,
            "level": int(row["level"]) if row["level"] is not None else None,
            "inventory_count": inventory_count,
            "abilities_count": abilities_count,
            "updated_at": row["updated_at"],
        }

    def get_kpis(self, book_id: str) -> dict[str, Any]:
        scene_ids = self.list_scene_ids(book_id)
        scene_stats = [self.get_scene_stats(book_id, scene_id) for scene_id in scene_ids]

        total_scenes = len(scene_stats)
        completed_scenes = sum(1 for item in scene_stats if item.get("objective_achieved"))
        completion_rate = (completed_scenes / total_scenes) if total_scenes else 0.0

        with self._connect() as conn:
            turn_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_turns,
                    COUNT(DISTINCT actor) AS active_agents
                FROM scene_turn_logs
                WHERE book_id = ?
                """,
                (book_id,),
            ).fetchone()

        total_turns = int(turn_row["total_turns"] or 0) if turn_row else 0
        active_agents = int(turn_row["active_agents"] or 0) if turn_row else 0
        cost_summary = self._usage_summary(book_id=book_id)

        return {
            "book_id": book_id,
            "total_scenes": total_scenes,
            "completed_scenes": completed_scenes,
            "completion_rate": completion_rate,
            "total_turns": total_turns,
            "active_agents": active_agents,
            "total_cost": cost_summary["total_cost"],
            "total_tokens": cost_summary["total_tokens"],
            "requests": cost_summary["requests"],
        }

    def _usage_summary(self, *, book_id: str | None = None) -> dict[str, Any]:
        where_clause = "WHERE book_id = ?" if book_id else ""
        params: tuple[Any, ...] = (book_id,) if book_id else ()

        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS requests,
                    COALESCE(SUM(total_cost), 0) AS total_cost,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM usage_events
                {where_clause}
                """,
                params,
            ).fetchone()

        return {
            "requests": int(row["requests"] or 0) if row else 0,
            "total_cost": float(row["total_cost"] or 0.0) if row else 0.0,
            "total_tokens": int(row["total_tokens"] or 0) if row else 0,
        }

    def get_costs(
        self,
        *,
        book_id: str,
        scope: Literal["current", "global"],
        start: datetime | None,
        end: datetime | None,
    ) -> dict[str, Any]:
        where_parts: list[str] = []
        params: dict[str, Any] = {}

        if scope == "current":
            where_parts.append("book_id = :book_id")
            params["book_id"] = book_id

        if start is not None:
            where_parts.append("created_at >= :start")
            params["start"] = start.astimezone(timezone.utc).isoformat()
        if end is not None:
            where_parts.append("created_at <= :end")
            params["end"] = end.astimezone(timezone.utc).isoformat()

        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        with self._connect() as conn:
            series_rows = conn.execute(
                f"""
                SELECT
                    substr(created_at, 1, 10) AS day,
                    COALESCE(SUM(total_cost), 0) AS total_cost,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens
                FROM usage_events
                {where_sql}
                GROUP BY day
                ORDER BY day ASC
                """,
                params,
            ).fetchall()

            by_agent_rows = conn.execute(
                f"""
                SELECT
                    agent_id,
                    COUNT(*) AS requests,
                    COALESCE(SUM(total_cost), 0) AS total_cost,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM usage_events
                {where_sql}
                GROUP BY agent_id
                ORDER BY total_cost DESC, agent_id ASC
                """,
                params,
            ).fetchall()

        series = [
            {
                "day": row["day"],
                "total_cost": float(row["total_cost"] or 0.0),
                "total_tokens": int(row["total_tokens"] or 0),
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
            }
            for row in series_rows
        ]

        by_agent = [
            {
                "agent_id": row["agent_id"],
                "requests": int(row["requests"] or 0),
                "total_cost": float(row["total_cost"] or 0.0),
                "total_tokens": int(row["total_tokens"] or 0),
            }
            for row in by_agent_rows
        ]

        return {
            "book_id": book_id,
            "scope": scope,
            "series": series,
            "by_agent": by_agent,
        }

    def create_run_session(
        self,
        *,
        run_id: str,
        book_id: str,
        scene_id: str,
        target_turns: int,
        status: str,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        summary = json.dumps({}, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scene_run_sessions(
                    run_id, book_id, scene_id, status, current_turn, target_turns,
                    summary_json, last_error, started_at, updated_at, finished_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?, NULL, ?, ?, NULL)
                """,
                (run_id, book_id, scene_id, status, int(target_turns), summary, now, now),
            )
        return self.get_run_session(run_id)

    def update_run_session(
        self,
        run_id: str,
        *,
        status: str | None = None,
        current_turn: int | None = None,
        summary: dict[str, Any] | None = None,
        last_error: str | None = None,
        finished: bool = False,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        updates: list[str] = ["updated_at = ?"]
        params: list[Any] = [now]

        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if current_turn is not None:
            updates.append("current_turn = ?")
            params.append(int(current_turn))
        if summary is not None:
            updates.append("summary_json = ?")
            params.append(json.dumps(summary, ensure_ascii=False))
        if last_error is not None:
            updates.append("last_error = ?")
            params.append(last_error)
        if finished:
            updates.append("finished_at = ?")
            params.append(now)

        params.append(run_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE scene_run_sessions SET {', '.join(updates)} WHERE run_id = ?",
                tuple(params),
            )

        return self.get_run_session(run_id)

    def get_run_session(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT run_id, book_id, scene_id, status, current_turn, target_turns,
                       summary_json, last_error, started_at, updated_at, finished_at
                FROM scene_run_sessions
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Run session `{run_id}` not found")
        result = dict(row)
        result["summary"] = _load_json(result.pop("summary_json")) or {}
        result["current_turn"] = int(result["current_turn"] or 0)
        result["target_turns"] = int(result["target_turns"] or 0)
        return result

    def get_latest_run_session(self, book_id: str, scene_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT run_id, book_id, scene_id, status, current_turn, target_turns,
                       summary_json, last_error, started_at, updated_at, finished_at
                FROM scene_run_sessions
                WHERE book_id = ? AND scene_id = ?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (book_id, scene_id),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["summary"] = _load_json(result.pop("summary_json")) or {}
        result["current_turn"] = int(result["current_turn"] or 0)
        result["target_turns"] = int(result["target_turns"] or 0)
        return result

    def add_intervention(
        self,
        *,
        run_id: str,
        book_id: str,
        scene_id: str,
        turn: int,
        kind: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO scene_interventions(
                    run_id, book_id, scene_id, turn, kind, content, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    book_id,
                    scene_id,
                    int(turn),
                    kind,
                    content,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                ),
            )
            intervention_id = int(cursor.lastrowid)

        return {
            "id": intervention_id,
            "run_id": run_id,
            "book_id": book_id,
            "scene_id": scene_id,
            "turn": int(turn),
            "kind": kind,
            "content": content,
            "metadata": metadata or {},
            "created_at": now,
        }

    def list_recent_interventions(self, book_id: str, scene_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, book_id, scene_id, turn, kind, content, metadata_json, created_at
                FROM scene_interventions
                WHERE book_id = ? AND scene_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (book_id, scene_id, max(1, int(limit))),
            ).fetchall()

        items = []
        for row in rows:
            payload = dict(row)
            payload["metadata"] = _load_json(payload.pop("metadata_json")) or {}
            payload["turn"] = int(payload["turn"] or 0)
            items.append(payload)
        return items

    def create_decision_request(
        self,
        *,
        run_id: str,
        book_id: str,
        scene_id: str,
        turn: int,
        question: str,
        options: list[dict[str, Any]],
        recommended_option: str,
        expires_at: datetime,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scene_decision_requests(
                    request_id, run_id, book_id, scene_id, turn, question, options_json,
                    recommended_option, status, selected_option, selected_source,
                    metadata_json, created_at, expires_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, ?, ?, NULL)
                """,
                (
                    request_id,
                    run_id,
                    book_id,
                    scene_id,
                    int(turn),
                    question,
                    json.dumps(options, ensure_ascii=False),
                    recommended_option,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                    expires_at.astimezone(timezone.utc).isoformat(),
                ),
            )

        result = self.get_decision_request(request_id)
        if result is None:
            raise ValueError(f"Failed to create decision request `{request_id}`")
        return result

    def get_pending_decision(self, book_id: str, scene_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT request_id, run_id, book_id, scene_id, turn, question, options_json,
                       recommended_option, status, selected_option, selected_source,
                       metadata_json, created_at, expires_at, resolved_at
                FROM scene_decision_requests
                WHERE book_id = ? AND scene_id = ? AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (book_id, scene_id),
            ).fetchone()
        return _parse_decision_row(row)

    def get_decision_request(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT request_id, run_id, book_id, scene_id, turn, question, options_json,
                       recommended_option, status, selected_option, selected_source,
                       metadata_json, created_at, expires_at, resolved_at
                FROM scene_decision_requests
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        return _parse_decision_row(row)

    def resolve_decision_request(
        self,
        request_id: str,
        *,
        selected_option: str,
        selected_source: str,
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM scene_decision_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            if row["status"] != "pending":
                return self.get_decision_request(request_id)

            conn.execute(
                """
                UPDATE scene_decision_requests
                SET status = 'resolved', selected_option = ?, selected_source = ?, resolved_at = ?
                WHERE request_id = ?
                """,
                (selected_option, selected_source, now, request_id),
            )

        return self.get_decision_request(request_id)

    def add_auto_role_generation_event(
        self,
        *,
        book_id: str,
        trigger: str,
        result: AutoRoleGenerationResult,
        scene_id: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO auto_role_generation_events(
                    book_id,
                    trigger,
                    scene_id,
                    created_count,
                    skipped_count,
                    failed_count,
                    created_json,
                    skipped_json,
                    failed_json,
                    duration_ms,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    book_id,
                    trigger,
                    scene_id,
                    len(result.created),
                    len(result.skipped),
                    len(result.failed),
                    json.dumps(result.created, ensure_ascii=False),
                    json.dumps(result.skipped, ensure_ascii=False),
                    json.dumps(result.failed, ensure_ascii=False),
                    float(result.duration_ms),
                    now,
                ),
            )


@dataclass(slots=True)
class SceneRunController:
    run_id: str
    book_id: str
    scene_id: str
    title: str
    objective: str
    participants: list[str]
    base_context: str
    profile_context: str
    target_turns: int
    state: dict[str, Any]
    history_events: list[str] = field(default_factory=list)
    logs: list[TurnLog] = field(default_factory=list)
    completed_turns: int = 0
    last_actor: str | None = None
    consecutive_turns: int = 0
    pause_requested: bool = False
    revision: int = 0
    global_notes: list[str] = field(default_factory=list)
    pending_decision_id: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    resume_event: threading.Event = field(default_factory=threading.Event)
    stop_event: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.resume_event.set()


class SceneRunManager:
    def __init__(self, *, factory: AgentFactory, repo: DashboardRepository) -> None:
        self._factory = factory
        self._repo = repo
        self._controllers: dict[str, SceneRunController] = {}
        self._controller_threads: dict[str, threading.Thread] = {}
        self._book_run_ids: dict[str, str] = {}
        self._lock = threading.Lock()

    def start_async(self, request: SceneStartRequest) -> dict[str, Any]:
        with self._lock:
            busy_run_id = self._book_run_ids.get(request.book_id)
            if busy_run_id:
                session = self._repo.get_run_session(busy_run_id)
                if session["status"] not in {"completed", "failed"}:
                    raise ValueError(
                        f"Another async run is in progress for book `{request.book_id}`"
                    )

            run_id = str(uuid.uuid4())
            target_turns = max(1, int(request.max_turns or self._factory.config.default_max_turns))
            self._repo.ensure_book(request.book_id)
            profile_context = _format_book_profile_context(
                self._repo.get_book_profile(request.book_id)
            )

            controller = SceneRunController(
                run_id=run_id,
                book_id=request.book_id,
                scene_id=request.scene_id,
                title=request.title,
                objective=request.objective,
                participants=list(request.participants),
                base_context=request.context,
                profile_context=profile_context,
                target_turns=target_turns,
                state=copy.deepcopy(request.state),
            )

            self._controllers[run_id] = controller
            self._book_run_ids[request.book_id] = run_id

            self._repo.create_run_session(
                run_id=run_id,
                book_id=request.book_id,
                scene_id=request.scene_id,
                target_turns=target_turns,
                status="running",
            )
            self._repo.upsert_scene_control(request.book_id, request.scene_id, "running")

            worker = threading.Thread(
                target=self._run_scene_worker,
                args=(controller,),
                daemon=True,
                name=f"scene-run-{request.book_id}-{request.scene_id}",
            )
            self._controller_threads[run_id] = worker
            worker.start()

        return self._repo.get_run_session(run_id)

    def is_book_busy(self, book_id: str) -> bool:
        with self._lock:
            run_id = self._book_run_ids.get(book_id)
            if not run_id:
                return False
            session = self._repo.get_run_session(run_id)
            return session["status"] not in {"completed", "failed"}

    def get_run_status(self, *, book_id: str, scene_id: str) -> dict[str, Any]:
        session = self._repo.get_latest_run_session(book_id, scene_id)
        pending = self._repo.get_pending_decision(book_id, scene_id)
        interventions = self._repo.list_recent_interventions(book_id, scene_id, limit=10)

        if session is None:
            return {
                "book_id": book_id,
                "scene_id": scene_id,
                "status": "idle",
                "run_id": None,
                "current_turn": 0,
                "target_turns": 0,
                "pending_decision": pending,
                "recent_interventions": interventions,
            }

        return {
            **session,
            "pending_decision": pending,
            "recent_interventions": interventions,
        }

    def pause(self, *, book_id: str, scene_id: str, message: str | None = None) -> dict[str, Any] | None:
        controller = self._find_controller(book_id, scene_id)
        if controller is None:
            return None

        with controller.lock:
            controller.pause_requested = True
            controller.resume_event.clear()
            controller.revision += 1
            turn = controller.completed_turns + 1

        self._repo.add_intervention(
            run_id=controller.run_id,
            book_id=book_id,
            scene_id=scene_id,
            turn=turn,
            kind="pause",
            content=message or "manual pause",
        )
        self._repo.update_run_session(controller.run_id, status="paused", current_turn=controller.completed_turns)
        return self._repo.upsert_scene_control(book_id, scene_id, "paused", message=message)

    def resume(self, *, book_id: str, scene_id: str, message: str | None = None) -> dict[str, Any] | None:
        controller = self._find_controller(book_id, scene_id)
        if controller is None:
            return None

        with controller.lock:
            controller.pause_requested = False
            controller.resume_event.set()
            turn = controller.completed_turns + 1

        self._repo.add_intervention(
            run_id=controller.run_id,
            book_id=book_id,
            scene_id=scene_id,
            turn=turn,
            kind="resume",
            content=message or "manual resume",
        )
        self._repo.update_run_session(controller.run_id, status="running", current_turn=controller.completed_turns)
        return self._repo.upsert_scene_control(book_id, scene_id, "running", message=message)

    def interrupt(self, *, book_id: str, scene_id: str, idea: str) -> dict[str, Any]:
        controller = self._find_controller(book_id, scene_id)
        if controller is None:
            raise ValueError("No running scene session found for interrupt")

        with controller.lock:
            controller.revision += 1
            controller.global_notes.append(idea.strip())
            turn = controller.completed_turns + 1
            pending_decision_id = controller.pending_decision_id

        if pending_decision_id:
            decision = self._repo.get_decision_request(pending_decision_id)
            if decision and decision["status"] == "pending":
                self._repo.resolve_decision_request(
                    pending_decision_id,
                    selected_option=decision["recommended_option"],
                    selected_source="system_interrupt",
                )
            with controller.lock:
                if controller.pending_decision_id == pending_decision_id:
                    controller.pending_decision_id = None

        self._repo.add_intervention(
            run_id=controller.run_id,
            book_id=book_id,
            scene_id=scene_id,
            turn=turn,
            kind="interrupt",
            content=idea.strip(),
        )
        self._factory.memory_store.append(
            MemoryEvent(
                book_id=book_id,
                agent_id="director_global",
                scene_id=scene_id,
                turn=turn,
                content=f"创作者干预: {idea.strip()}",
                importance=0.95,
                tags=["creator-interrupt", "global-note"],
            )
        )

        return self.get_run_status(book_id=book_id, scene_id=scene_id)

    def select_decision(
        self,
        *,
        book_id: str,
        scene_id: str,
        request_id: str,
        selected_option: str,
    ) -> dict[str, Any]:
        request = self._repo.get_decision_request(request_id)
        if request is None:
            raise ValueError(f"Decision request `{request_id}` not found")
        if request["book_id"] != book_id or request["scene_id"] != scene_id:
            raise ValueError("Decision request does not match target book/scene")
        if request["status"] != "pending":
            return request

        available_ids = {str(item.get("id")) for item in request["options"] if isinstance(item, dict)}
        if selected_option not in available_ids:
            raise ValueError(f"Selected option `{selected_option}` is invalid")

        updated = self._repo.resolve_decision_request(
            request_id,
            selected_option=selected_option,
            selected_source="user",
        )
        if updated is None:
            raise ValueError(f"Decision request `{request_id}` not found")

        self._repo.add_intervention(
            run_id=updated["run_id"],
            book_id=book_id,
            scene_id=scene_id,
            turn=int(updated["turn"]),
            kind="decision_selected",
            content=selected_option,
            metadata={"request_id": request_id},
        )
        return updated

    def shutdown(self) -> None:
        with self._lock:
            controllers = list(self._controllers.values())
            threads = list(self._controller_threads.values())

        for controller in controllers:
            controller.stop_event.set()
            controller.resume_event.set()

        for thread in threads:
            thread.join(timeout=1.5)

    def _find_controller(self, book_id: str, scene_id: str) -> SceneRunController | None:
        with self._lock:
            run_id = self._book_run_ids.get(book_id)
            if not run_id:
                return None
            controller = self._controllers.get(run_id)
            if controller is None:
                return None
            if controller.scene_id != scene_id:
                return None
            return controller

    def _run_scene_worker(self, controller: SceneRunController) -> None:
        final_status = "failed"
        final_error: str | None = None
        final_summary: dict[str, Any] = {}

        try:
            agents = self._factory.create_agents_for_book(controller.book_id)
            orchestrator = self._factory.create_orchestrator()

            missing_participants = [
                agent_id for agent_id in controller.participants if agent_id not in agents
            ]
            if missing_participants:
                raise ValueError(f"Participants missing in agents: {missing_participants}")

            state = copy.deepcopy(controller.state)
            unresolved_conflicts = list(_normalize_conflicts(state.get("unresolved_conflicts")))
            if "unresolved_conflicts" not in state:
                state["unresolved_conflicts"] = unresolved_conflicts

            while controller.completed_turns < controller.target_turns:
                if controller.stop_event.is_set():
                    raise RuntimeError("Run stopped by shutdown")

                self._wait_if_paused(controller)
                if controller.stop_event.is_set():
                    raise RuntimeError("Run stopped by shutdown")

                turn = controller.completed_turns + 1
                with controller.lock:
                    revision_snapshot = controller.revision
                    context_text = _compose_scene_context(
                        controller.base_context,
                        controller.global_notes,
                        profile_context=controller.profile_context,
                    )

                scene_input = SceneInput(
                    book_id=controller.book_id,
                    scene_id=controller.scene_id,
                    title=controller.title,
                    objective=controller.objective,
                    participants=list(controller.participants),
                    context=context_text,
                    state=copy.deepcopy(state),
                    recent_events=controller.history_events[-8:],
                    unresolved_conflicts=list(state.get("unresolved_conflicts", [])),
                    max_turns=controller.target_turns,
                )

                actor_id, score = orchestrator._select_actor(  # noqa: SLF001
                    scene_input=scene_input,
                    agents=agents,
                    state=state,
                    last_actor=controller.last_actor,
                    consecutive_turns=controller.consecutive_turns,
                )

                memory_slice = self._factory.memory_store.retrieve(
                    agent_id=actor_id,
                    scene_id=controller.scene_id,
                    top_k=getattr(self._factory.config.memory, "top_k", 5),
                    book_id=controller.book_id,
                )

                action = agents[actor_id].next_action(scene_input, memory_slice)
                decision = orchestrator._director_decide(  # noqa: SLF001
                    scene_input=scene_input,
                    proposed_action=action,
                    logs=controller.logs[-4:],
                )

                with controller.lock:
                    changed = revision_snapshot != controller.revision
                if changed:
                    self._repo.add_intervention(
                        run_id=controller.run_id,
                        book_id=controller.book_id,
                        scene_id=controller.scene_id,
                        turn=turn,
                        kind="rerun",
                        content="intervention changed current turn, rerun from same turn",
                    )
                    continue

                settings = self._repo.get_runtime_settings(controller.book_id)
                if settings["uncertainty_enabled"] and _should_request_user_decision(decision):
                    decision = self._resolve_uncertain_turn(
                        controller=controller,
                        turn=turn,
                        revision_snapshot=revision_snapshot,
                        proposed_action=action,
                        director_decision=decision,
                        timeout_seconds=int(settings["decision_timeout_seconds"]),
                    )
                    if decision is None:
                        continue

                state_update_events = self._process_character_updates(
                    controller=controller,
                    orchestrator=orchestrator,
                    decision=decision,
                    agents=agents,
                    turn=turn,
                    settings=settings,
                    revision_snapshot=revision_snapshot,
                )
                if state_update_events is None:
                    continue

                state = _deep_merge_dicts(state, decision.state_delta)
                state["unresolved_conflicts"] = _update_unresolved_conflicts(
                    existing=list(state.get("unresolved_conflicts", [])),
                    decision=decision,
                )

                turn_log = TurnLog(
                    book_id=controller.book_id,
                    scene_id=controller.scene_id,
                    turn=turn,
                    actor=actor_id,
                    score=score,
                    action=action,
                    decision=decision,
                    state_after=copy.deepcopy(state),
                )
                controller.logs.append(turn_log)

                orchestrator._persist_turn(controller.book_id, controller.scene_id, turn_log)  # noqa: SLF001
                orchestrator._persist_memories(controller.book_id, controller.scene_id, turn_log, agents)  # noqa: SLF001

                controller.history_events.append(
                    f"Turn {turn} {actor_id}: {decision.resolved_action.action} / {decision.resolved_action.speech}"
                )
                if state_update_events:
                    controller.history_events.append(
                        f"Turn {turn} state_updates: {len(state_update_events)} event(s)"
                    )

                if controller.last_actor == actor_id:
                    controller.consecutive_turns += 1
                else:
                    controller.consecutive_turns = 1
                controller.last_actor = actor_id
                controller.completed_turns = turn

                self._repo.update_run_session(
                    controller.run_id,
                    status="running",
                    current_turn=controller.completed_turns,
                )
                self._repo.upsert_scene_control(controller.book_id, controller.scene_id, "running")

                if _objective_achieved(state):
                    final_status = "completed"
                    final_summary = {
                        "status": "objective_achieved",
                        "turns": controller.completed_turns,
                        "final_state": state,
                        "stop_reason": "state indicates objective achieved",
                    }
                    self._repo.upsert_scene_control(controller.book_id, controller.scene_id, "completed")
                    break
            else:
                final_status = "completed"
                final_summary = {
                    "status": "max_turns_reached",
                    "turns": controller.completed_turns,
                    "final_state": state,
                    "stop_reason": "reached turn limit",
                }
                self._repo.upsert_scene_control(controller.book_id, controller.scene_id, "ready")

            if not final_summary:
                final_summary = {
                    "status": "failed",
                    "turns": controller.completed_turns,
                    "final_state": state,
                    "stop_reason": "unexpected termination",
                }
        except Exception as exc:  # pragma: no cover - integration behavior
            final_status = "failed"
            final_error = str(exc)
            final_summary = {
                "status": "failed",
                "turns": controller.completed_turns,
                "final_state": controller.state,
                "stop_reason": str(exc),
            }
            self._repo.upsert_scene_control(controller.book_id, controller.scene_id, "failed", message=str(exc))
        finally:
            self._repo.update_run_session(
                controller.run_id,
                status=final_status,
                current_turn=controller.completed_turns,
                summary=final_summary,
                last_error=final_error,
                finished=True,
            )

            with self._lock:
                self._controllers.pop(controller.run_id, None)
                self._controller_threads.pop(controller.run_id, None)
                existing = self._book_run_ids.get(controller.book_id)
                if existing == controller.run_id:
                    self._book_run_ids.pop(controller.book_id, None)

    def _wait_if_paused(self, controller: SceneRunController) -> None:
        while True:
            with controller.lock:
                paused = controller.pause_requested
            if not paused:
                return

            self._repo.update_run_session(
                controller.run_id,
                status="paused",
                current_turn=controller.completed_turns,
            )
            self._repo.upsert_scene_control(controller.book_id, controller.scene_id, "paused")
            controller.resume_event.wait(timeout=0.3)
            if controller.stop_event.is_set():
                return

    def _resolve_uncertain_turn(
        self,
        *,
        controller: SceneRunController,
        turn: int,
        revision_snapshot: int,
        proposed_action: AgentAction,
        director_decision: DirectorDecision,
        timeout_seconds: int,
    ) -> DirectorDecision | None:
        options, recommended_option = _build_decision_options(director_decision)
        question = "导演对该回合裁决存在不确定性，请选择处理方案。"
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(5, timeout_seconds))
        request = self._repo.create_decision_request(
            run_id=controller.run_id,
            book_id=controller.book_id,
            scene_id=controller.scene_id,
            turn=turn,
            question=question,
            options=options,
            recommended_option=recommended_option,
            expires_at=expires_at,
            metadata={
                "decision_type": "action_resolution",
                "director_conflict": director_decision.conflict,
                "director_confidence": director_decision.confidence,
            },
        )

        with controller.lock:
            controller.pending_decision_id = request["request_id"]

        self._repo.update_run_session(
            controller.run_id,
            status="waiting_user",
            current_turn=controller.completed_turns,
        )
        self._repo.upsert_scene_control(
            controller.book_id,
            controller.scene_id,
            "waiting_user",
            message="awaiting creator decision",
        )

        started = time.time()
        while True:
            if controller.stop_event.is_set():
                self._cleanup_pending_decision(
                    controller=controller,
                    request_id=request["request_id"],
                    turn=turn,
                    recommended_option=recommended_option,
                    selected_source="system_stop",
                    intervention_kind="decision_aborted",
                    metadata={"reason": "worker_stopped"},
                )
                return None

            with controller.lock:
                changed = controller.revision != revision_snapshot
                current_pending = controller.pending_decision_id
            if changed:
                self._cleanup_pending_decision(
                    controller=controller,
                    request_id=request["request_id"],
                    turn=turn,
                    recommended_option=recommended_option,
                    selected_source="system_revision",
                    intervention_kind="decision_aborted",
                    metadata={"reason": "revision_changed"},
                )
                return None
            if current_pending != request["request_id"]:
                self._cleanup_pending_decision(
                    controller=controller,
                    request_id=request["request_id"],
                    turn=turn,
                    recommended_option=recommended_option,
                    selected_source="system_superseded",
                    intervention_kind="decision_aborted",
                    metadata={"reason": "pending_decision_replaced"},
                )
                return None

            decision_row = self._repo.get_decision_request(request["request_id"])
            if decision_row is None:
                return None

            if decision_row["status"] != "pending":
                selected = decision_row["selected_option"] or recommended_option
                selected_source = decision_row["selected_source"] or "user"
                with controller.lock:
                    controller.pending_decision_id = None
                self._repo.update_run_session(
                    controller.run_id,
                    status="running",
                    current_turn=controller.completed_turns,
                )
                self._repo.upsert_scene_control(controller.book_id, controller.scene_id, "running")
                return _apply_user_decision_choice(
                    selected,
                    proposed_action=proposed_action,
                    director_decision=director_decision,
                    source=selected_source,
                )

            elapsed = time.time() - started
            if elapsed >= timeout_seconds:
                auto = self._repo.resolve_decision_request(
                    request["request_id"],
                    selected_option=recommended_option,
                    selected_source="timeout_auto",
                )
                if auto is None:
                    return None
                with controller.lock:
                    controller.pending_decision_id = None
                self._repo.add_intervention(
                    run_id=controller.run_id,
                    book_id=controller.book_id,
                    scene_id=controller.scene_id,
                    turn=turn,
                    kind="decision_timeout_auto",
                    content=recommended_option,
                    metadata={"request_id": request["request_id"]},
                )
                self._repo.update_run_session(
                    controller.run_id,
                    status="running",
                    current_turn=controller.completed_turns,
                )
                self._repo.upsert_scene_control(controller.book_id, controller.scene_id, "running")
                return _apply_user_decision_choice(
                    recommended_option,
                    proposed_action=proposed_action,
                    director_decision=director_decision,
                    source="timeout_auto",
                )

            time.sleep(0.25)

    def _process_character_updates(
        self,
        *,
        controller: SceneRunController,
        orchestrator: Any,
        decision: DirectorDecision,
        agents: dict[str, Any],
        turn: int,
        settings: dict[str, Any],
        revision_snapshot: int,
    ) -> list[dict[str, Any]] | None:
        cfg = self._factory.config.character_state
        if not bool(getattr(cfg, "enabled", True)):
            return []

        max_updates = max(1, int(getattr(cfg, "max_updates_per_turn", 8)))
        threshold = max(0.0, min(1.0, float(getattr(cfg, "auto_apply_confidence", 0.75))))
        uncertainty_enabled = bool(settings.get("uncertainty_enabled"))
        events: list[dict[str, Any]] = []

        for update in decision.character_updates[:max_updates]:
            if update.target not in agents:
                event = orchestrator.apply_character_update_event(
                    book_id=controller.book_id,
                    scene_id=controller.scene_id,
                    turn=turn,
                    update=update,
                    applied_status="invalid_target",
                    source="director_async",
                    apply=False,
                )
                events.append(event)
                continue

            if float(update.confidence) >= threshold:
                event = orchestrator.apply_character_update_event(
                    book_id=controller.book_id,
                    scene_id=controller.scene_id,
                    turn=turn,
                    update=update,
                    applied_status="applied_auto",
                    source="director_async",
                    apply=True,
                )
                events.append(event)
                continue

            if not uncertainty_enabled:
                event = orchestrator.apply_character_update_event(
                    book_id=controller.book_id,
                    scene_id=controller.scene_id,
                    turn=turn,
                    update=update,
                    applied_status="skipped_low_confidence",
                    source="director_async",
                    apply=False,
                )
                events.append(event)
                continue

            decision_choice = self._resolve_uncertain_state_update(
                controller=controller,
                turn=turn,
                revision_snapshot=revision_snapshot,
                update=update,
                timeout_seconds=int(settings.get("decision_timeout_seconds") or 60),
                threshold=threshold,
            )
            if decision_choice is None:
                return None

            selected_option, selected_source = decision_choice
            apply = selected_option == "state_update_apply"
            applied_status = (
                "applied_user" if apply and selected_source == "user"
                else "applied_timeout_auto" if apply
                else "discarded_user" if selected_source == "user"
                else "discarded_timeout_auto"
            )
            event = orchestrator.apply_character_update_event(
                book_id=controller.book_id,
                scene_id=controller.scene_id,
                turn=turn,
                update=update,
                applied_status=applied_status,
                source=f"director_async:{selected_source}",
                apply=apply,
            )
            events.append(event)

        return events

    def _resolve_uncertain_state_update(
        self,
        *,
        controller: SceneRunController,
        turn: int,
        revision_snapshot: int,
        update: CharacterStateUpdate,
        timeout_seconds: int,
        threshold: float,
    ) -> tuple[str, str] | None:
        options, recommended_option = _build_state_update_decision_options(update, threshold=threshold)
        question = (
            f"导演建议更新角色 `{update.target}` 的动态状态，置信度 {update.confidence:.2f}。"
            "是否应用该变更？"
        )
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(5, timeout_seconds))
        request = self._repo.create_decision_request(
            run_id=controller.run_id,
            book_id=controller.book_id,
            scene_id=controller.scene_id,
            turn=turn,
            question=question,
            options=options,
            recommended_option=recommended_option,
            expires_at=expires_at,
            metadata={
                "decision_type": "character_state_update",
                "target": update.target,
                "confidence": update.confidence,
                "changes": update.changes,
                "reason": update.reason,
            },
        )

        with controller.lock:
            controller.pending_decision_id = request["request_id"]

        self._repo.update_run_session(
            controller.run_id,
            status="waiting_user",
            current_turn=controller.completed_turns,
        )
        self._repo.upsert_scene_control(
            controller.book_id,
            controller.scene_id,
            "waiting_user",
            message="awaiting creator decision for state update",
        )

        started = time.time()
        while True:
            if controller.stop_event.is_set():
                self._cleanup_pending_decision(
                    controller=controller,
                    request_id=request["request_id"],
                    turn=turn,
                    recommended_option=recommended_option,
                    selected_source="system_stop",
                    intervention_kind="state_update_aborted",
                    metadata={"reason": "worker_stopped", "target": update.target},
                )
                return None

            with controller.lock:
                changed = controller.revision != revision_snapshot
                current_pending = controller.pending_decision_id
            if changed:
                self._cleanup_pending_decision(
                    controller=controller,
                    request_id=request["request_id"],
                    turn=turn,
                    recommended_option=recommended_option,
                    selected_source="system_revision",
                    intervention_kind="state_update_aborted",
                    metadata={"reason": "revision_changed", "target": update.target},
                )
                return None
            if current_pending != request["request_id"]:
                self._cleanup_pending_decision(
                    controller=controller,
                    request_id=request["request_id"],
                    turn=turn,
                    recommended_option=recommended_option,
                    selected_source="system_superseded",
                    intervention_kind="state_update_aborted",
                    metadata={"reason": "pending_decision_replaced", "target": update.target},
                )
                return None

            decision_row = self._repo.get_decision_request(request["request_id"])
            if decision_row is None:
                return None

            if decision_row["status"] != "pending":
                selected = decision_row["selected_option"] or recommended_option
                selected_source = decision_row["selected_source"] or "user"
                with controller.lock:
                    controller.pending_decision_id = None
                self._repo.update_run_session(
                    controller.run_id,
                    status="running",
                    current_turn=controller.completed_turns,
                )
                self._repo.upsert_scene_control(controller.book_id, controller.scene_id, "running")
                return selected, selected_source

            elapsed = time.time() - started
            if elapsed >= timeout_seconds:
                auto = self._repo.resolve_decision_request(
                    request["request_id"],
                    selected_option=recommended_option,
                    selected_source="timeout_auto",
                )
                if auto is None:
                    return None
                with controller.lock:
                    controller.pending_decision_id = None
                self._repo.add_intervention(
                    run_id=controller.run_id,
                    book_id=controller.book_id,
                    scene_id=controller.scene_id,
                    turn=turn,
                    kind="state_update_timeout_auto",
                    content=recommended_option,
                    metadata={"request_id": request["request_id"], "target": update.target},
                )
                self._repo.update_run_session(
                    controller.run_id,
                    status="running",
                    current_turn=controller.completed_turns,
                )
                self._repo.upsert_scene_control(controller.book_id, controller.scene_id, "running")
                return recommended_option, "timeout_auto"

            time.sleep(0.25)

    def _cleanup_pending_decision(
        self,
        *,
        controller: SceneRunController,
        request_id: str,
        turn: int,
        recommended_option: str,
        selected_source: str,
        intervention_kind: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        request = self._repo.get_decision_request(request_id)
        if request and request.get("status") == "pending":
            self._repo.resolve_decision_request(
                request_id,
                selected_option=recommended_option,
                selected_source=selected_source,
            )
            self._repo.add_intervention(
                run_id=controller.run_id,
                book_id=controller.book_id,
                scene_id=controller.scene_id,
                turn=turn,
                kind=intervention_kind,
                content=recommended_option,
                metadata={"request_id": request_id, **(metadata or {})},
            )
        with controller.lock:
            if controller.pending_decision_id == request_id:
                controller.pending_decision_id = None


def create_dashboard_app(
    *,
    urls_config_path: str = DEFAULT_URLS_CONFIG,
    runtime_config_path: str = DEFAULT_RUNTIME_CONFIG,
    factory_config_path: str = DEFAULT_FACTORY_CONFIG,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    factory = AgentFactory.from_yaml(
        urls_config_path=urls_config_path,
        runtime_config_path=runtime_config_path,
        factory_config_path=factory_config_path,
        transport=transport,
    )

    repo = DashboardRepository(factory.memory_store.sqlite_path)
    run_locks: dict[str, threading.Lock] = {}
    run_locks_guard = threading.Lock()
    run_manager = SceneRunManager(factory=factory, repo=repo)
    auto_character_service = AutoCharacterService(factory)

    def _get_book_lock(book_id: str) -> threading.Lock:
        with run_locks_guard:
            existing = run_locks.get(book_id)
            if existing is not None:
                return existing
            created = threading.Lock()
            run_locks[book_id] = created
            return created

    def _run_auto_role_generation(
        *,
        book_id: str,
        trigger: Literal["profile_save", "scene_start"],
        scene_input: dict[str, Any] | None = None,
    ) -> AutoRoleGenerationResult | None:
        config = factory.config.auto_character
        if not config.enabled:
            return None
        if trigger == "profile_save" and not config.trigger_on_profile_save:
            return None
        if trigger == "scene_start" and not config.trigger_on_scene_start:
            return None

        profile = repo.get_book_profile(book_id)
        result = auto_character_service.generate(
            book_id=book_id,
            profile=profile,
            trigger=trigger,
            scene_input=scene_input,
        )
        repo.add_auto_role_generation_event(
            book_id=book_id,
            trigger=trigger,
            scene_id=str(scene_input.get("scene_id")).strip()
            if isinstance(scene_input, dict) and str(scene_input.get("scene_id", "")).strip()
            else None,
            result=result,
        )
        return result

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            run_manager.shutdown()
            await factory.aclose()

    app = FastAPI(title="Living Novel Dashboard API", version="0.3.0", lifespan=lifespan)
    app.state.factory = factory
    app.state.repo = repo
    app.state.run_locks = run_locks
    app.state.run_locks_guard = run_locks_guard
    app.state.run_manager = run_manager

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/books")
    def get_books() -> dict[str, Any]:
        return {"items": repo.list_books()}

    @app.post("/api/books")
    def create_book(request: BookCreateRequest) -> dict[str, Any]:
        existing = repo.get_book(request.book_id)
        is_new_book = existing is None
        if is_new_book and request.profile is None:
            raise HTTPException(
                status_code=422,
                detail="New book creation requires `profile` with 8 required fields",
            )

        auto_roles: dict[str, Any] | None = None
        try:
            book = repo.ensure_book(request.book_id, request.title)
            if request.profile is not None:
                repo.upsert_book_profile(book["book_id"], request.profile.model_dump())
                auto_result = _run_auto_role_generation(
                    book_id=book["book_id"],
                    trigger="profile_save",
                )
                if auto_result is not None:
                    auto_roles = auto_result.to_dict()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        response = {
            **book,
            "profile_completed": repo.get_book_profile(book["book_id"]) is not None,
        }
        if auto_roles is not None:
            response["auto_roles"] = auto_roles
        return response

    @app.post("/api/books/{book_id}/activate")
    def activate_book(book_id: str) -> dict[str, Any]:
        return repo.activate_book(book_id)

    @app.get("/api/books/{book_id}/profile")
    def get_book_profile(book_id: str) -> dict[str, Any]:
        return repo.get_book_profile_view(book_id)

    @app.patch("/api/books/{book_id}/profile")
    def patch_book_profile(book_id: str, body: BookProfilePatchRequest) -> dict[str, Any]:
        updates = body.model_dump(exclude_none=True)
        try:
            updated = repo.patch_book_profile(book_id, updates)
            auto_result = _run_auto_role_generation(
                book_id=updated["book_id"],
                trigger="profile_save",
            )
            if auto_result is not None:
                updated["auto_roles"] = auto_result.to_dict()
            return updated
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/books/{book_id}/interactive-settings")
    def get_interactive_settings(book_id: str) -> dict[str, Any]:
        return repo.get_runtime_settings(book_id)

    @app.patch("/api/books/{book_id}/interactive-settings")
    def patch_interactive_settings(book_id: str, body: InteractiveSettingsPatchRequest) -> dict[str, Any]:
        return repo.update_runtime_settings(
            book_id,
            uncertainty_enabled=body.uncertainty_enabled,
            decision_timeout_seconds=body.decision_timeout_seconds,
        )

    @app.get("/api/dashboard/kpis")
    def get_kpis(
        book_id: str = Query(default=DEFAULT_BOOK_ID, min_length=1),
    ) -> dict[str, Any]:
        return repo.get_kpis(book_id)

    @app.get("/api/dashboard/scenes")
    def get_scenes(
        book_id: str = Query(default=DEFAULT_BOOK_ID, min_length=1),
    ) -> dict[str, Any]:
        scene_ids = repo.list_scene_ids(book_id)
        scenes = [repo.get_scene_stats(book_id, scene_id) for scene_id in scene_ids]
        scenes.sort(key=lambda item: item.get("last_updated") or "", reverse=True)
        return {"book_id": book_id, "items": scenes}

    @app.get("/api/dashboard/scenes/{scene_id}/turns")
    def get_scene_turns(
        scene_id: str,
        book_id: str = Query(default=DEFAULT_BOOK_ID, min_length=1),
    ) -> dict[str, Any]:
        return {
            "book_id": book_id,
            "scene_id": scene_id,
            "items": repo.list_scene_turns(book_id, scene_id),
        }

    @app.get("/api/dashboard/agents")
    def get_agents(
        book_id: str = Query(default=DEFAULT_BOOK_ID, min_length=1),
    ) -> dict[str, Any]:
        return {"book_id": book_id, "items": repo.list_agents_progress(book_id)}

    @app.get("/api/dashboard/agents/{agent_id}/state")
    def get_agent_runtime_state(
        agent_id: str,
        book_id: str = Query(default=DEFAULT_BOOK_ID, min_length=1),
    ) -> dict[str, Any]:
        return repo.get_agent_runtime_state(book_id=book_id, agent_id=agent_id)

    @app.get("/api/dashboard/costs")
    def get_costs(
        book_id: str = Query(default=DEFAULT_BOOK_ID, min_length=1),
        scope: Literal["current", "global"] = Query(default="current"),
        from_ts: str | None = Query(default=None, alias="from"),
        to_ts: str | None = Query(default=None, alias="to"),
    ) -> dict[str, Any]:
        start = _parse_iso_datetime(from_ts) if from_ts else None
        end = _parse_iso_datetime(to_ts) if to_ts else None
        return repo.get_costs(book_id=book_id, scope=scope, start=start, end=end)

    @app.get("/api/control/scenes/{scene_id}/run")
    def get_scene_run_status(
        scene_id: str,
        book_id: str = Query(default=DEFAULT_BOOK_ID, min_length=1),
    ) -> dict[str, Any]:
        return run_manager.get_run_status(book_id=book_id, scene_id=scene_id)

    @app.post("/api/control/scenes/start_async")
    def start_scene_async(request: SceneStartRequest) -> dict[str, Any]:
        control = repo.get_scene_control(request.book_id, request.scene_id)
        if control and control.get("status") == "paused":
            raise HTTPException(status_code=409, detail="Scene is paused. Resume before start.")
        if run_manager.is_book_busy(request.book_id):
            raise HTTPException(
                status_code=409,
                detail=f"Another async run is in progress for book `{request.book_id}`",
            )

        auto_result = _run_auto_role_generation(
            book_id=request.book_id,
            trigger="scene_start",
            scene_input={
                "scene_id": request.scene_id,
                "title": request.title,
                "objective": request.objective,
                "context": request.context,
                "participants": list(request.participants),
            },
        )
        try:
            participants = _resolve_participants(factory, request.book_id, request.participants)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        normalized_request = request.model_copy(update={"participants": participants})
        try:
            session = run_manager.start_async(normalized_request)
            response = {
                "book_id": request.book_id,
                "scene_id": request.scene_id,
                "run_id": session["run_id"],
                "status": session["status"],
                "current_turn": session["current_turn"],
                "target_turns": session["target_turns"],
            }
            if auto_result is not None:
                response["auto_roles"] = auto_result.to_dict()
            return response
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/control/scenes/start")
    def start_scene(request: SceneStartRequest) -> dict[str, Any]:
        if run_manager.is_book_busy(request.book_id):
            raise HTTPException(
                status_code=409,
                detail=f"Async run is in progress for book `{request.book_id}`",
            )

        control = repo.get_scene_control(request.book_id, request.scene_id)
        if control and control.get("status") == "paused":
            raise HTTPException(status_code=409, detail="Scene is paused. Resume before start.")

        book_lock = _get_book_lock(request.book_id)
        if not book_lock.acquire(blocking=False):
            raise HTTPException(
                status_code=409,
                detail=f"Another scene run is in progress for book `{request.book_id}`",
            )

        repo.ensure_book(request.book_id)
        profile_context = _format_book_profile_context(repo.get_book_profile(request.book_id))
        merged_context = _compose_scene_context(
            request.context,
            [],
            profile_context=profile_context,
        )
        try:
            auto_result = _run_auto_role_generation(
                book_id=request.book_id,
                trigger="scene_start",
                scene_input={
                    "scene_id": request.scene_id,
                    "title": request.title,
                    "objective": request.objective,
                    "context": request.context,
                    "participants": list(request.participants),
                },
            )
            participants = _resolve_participants(factory, request.book_id, request.participants)
            repo.upsert_scene_control(request.book_id, request.scene_id, "running")
            agents = factory.create_agents_for_book(request.book_id)
            scene = SceneInput(
                book_id=request.book_id,
                scene_id=request.scene_id,
                title=request.title,
                objective=request.objective,
                participants=participants,
                context=merged_context,
                state=request.state,
                max_turns=request.max_turns,
            )
            orchestrator = factory.create_orchestrator()
            result = orchestrator.run_scene(scene, agents, max_turns=request.max_turns)

            next_status = "completed" if result.status == "objective_achieved" else "ready"
            repo.upsert_scene_control(request.book_id, request.scene_id, next_status)

            response = {
                "book_id": request.book_id,
                "scene_id": result.scene_id,
                "status": result.status,
                "turns": result.turns,
                "final_state": result.final_state,
                "stop_reason": result.stop_reason,
            }
            if auto_result is not None:
                response["auto_roles"] = auto_result.to_dict()
            return response
        except ValueError as exc:
            repo.upsert_scene_control(request.book_id, request.scene_id, "ready", message=str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover - exercised by API layer tests
            repo.upsert_scene_control(request.book_id, request.scene_id, "failed", message=str(exc))
            raise HTTPException(status_code=500, detail=f"Scene start failed: {exc}") from exc
        finally:
            book_lock.release()

    @app.post("/api/control/scenes/{scene_id}/interrupt")
    def interrupt_scene(scene_id: str, body: SceneInterruptRequest) -> dict[str, Any]:
        try:
            return run_manager.interrupt(book_id=body.book_id, scene_id=scene_id, idea=body.idea)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/control/scenes/{scene_id}/decisions/pending")
    def get_pending_decision(
        scene_id: str,
        book_id: str = Query(default=DEFAULT_BOOK_ID, min_length=1),
    ) -> dict[str, Any]:
        pending = repo.get_pending_decision(book_id, scene_id)
        return {
            "book_id": book_id,
            "scene_id": scene_id,
            "item": pending,
        }

    @app.post("/api/control/scenes/{scene_id}/decisions/{request_id}/select")
    def select_decision(scene_id: str, request_id: str, body: DecisionSelectRequest) -> dict[str, Any]:
        try:
            item = run_manager.select_decision(
                book_id=body.book_id,
                scene_id=scene_id,
                request_id=request_id,
                selected_option=body.selected_option,
            )
            return item
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/control/scenes/{scene_id}/pause")
    def pause_scene(scene_id: str, body: SceneControlRequest) -> dict[str, Any]:
        managed = run_manager.pause(book_id=body.book_id, scene_id=scene_id, message=body.message)
        if managed is not None:
            return managed
        return repo.upsert_scene_control(body.book_id, scene_id, "paused", message=body.message)

    @app.post("/api/control/scenes/{scene_id}/resume")
    def resume_scene(scene_id: str, body: SceneControlRequest) -> dict[str, Any]:
        managed = run_manager.resume(book_id=body.book_id, scene_id=scene_id, message=body.message)
        if managed is not None:
            return managed
        return repo.upsert_scene_control(body.book_id, scene_id, "ready", message=body.message)

    return app


def _objective_achieved(state_payload: dict[str, Any]) -> bool:
    flag = state_payload.get("objective_achieved")
    if isinstance(flag, bool) and flag:
        return True

    status = str(state_payload.get("objective_status", "")).strip().lower()
    return status in {"achieved", "completed", "done", "success"}


def _normalize_conflicts(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _load_json(raw_text: str | None) -> Any:
    if raw_text is None:
        return None
    try:
        return sqlite_safe_json_loads(raw_text)
    except Exception:
        return raw_text


def sqlite_safe_json_loads(raw_text: str) -> Any:
    return json.loads(raw_text)


def _parse_iso_datetime(raw_value: str) -> datetime:
    text = raw_value.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Datetime query cannot be empty")

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid datetime format: {raw_value}") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_decision_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None

    item = dict(row)
    item["turn"] = int(item["turn"] or 0)
    item["options"] = _load_json(item.pop("options_json")) or []
    item["metadata"] = _load_json(item.pop("metadata_json")) or {}

    expires_at = _parse_safe_datetime(item.get("expires_at"))
    if expires_at is not None:
        item["remaining_seconds"] = max(
            0,
            int((expires_at - datetime.now(timezone.utc)).total_seconds()),
        )
    else:
        item["remaining_seconds"] = 0

    return item


def _parse_safe_datetime(raw: Any) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _normalize_participant_alias(value: str) -> str:
    text = value.strip()
    text = re.sub(r"\s+", "", text)
    return text.casefold()


def _build_agent_alias_map(factory: AgentFactory, book_id: str) -> tuple[dict[str, str], list[str]]:
    alias_to_agent: dict[str, str] = {}
    available: list[str] = []

    try:
        skills = factory.load_skills_for_book(book_id)
    except Exception:
        return alias_to_agent, available

    for skill in skills.values():
        agent_id = skill.agent_id
        available.append(agent_id)
        alias_candidates = [
            agent_id,
            skill.display_name,
            skill.identity.get("姓名", ""),
            skill.identity.get("称呼", ""),
        ]
        for candidate in alias_candidates:
            text = str(candidate or "").strip()
            if not text:
                continue
            normalized = _normalize_participant_alias(text)
            alias_to_agent.setdefault(normalized, agent_id)

    available = sorted(set(available))
    return alias_to_agent, available


def _resolve_participants(factory: AgentFactory, book_id: str, participants: list[str]) -> list[str]:
    alias_to_agent, available_agents = _build_agent_alias_map(factory, book_id)
    resolved: list[str] = []
    missing: list[str] = []

    for raw in participants:
        text = str(raw or "").strip()
        if not text:
            continue
        normalized = _normalize_participant_alias(text)
        agent_id = alias_to_agent.get(normalized)
        if agent_id is None:
            missing.append(text)
            continue
        if agent_id not in resolved:
            resolved.append(agent_id)

    if missing:
        available_text = ", ".join(available_agents) if available_agents else "(none)"
        missing_text = ", ".join(missing)
        raise ValueError(
            f"Participants missing in agents: [{missing_text}]. "
            f"Available agent_id: {available_text}. "
            "You can input agent_id, 姓名, or 称呼."
        )

    if not resolved:
        raise ValueError("participants cannot be empty")

    return resolved


def _normalize_profile_payload(profile: dict[str, Any], *, require_complete: bool) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key in BOOK_PROFILE_FIELD_KEYS:
        if key not in profile:
            continue
        value = str(profile.get(key) or "").strip()
        if not value:
            raise ValueError(f"`{key}` cannot be empty")
        normalized[key] = value

    if require_complete:
        missing = [key for key in BOOK_PROFILE_FIELD_KEYS if key not in normalized]
        if missing:
            missing_text = ", ".join(missing)
            raise ValueError(f"Missing profile fields: {missing_text}")

    return normalized


def _format_book_profile_context(profile: dict[str, Any] | None) -> str:
    if not profile:
        return ""
    lines = []
    for key, label in BOOK_PROFILE_FIELDS:
        value = str(profile.get(key) or "").strip()
        if value:
            lines.append(f"- {label}: {value}")
    if not lines:
        return ""
    return "[书籍设定]\n" + "\n".join(lines)


def _compose_scene_context(
    base_context: str,
    notes: list[str],
    *,
    profile_context: str = "",
) -> str:
    sections: list[str] = []
    profile_text = profile_context.strip()
    if profile_text:
        sections.append(profile_text)

    base_text = base_context.strip()
    if base_text:
        sections.append(base_text)

    clean_notes = [note.strip() for note in notes if note and note.strip()]
    if clean_notes:
        notes_text = "\n".join(f"- {note}" for note in clean_notes[-8:])
        sections.append(f"[创作者干预]\n{notes_text}")

    return "\n\n".join(sections)


def _deep_merge_dicts(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in delta.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _update_unresolved_conflicts(existing: list[str], decision: DirectorDecision) -> list[str]:
    conflicts = [item for item in existing if isinstance(item, str)]
    delta_conflicts = decision.state_delta.get("unresolved_conflicts")
    if isinstance(delta_conflicts, list):
        return [str(item) for item in delta_conflicts]

    if decision.conflict:
        conflict = decision.conflict.strip()
        if conflict and conflict not in conflicts:
            conflicts.append(conflict)
    elif decision.accepted and conflicts:
        conflicts = conflicts[1:]

    return conflicts


def _should_request_user_decision(decision: DirectorDecision) -> bool:
    if decision.conflict:
        return True
    if decision.state_delta.get("director_fallback"):
        return True
    return float(decision.confidence) < 0.65


def _build_decision_options(
    director_decision: DirectorDecision,
) -> tuple[list[dict[str, str]], str]:
    options = [
        {
            "id": "accept_director",
            "label": "按导演裁决推进",
            "description": "保持当前裁决，继续推进剧情。",
        },
        {
            "id": "use_actor_proposal",
            "label": "采用角色原提案",
            "description": "忽略导演修正，使用角色动作原案。",
        },
        {
            "id": "conservative_rewrite",
            "label": "保守改写推进",
            "description": "降低冲突强度，保留推进目标。",
        },
    ]

    if director_decision.conflict or director_decision.confidence < 0.45:
        return options, "conservative_rewrite"
    return options, "accept_director"


def _build_state_update_decision_options(
    update: CharacterStateUpdate,
    *,
    threshold: float,
) -> tuple[list[dict[str, str]], str]:
    options = [
        {
            "id": "state_update_apply",
            "label": "应用状态变更",
            "description": "接受导演更新并立即写入角色动态状态。",
        },
        {
            "id": "state_update_discard",
            "label": "忽略本次变更",
            "description": "不应用该更新，保持当前角色动态状态不变。",
        },
    ]
    if float(update.confidence) >= max(0.0, threshold - 0.1):
        return options, "state_update_apply"
    return options, "state_update_discard"


def _apply_user_decision_choice(
    selected_option: str,
    *,
    proposed_action: AgentAction,
    director_decision: DirectorDecision,
    source: str,
) -> DirectorDecision:
    if selected_option == "use_actor_proposal":
        return DirectorDecision(
            accepted=True,
            resolved_action=proposed_action,
            state_delta=copy.deepcopy(director_decision.state_delta),
            conflict=None,
            rationale=f"user_selected:{source}; use actor proposal",
            confidence=0.85,
            character_updates=copy.deepcopy(director_decision.character_updates),
        )

    if selected_option == "conservative_rewrite":
        action = director_decision.resolved_action
        conservative_action = AgentAction(
            agent_id=action.agent_id,
            intent=action.intent,
            speech=action.speech or "先稳住局面，再继续试探。",
            action=f"保守推进: {action.action}",
            emotion="克制",
            target=action.target,
            reason=action.reason,
            goal_progress="以低风险方式维持目标推进",
            meta={**action.meta, "user_choice": "conservative_rewrite", "source": source},
        )
        state_delta = copy.deepcopy(director_decision.state_delta)
        state_delta.setdefault("uncertainty_resolution", "conservative_rewrite")
        return DirectorDecision(
            accepted=True,
            resolved_action=conservative_action,
            state_delta=state_delta,
            conflict=None,
            rationale=f"user_selected:{source}; conservative rewrite",
            confidence=0.8,
            character_updates=copy.deepcopy(director_decision.character_updates),
        )

    return DirectorDecision(
        accepted=director_decision.accepted,
        resolved_action=director_decision.resolved_action,
        state_delta=copy.deepcopy(director_decision.state_delta),
        conflict=director_decision.conflict,
        rationale=f"user_selected:{source}; accept director",
        confidence=max(0.7, float(director_decision.confidence)),
        character_updates=copy.deepcopy(director_decision.character_updates),
    )


def build_app_from_env() -> FastAPI:
    urls = os.getenv("LIVING_NOVEL_URLS_CONFIG", DEFAULT_URLS_CONFIG)
    runtime = os.getenv("LIVING_NOVEL_RUNTIME_CONFIG", DEFAULT_RUNTIME_CONFIG)
    factory = os.getenv("LIVING_NOVEL_FACTORY_CONFIG", DEFAULT_FACTORY_CONFIG)
    return create_dashboard_app(
        urls_config_path=urls,
        runtime_config_path=runtime,
        factory_config_path=factory,
    )


app = build_app_from_env()
