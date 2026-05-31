"""Persistent database registry for NL2SQL pattern memory."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


DEFAULT_REGISTRY_PATH = "~/.hermes/nl2sql_registry.db"


class DBLifecycle(StrEnum):
    NEW = "NEW"
    SEEDING = "SEEDING"
    ACTIVE = "ACTIVE"
    STALE = "STALE"


@dataclass(frozen=True)
class DatabaseProfile:
    db_id: str
    source: str
    db_path: str
    schema_fingerprint: str
    schema_snapshot: dict[str, Any]
    table_names: list[str]
    pattern_count: int
    maturity_tier: int
    lifecycle: str
    first_seen: float
    last_seen: float
    last_schema_change: float


def maturity_tier_for_count(pattern_count: int) -> int:
    if pattern_count >= 50:
        return 1
    if pattern_count >= 10:
        return 2
    if pattern_count >= 5:
        return 3
    if pattern_count >= 1:
        return 4
    return 5


def lifecycle_for_count(pattern_count: int) -> str:
    if pattern_count <= 0:
        return DBLifecycle.NEW.value
    if pattern_count < 5:
        return DBLifecycle.SEEDING.value
    return DBLifecycle.ACTIVE.value


def extract_sqlite_schema(db_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Extract a stable SQLite schema snapshot."""
    path = os.fspath(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        table_rows = conn.execute(
            """
            SELECT name, sql
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        tables: list[dict[str, Any]] = []
        for row in table_rows:
            table_name = row["name"]
            escaped = table_name.replace('"', '""')
            columns = [
                {
                    "name": col["name"],
                    "type": col["type"] or "",
                    "not_null": bool(col["notnull"]),
                    "default": col["dflt_value"],
                    "primary_key_position": int(col["pk"] or 0),
                }
                for col in conn.execute(f'PRAGMA table_info("{escaped}")').fetchall()
            ]
            foreign_keys = [
                {
                    "from": fk["from"],
                    "to_table": fk["table"],
                    "to": fk["to"],
                    "on_update": fk["on_update"],
                    "on_delete": fk["on_delete"],
                }
                for fk in conn.execute(f'PRAGMA foreign_key_list("{escaped}")').fetchall()
            ]
            tables.append(
                {
                    "name": table_name,
                    "create_sql": row["sql"] or "",
                    "columns": columns,
                    "foreign_keys": foreign_keys,
                }
            )
        return {"dialect": "sqlite", "tables": tables}
    finally:
        conn.close()


def schema_fingerprint(schema_snapshot: dict[str, Any]) -> str:
    payload = json.dumps(schema_snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DBRegistry:
    """SQLite-backed registry of encountered databases and schema state."""

    def __init__(self, db_path: str = DEFAULT_REGISTRY_PATH):
        self.db_path = os.path.expanduser(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS databases (
                    db_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL DEFAULT '',
                    db_path TEXT NOT NULL DEFAULT '',
                    schema_fingerprint TEXT NOT NULL,
                    schema_snapshot TEXT NOT NULL,
                    table_names TEXT NOT NULL,
                    pattern_count INTEGER NOT NULL DEFAULT 0,
                    maturity_tier INTEGER NOT NULL DEFAULT 5,
                    lifecycle TEXT NOT NULL DEFAULT 'NEW',
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    last_schema_change REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_databases_tier ON databases(maturity_tier)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_databases_lifecycle ON databases(lifecycle)")

    def register_database(
        self,
        db_id: str,
        schema_snapshot: dict[str, Any] | None = None,
        db_path: str = "",
        source: str = "",
    ) -> DatabaseProfile:
        if not schema_snapshot:
            if not db_path:
                raise ValueError("register_database requires schema_snapshot or db_path")
            schema_snapshot = extract_sqlite_schema(db_path)

        now = time.time()
        table_names = sorted(t["name"] for t in schema_snapshot.get("tables", []))
        fingerprint = schema_fingerprint(schema_snapshot)

        with self._connect() as conn:
            existing = conn.execute("SELECT * FROM databases WHERE db_id = ?", (db_id,)).fetchone()
            if existing is None:
                profile = {
                    "db_id": db_id,
                    "source": source,
                    "db_path": db_path,
                    "schema_fingerprint": fingerprint,
                    "schema_snapshot": json.dumps(schema_snapshot, sort_keys=True),
                    "table_names": json.dumps(table_names),
                    "pattern_count": 0,
                    "maturity_tier": 5,
                    "lifecycle": DBLifecycle.NEW.value,
                    "first_seen": now,
                    "last_seen": now,
                    "last_schema_change": now,
                }
                conn.execute(
                    """
                    INSERT INTO databases (
                        db_id, source, db_path, schema_fingerprint, schema_snapshot,
                        table_names, pattern_count, maturity_tier, lifecycle,
                        first_seen, last_seen, last_schema_change
                    )
                    VALUES (
                        :db_id, :source, :db_path, :schema_fingerprint, :schema_snapshot,
                        :table_names, :pattern_count, :maturity_tier, :lifecycle,
                        :first_seen, :last_seen, :last_schema_change
                    )
                    """,
                    profile,
                )
            else:
                changed = existing["schema_fingerprint"] != fingerprint
                lifecycle = DBLifecycle.STALE.value if changed else existing["lifecycle"]
                conn.execute(
                    """
                    UPDATE databases
                    SET source = COALESCE(NULLIF(?, ''), source),
                        db_path = COALESCE(NULLIF(?, ''), db_path),
                        schema_fingerprint = ?,
                        schema_snapshot = ?,
                        table_names = ?,
                        lifecycle = ?,
                        last_seen = ?,
                        last_schema_change = CASE WHEN ? THEN ? ELSE last_schema_change END
                    WHERE db_id = ?
                    """,
                    (
                        source,
                        db_path,
                        fingerprint,
                        json.dumps(schema_snapshot, sort_keys=True),
                        json.dumps(table_names),
                        lifecycle,
                        now,
                        1 if changed else 0,
                        now,
                        db_id,
                    ),
                )
        return self.get(db_id)  # type: ignore[return-value]

    def register_sqlite_database(self, db_id: str, db_path: str, source: str = "") -> DatabaseProfile:
        return self.register_database(db_id=db_id, db_path=db_path, source=source)

    def register_bird_database(self, db_root: str, db_id: str) -> DatabaseProfile:
        db_path = os.path.join(db_root, db_id, f"{db_id}.sqlite")
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"BIRD SQLite database not found: {db_path}")
        return self.register_sqlite_database(db_id=db_id, db_path=db_path, source="bird")

    def ensure_database(
        self,
        db_id: str,
        db_path: str = "",
        db_root: str = "",
        source: str = "",
    ) -> DatabaseProfile | None:
        existing = self.get(db_id)
        path = db_path
        if not path and db_root:
            candidate = os.path.join(db_root, db_id, f"{db_id}.sqlite")
            if os.path.exists(candidate):
                path = candidate
                source = source or "bird"
        if path and os.path.exists(path):
            return self.register_sqlite_database(db_id=db_id, db_path=path, source=source)
        return existing

    def get(self, db_id: str) -> DatabaseProfile | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM databases WHERE db_id = ?", (db_id,)).fetchone()
        return self._row_to_profile(row) if row else None

    def set_pattern_count(self, db_id: str, pattern_count: int) -> None:
        tier = maturity_tier_for_count(pattern_count)
        lifecycle = lifecycle_for_count(pattern_count)
        with self._connect() as conn:
            current = conn.execute("SELECT lifecycle FROM databases WHERE db_id = ?", (db_id,)).fetchone()
            if not current:
                return
            if current["lifecycle"] == DBLifecycle.STALE.value:
                lifecycle = DBLifecycle.STALE.value
            conn.execute(
                """
                UPDATE databases
                SET pattern_count = ?, maturity_tier = ?, lifecycle = ?, last_seen = ?
                WHERE db_id = ?
                """,
                (pattern_count, tier, lifecycle, time.time(), db_id),
            )

    def increment_pattern_count(self, db_id: str, amount: int = 1) -> None:
        profile = self.get(db_id)
        if not profile:
            return
        self.set_pattern_count(db_id, profile.pattern_count + amount)

    def find_similar_databases(
        self,
        db_id: str,
        threshold: float = 0.2,
        limit: int = 5,
    ) -> list[tuple[DatabaseProfile, float]]:
        profile = self.get(db_id)
        if not profile:
            return []
        return self.find_similar_by_tables(profile.table_names, exclude_db_id=db_id, threshold=threshold, limit=limit)

    def find_similar_by_tables(
        self,
        table_names: list[str],
        exclude_db_id: str = "",
        threshold: float = 0.2,
        limit: int = 5,
    ) -> list[tuple[DatabaseProfile, float]]:
        target = {t.lower() for t in table_names}
        if not target:
            return []
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM databases").fetchall()
        scored: list[tuple[DatabaseProfile, float]] = []
        for row in rows:
            candidate = self._row_to_profile(row)
            if candidate.db_id == exclude_db_id:
                continue
            candidate_tables = {t.lower() for t in candidate.table_names}
            union = target | candidate_tables
            score = len(target & candidate_tables) / len(union) if union else 0.0
            if score >= threshold:
                scored.append((candidate, score))
        scored.sort(key=lambda item: (-item[1], item[0].db_id))
        return scored[:limit]

    def list_databases(self) -> list[DatabaseProfile]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM databases ORDER BY db_id").fetchall()
        return [self._row_to_profile(row) for row in rows]

    def _row_to_profile(self, row: sqlite3.Row) -> DatabaseProfile:
        return DatabaseProfile(
            db_id=row["db_id"],
            source=row["source"],
            db_path=row["db_path"],
            schema_fingerprint=row["schema_fingerprint"],
            schema_snapshot=json.loads(row["schema_snapshot"]),
            table_names=json.loads(row["table_names"]),
            pattern_count=int(row["pattern_count"]),
            maturity_tier=int(row["maturity_tier"]),
            lifecycle=row["lifecycle"],
            first_seen=float(row["first_seen"]),
            last_seen=float(row["last_seen"]),
            last_schema_change=float(row["last_schema_change"]),
        )


def default_bird_db_root() -> str:
    return str(
        Path(__file__).resolve().parents[3]
        / "bird_bench"
        / "dev"
        / "dev_20240627"
        / "databases"
        / "dev_databases"
    )
