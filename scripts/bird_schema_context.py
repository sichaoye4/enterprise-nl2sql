"""Enriched BIRD schema context builder for NL2SQL prompts."""
from __future__ import annotations

import csv
import os
import re
import sqlite3
from collections import defaultdict
from functools import lru_cache


MAX_SAMPLE_COLUMNS = 40
MAX_TABLE_COLUMNS_FOR_SAMPLING = 220
TEXT_TYPES = ("CHAR", "CLOB", "TEXT", "VARCHAR", "NCHAR", "NVARCHAR")


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _tokens(text: str) -> set[str]:
    return {
        t.lower()
        for t in re.findall(r"[A-Za-z][A-Za-z0-9_]+", text or "")
        if len(t) > 1
    }


def _name_tokens(name: str) -> set[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name or "")
    return _tokens(spaced.replace("_", " "))


@lru_cache(maxsize=128)
def _raw_ddl(db_path: str) -> tuple[str, ...]:
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL ORDER BY name"
            ).fetchall()
        return tuple(r[0] for r in rows)
    except sqlite3.Error:
        return ()


@lru_cache(maxsize=128)
def _schema_info(db_path: str) -> tuple[tuple[str, tuple[tuple]], ...]:
    try:
        with sqlite3.connect(db_path) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            ]
            out = []
            for table in tables:
                cols = conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
                out.append((table, tuple(cols)))
        return tuple(out)
    except sqlite3.Error:
        return ()


@lru_cache(maxsize=128)
def _foreign_keys(db_path: str) -> tuple[tuple[str, str, str, str], ...]:
    paths: list[tuple[str, str, str, str]] = []
    try:
        with sqlite3.connect(db_path) as conn:
            for table, _cols in _schema_info(db_path):
                for row in conn.execute(f"PRAGMA foreign_key_list({_quote_ident(table)})").fetchall():
                    # row: id, seq, table, from, to, on_update, on_delete, match
                    paths.append((table, row[3], row[2], row[4]))
    except sqlite3.Error:
        pass
    return tuple(paths)


@lru_cache(maxsize=128)
def _descriptions(db_dir: str) -> dict[tuple[str, str], dict[str, str]]:
    desc_dir = os.path.join(db_dir, "database_description")
    descriptions: dict[tuple[str, str], dict[str, str]] = {}
    if not os.path.isdir(desc_dir):
        return descriptions
    for filename in os.listdir(desc_dir):
        if not filename.endswith(".csv"):
            continue
        table = os.path.splitext(filename)[0]
        path = os.path.join(desc_dir, filename)
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    col = (row.get("original_column_name") or row.get("column_name") or "").strip()
                    if not col:
                        continue
                    descriptions[(table.lower(), col.lower())] = {
                        "description": (row.get("column_description") or "").strip(),
                        "value_description": (row.get("value_description") or "").strip(),
                    }
        except (OSError, csv.Error, UnicodeDecodeError):
            continue
    return descriptions


@lru_cache(maxsize=128)
def _inferred_join_paths(db_path: str) -> tuple[tuple[str, str, str, str], ...]:
    explicit = set(_foreign_keys(db_path))
    paths = set(explicit)
    cols_by_name: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for table, cols in _schema_info(db_path):
        for _cid, name, _ctype, _notnull, _dflt, pk in cols:
            norm = re.sub(r"[^a-z0-9]", "", name.lower())
            if norm and (pk or norm.endswith("id") or norm.endswith("code") or norm in {"uuid", "cds"}):
                cols_by_name[norm].append((table, name, pk))
    for matches in cols_by_name.values():
        if len(matches) < 2 or len(matches) > 8:
            continue
        pk_matches = [m for m in matches if m[2]]
        targets = pk_matches or matches[:1]
        for table, col, _pk in matches:
            for to_table, to_col, _to_pk in targets:
                if table != to_table:
                    paths.add((table, col, to_table, to_col))
    return tuple(sorted(paths))


@lru_cache(maxsize=128)
def _all_samples(db_path: str) -> dict[tuple[str, str], tuple[str, ...]]:
    samples: dict[tuple[str, str], tuple[str, ...]] = {}
    schema = _schema_info(db_path)
    column_count = sum(len(cols) for _table, cols in schema)
    if column_count > MAX_TABLE_COLUMNS_FOR_SAMPLING:
        return samples
    sampled = 0
    try:
        with sqlite3.connect(db_path) as conn:
            for table, cols in schema:
                for _cid, name, ctype, _notnull, _dflt, _pk in cols:
                    if sampled >= MAX_SAMPLE_COLUMNS:
                        return samples
                    if not any(t in (ctype or "").upper() for t in TEXT_TYPES):
                        continue
                    sql = (
                        f"SELECT {_quote_ident(name)} FROM {_quote_ident(table)} "
                        f"WHERE {_quote_ident(name)} IS NOT NULL "
                        f"GROUP BY {_quote_ident(name)} ORDER BY COUNT(*) DESC LIMIT 5"
                    )
                    try:
                        vals = [str(r[0]) for r in conn.execute(sql).fetchall() if r[0] not in (None, "")]
                    except sqlite3.Error:
                        vals = []
                    if vals:
                        samples[(table.lower(), name.lower())] = tuple(vals)
                        sampled += 1
    except sqlite3.Error:
        pass
    return samples


