"""Persistent project-scoped memory backed by SQLite."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


_SENSITIVE_KEY = re.compile(r"secret|token|password|api[_-]?key", re.IGNORECASE)


@dataclass(frozen=True)
class MemoryRecord:
    """A recalled memory item."""

    project_id: str
    key: str
    value: str
    tags: tuple[str, ...]
    created_at: int
    updated_at: int


class MemoryStore:
    """Store and recall tagged memory without crossing project boundaries."""

    def __init__(self, database: str | Path) -> None:
        self._connection = sqlite3.connect(str(database))
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                project_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (project_id, key)
            )
            """
        )
        self._connection.commit()

    def upsert(
        self, project_id: str, key: str, value: str, tags: list[str]
    ) -> None:
        """Insert or replace one project memory after normalizing its tags."""

        if _SENSITIVE_KEY.search(key):
            raise ValueError("sensitive credential-shaped memory keys are forbidden")

        encoded_tags = json.dumps(
            sorted(set(tags)), ensure_ascii=False, separators=(",", ":")
        )
        timestamp = time.time_ns()
        self._connection.execute(
            """
            INSERT INTO memories (
                project_id, key, value, tags_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, key) DO UPDATE SET
                value = excluded.value,
                tags_json = excluded.tags_json,
                updated_at = excluded.updated_at
            """,
            (project_id, key, value, encoded_tags, timestamp, timestamp),
        )
        self._connection.commit()

    def recall(
        self,
        project_id: str,
        tags: list[str],
        limit: int,
        char_budget: int,
    ) -> list[MemoryRecord]:
        """Recall tag-matching memories ordered by overlap and recency."""

        if limit <= 0 or char_budget <= 0 or not tags:
            return []

        requested_tags = set(tags)
        matches: list[tuple[int, MemoryRecord]] = []
        rows = self._connection.execute(
            """
            SELECT project_id, key, value, tags_json, created_at, updated_at
            FROM memories
            WHERE project_id = ?
            """,
            (project_id,),
        )
        for row_project_id, key, value, encoded_tags, created_at, updated_at in rows:
            stored_tags = tuple(json.loads(encoded_tags))
            overlap = len(requested_tags.intersection(stored_tags))
            if overlap:
                matches.append(
                    (
                        overlap,
                        MemoryRecord(
                            project_id=row_project_id,
                            key=key,
                            value=value,
                            tags=stored_tags,
                            created_at=created_at,
                            updated_at=updated_at,
                        ),
                    )
                )

        matches.sort(
            key=lambda match: (-match[0], -match[1].updated_at, match[1].key)
        )
        recalled: list[MemoryRecord] = []
        used_characters = 0
        for _, memory in matches:
            if len(recalled) >= limit:
                break
            value_characters = len(memory.value)
            if used_characters + value_characters > char_budget:
                continue
            recalled.append(memory)
            used_characters += value_characters
        return recalled
