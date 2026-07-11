from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


def _load_api_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "nl2sql_api.py"
    spec = importlib.util.spec_from_file_location("nl2sql_api_for_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_database_preview_includes_schema_counts_and_sample_rows(tmp_path: Path) -> None:
    api = _load_api_module()
    api.BIRD_ROOT = str(tmp_path)
    db_id = "demo_database"
    db_dir = tmp_path / db_id
    db_dir.mkdir()
    db_path = db_dir / f"{db_id}.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            'CREATE TABLE "orders" ("id" INTEGER PRIMARY KEY, "customer" TEXT NOT NULL)'
        )
        connection.executemany(
            'INSERT INTO "orders" ("customer") VALUES (?)', [("Ada",), ("Grace",)]
        )

    preview = api.get_database_preview(db_id, row_limit=1)

    assert preview["name"] == "Demo Database"
    assert preview["table_count"] == 1
    table = preview["tables"][0]
    assert table["name"] == "orders"
    assert table["row_count"] == 2
    assert table["columns"] == [
        {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
        {"name": "customer", "type": "TEXT", "primary_key": False, "nullable": False},
    ]
    assert table["sample_rows"] == [{"id": 1, "customer": "Ada"}]


def test_database_preview_returns_a_safe_error_for_missing_database(tmp_path: Path) -> None:
    api = _load_api_module()
    api.BIRD_ROOT = str(tmp_path)

    assert api.get_database_preview("missing") == {"error": "Database not found"}