def _semantic_root(db_root: str) -> str:
    root = os.path.abspath(db_root)
    while root and root != os.path.dirname(root):
        candidate = os.path.join(root, "bird_semantic")
        if os.path.isdir(candidate):
            return candidate
        root = os.path.dirname(root)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bird_semantic"))


def _semantic_matches(db_root: str, db_id: str, text: str) -> tuple[list[str], list[str]]:
    sem_dir = os.path.join(_semantic_root(db_root), db_id)
    if not os.path.isdir(sem_dir):
        return [], []
    words = _tokens(text)
    hints: list[str] = []
    joins: list[str] = []
    for subdir in ("terms", "dimensions", "metrics"):
        folder = os.path.join(sem_dir, subdir)
        if not os.path.isdir(folder):
            continue
        for filename in sorted(os.listdir(folder)):
            if not filename.endswith((".yaml", ".yml", ".json")):
                continue
            name = os.path.splitext(filename)[0]
            if not (_name_tokens(name) & words):
                continue
            path = os.path.join(folder, filename)
            try:
                body = open(path, encoding="utf-8").read().strip()
            except OSError:
                continue
            first_lines = [ln for ln in body.splitlines()[:12] if ln.strip()]
            hints.append(f"- {subdir[:-1]} `{name}`:\n  " + "\n  ".join(first_lines))
            if len(hints) >= 12:
                break
    join_dir = os.path.join(sem_dir, "join_paths")
    if os.path.isdir(join_dir):
        for filename in sorted(os.listdir(join_dir))[:20]:
            if not filename.endswith((".yaml", ".yml", ".json")):
                continue
            path = os.path.join(join_dir, filename)
            try:
                body = open(path, encoding="utf-8").read()
            except OSError:
                continue
            m = re.search(r"join_condition:\s*(.+)", body)
            if m:
                joins.append(f"- {m.group(1).strip()}")
    return hints, joins


def build_schema_context(db_root, db_id, question, evidence) -> str:
    """Return enriched schema context for a BIRD database.

    Gracefully falls back to raw DDL when descriptions, semantic files, or sample
    values are unavailable.
    """
    db_dir = os.path.join(str(db_root), db_id)
    db_path = os.path.join(db_dir, f"{db_id}.sqlite")
    desc = _descriptions(db_dir)
    schema = _schema_info(db_path)
    join_paths = list(_inferred_join_paths(db_path))
    text = f"{question or ''}\n{evidence or ''}"
    query_words = _tokens(text)

    lines: list[str] = [f"Database Schema for: {db_id}", "", "Raw DDL:"]
    ddl = _raw_ddl(db_path)
    lines.append("\n\n".join(ddl) if ddl else "(schema unavailable)")

    samples = _all_samples(db_path)
    relevant_samples: dict[tuple[str, str], tuple[str, ...]] = {}

    lines.extend(["", "Table/column definitions:"])
    for table, cols in schema:
        lines.append(f"- {table}")
        lines.append("  Columns:")
        for _cid, name, ctype, notnull, _dflt, pk in cols:
            markers = []
            if pk:
                markers.append("PK")
            if notnull:
                markers.append("NOT NULL")
            links = [jp for jp in join_paths if jp[0].lower() == table.lower() and jp[1].lower() == name.lower()]
            if links:
                markers.append("links to " + ", ".join(f"{to_t}.{to_c}" for _t, _c, to_t, to_c in links[:3]))
            meta = desc.get((table.lower(), name.lower()), {})
            description = meta.get("description") or meta.get("value_description") or ""
            overlap = query_words & (_name_tokens(table) | _name_tokens(name) | _tokens(description))
            sample_vals = samples.get((table.lower(), name.lower())) if overlap else None
            if sample_vals:
                relevant_samples[(table.lower(), name.lower())] = sample_vals
            marker_text = f" ({', '.join(markers)})" if markers else ""
            desc_text = f" - {description}" if description else ""
            lines.append(f"    `{name}` {ctype or ''}{marker_text}{desc_text}".rstrip())

    lines.extend(["", "Join paths:"])
    if join_paths:
        lines.extend(f"- {t}.{c} -> {to_t}.{to_c}" for t, c, to_t, to_c in join_paths[:80])
    else:
        lines.append("- (none detected)")

    lines.extend(["", "Sample values for relevant text columns:"])
    if relevant_samples:
        for (table, col), vals in sorted(relevant_samples.items()):
            rendered = ", ".join(repr(v) for v in vals[:5])
            lines.append(f"- {table}.`{col}`: {rendered}")
    else:
        lines.append("- (no relevant samples selected)")

    semantic_hints, semantic_joins = _semantic_matches(str(db_root), db_id, text)
    if semantic_joins:
        lines.extend(["", "Semantic join paths (from BIRD bird_semantic):"])
        lines.extend(semantic_joins)
    lines.extend(["", "Semantic registry hints (from BIRD bird_semantic):"])
    if semantic_hints:
        lines.extend(semantic_hints)
    else:
        lines.append("- (no matching semantic hints)")

    lines.extend(["", "BIRD evidence:"])
    lines.append(evidence or "(none)")
    return "\n".join(lines)
