#!/usr/bin/env python3
"""Build semantic-engine YAML models for BIRD dev databases.

The generated files use the newer semantic_engine single-file model format:

    bird_semantic_engine/<db_id>/model.yml

The optional evaluation mode is deliberately local and API-free. It routes each
BIRD question through the semantic engine and executes only deterministic
SEMANTIC_SQL outputs against SQLite, then compares with a stored LLM baseline.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parent.parent
DEV_DIR = ROOT / "bird_bench" / "dev" / "dev_20240627"
DB_ROOT = DEV_DIR / "databases" / "dev_databases"
OUTPUT_DIR = ROOT / "bird_semantic_engine"
RESULTS_DIR = ROOT / "bird_bench" / "results"
OWNER = "bird_benchmark"

STOPWORDS = {
    "what", "which", "where", "that", "have", "with", "from", "show",
    "list", "give", "tell", "find", "the", "and", "for", "how", "many",
    "much", "total", "each", "name", "please", "does", "dose", "than",
    "there", "their", "they", "been", "were", "being", "would", "could",
    "should", "about", "between", "through", "during", "before", "after",
    "above", "below", "into", "more", "less", "most", "least",
    "some", "any", "all", "both", "such", "just", "also", "very", "well",
    "then", "here", "there", "when", "where", "why", "how", "our", "your",
    "its", "his", "her", "their", "this", "that", "these", "those",
}

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass(frozen=True)
class ColumnInfo:
    index: int
    table_idx: int
    table: str
    column: str
    column_type: str
    member_name: str


@dataclass
class MeasureUse:
    entity: str
    column: str
    column_member: str
    aggregation: str
    distinct: bool = False
    filters: tuple[tuple[str, str, tuple[Any, ...]], ...] = ()
    group_members: set[str] = field(default_factory=set)
    questions: set[int] = field(default_factory=set)
    where_examples: Counter[str] = field(default_factory=Counter)
    question_terms: set[str] = field(default_factory=set)


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def flatten_primary_keys(raw: list[Any]) -> set[int]:
    output: set[int] = set()
    for item in raw:
        if isinstance(item, list):
            output.update(int(value) for value in item)
        else:
            output.add(int(item))
    return output


def titleize(value: str) -> str:
    words = re.sub(r"[_\-]+", " ", value).strip().split()
    return " ".join(word[:1].upper() + word[1:] for word in words) or value


def member_name(value: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower() or "member"
    if re.match(r"^\d", base):
        base = f"c_{base}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def sql_quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def schema_maps(schema: dict[str, Any]) -> tuple[dict[int, list[ColumnInfo]], dict[int, ColumnInfo], dict[tuple[str, str], ColumnInfo]]:
    table_names = schema["table_names_original"]
    column_names = schema["column_names_original"]
    column_types = schema["column_types"]
    used_by_table: dict[int, set[str]] = defaultdict(set)
    by_table: dict[int, list[ColumnInfo]] = defaultdict(list)
    by_index: dict[int, ColumnInfo] = {}
    by_name: dict[tuple[str, str], ColumnInfo] = {}

    for idx, (table_idx, column) in enumerate(column_names):
        if table_idx == -1:
            continue
        table = table_names[table_idx]
        info = ColumnInfo(
            index=idx,
            table_idx=table_idx,
            table=table,
            column=column,
            column_type=str(column_types[idx] if idx < len(column_types) else "text").lower(),
            member_name=member_name(column, used_by_table[table_idx]),
        )
        by_table[table_idx].append(info)
        by_index[idx] = info
        by_name[(table.lower(), column.lower())] = info
    return by_table, by_index, by_name


def sqlite_cardinality(db_id: str, table: str, column: str) -> tuple[int, int] | None:
    db_path = DB_ROOT / db_id / f"{db_id}.sqlite"
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT {sql_quote(column)}) FROM {sql_quote(table)}"
            ).fetchone()
        return (int(row[0] or 0), int(row[1] or 0))
    except sqlite3.Error:
        return None


def build_aliases(tree: exp.Expression) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        table_name = table.name
        aliases[table_name.lower()] = table_name
        if table.alias:
            aliases[table.alias.lower()] = table_name
    return aliases


def resolve_column(
    column: exp.Column,
    aliases: dict[str, str],
    by_name: dict[tuple[str, str], ColumnInfo],
) -> ColumnInfo | None:
    col_name = column.name
    qualifier = column.table
    if qualifier:
        table = aliases.get(qualifier.lower(), qualifier)
        return by_name.get((table.lower(), col_name.lower()))
    matches = [info for (table, col), info in by_name.items() if col == col_name.lower()]
    return matches[0] if len(matches) == 1 else None


def first_table_entity(tree: exp.Expression, entity_by_table: dict[str, str]) -> str | None:
    table = next(tree.find_all(exp.Table), None)
    if not table:
        return None
    return entity_by_table.get(table.name)


def extract_group_members(
    tree: exp.Expression,
    aliases: dict[str, str],
    by_name: dict[tuple[str, str], ColumnInfo],
    dimension_by_col: dict[int, str],
    entity_by_table: dict[str, str],
) -> set[str]:
    output: set[str] = set()
    group = tree.args.get("group")
    if not group:
        return output
    for column in group.find_all(exp.Column):
        info = resolve_column(column, aliases, by_name)
        if info and info.index in dimension_by_col:
            output.add(f"{entity_by_table[info.table]}.{dimension_by_col[info.index]}")
    return output


def extract_simple_filters(
    tree: exp.Expression,
    aliases: dict[str, str],
    by_name: dict[tuple[str, str], ColumnInfo],
    dimension_by_col: dict[int, str],
) -> list[tuple[str, str, tuple[Any, ...]]]:
    where = tree.args.get("where")
    if not where:
        return []
    filters: list[tuple[str, str, tuple[Any, ...]]] = []
    for predicate in where.walk():
        if isinstance(predicate, exp.EQ):
            left, right = predicate.left, predicate.right
            if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
                info = resolve_column(left, aliases, by_name)
                if info and info.index in dimension_by_col:
                    filters.append((dimension_by_col[info.index], "equals", (right.to_py(),)))
        elif isinstance(predicate, exp.In) and isinstance(predicate.this, exp.Column):
            info = resolve_column(predicate.this, aliases, by_name)
            values = []
            for item in predicate.expressions:
                if isinstance(item, exp.Literal):
                    values.append(item.to_py())
            if info and values and info.index in dimension_by_col:
                filters.append((dimension_by_col[info.index], "equals", tuple(values)))
    return sorted(set(filters))


def is_distinct_count(node: exp.Count) -> bool:
    return isinstance(node.this, exp.Distinct) or "DISTINCT" in node.sql(dialect="sqlite").upper()


def aggregate_arg_column(node: exp.Expression) -> exp.Column | None:
    target = node.this
    if isinstance(target, exp.Distinct):
        target = next(iter(target.expressions), None)
    if isinstance(target, exp.Star):
        return None
    if isinstance(target, exp.Column):
        return target
    column = next(target.find_all(exp.Column), None) if target is not None else None
    return column


def mine_gold_sql(
    schema: dict[str, Any],
    questions: list[tuple[int, dict[str, Any]]],
    dimension_by_col: dict[int, str],
    entity_by_table: dict[str, str],
    by_name: dict[tuple[str, str], ColumnInfo],
) -> tuple[dict[tuple[Any, ...], MeasureUse], set[int], list[dict[str, str]]]:
    measures: dict[tuple[Any, ...], MeasureUse] = {}
    dimension_cols: set[int] = set()
    sql_relationships: list[dict[str, str]] = []
    aggregate_types = (
        (exp.Sum, "sum"),
        (exp.Avg, "avg"),
        (exp.Min, "min"),
        (exp.Max, "max"),
        (exp.Count, "count"),
    )

    for q_idx, question in questions:
        sql = question.get("SQL") or ""
        try:
            tree = sqlglot.parse_one(sql, read="sqlite")
        except sqlglot.errors.SqlglotError:
            continue
        aliases = build_aliases(tree)

        for column in tree.find_all(exp.Column):
            info = resolve_column(column, aliases, by_name)
            if info:
                dimension_cols.add(info.index)

        group_members = extract_group_members(tree, aliases, by_name, dimension_by_col, entity_by_table)
        filters = tuple(extract_simple_filters(tree, aliases, by_name, dimension_by_col))
        where_sql = tree.args["where"].sql(dialect="sqlite") if tree.args.get("where") else ""

        for cls, aggregation in aggregate_types:
            for node in tree.find_all(cls):
                column = aggregate_arg_column(node)
                distinct = isinstance(node, exp.Count) and is_distinct_count(node)
                if column is None:
                    entity = first_table_entity(tree, entity_by_table)
                    if not entity:
                        continue
                    column_member = "row_count"
                    column_name = "1"
                else:
                    info = resolve_column(column, aliases, by_name)
                    if not info:
                        # Ambiguous column — try with the first FROM table as context
                        from_entity = first_table_entity(tree, entity_by_table)
                        if from_entity:
                            from_table = {v: k for k, v in entity_by_table.items()}.get(from_entity, "")
                            info = by_name.get((from_table.lower(), column.name.lower()))
                    if not info:
                        continue
                    entity = entity_by_table[info.table]
                    column_member = info.member_name
                    column_name = info.column
                # Skip SUM/AVG on non-numeric columns
                if aggregation in ("sum", "avg") and info and info.column_type not in {
                    "integer", "int", "number", "real", "float",
                    "double", "decimal", "numeric",
                }:
                    continue
                key = (entity, aggregation, column_member, distinct, filters)
                use = measures.setdefault(
                    key,
                    MeasureUse(
                        entity=entity,
                        column=column_name,
                        column_member=column_member,
                        aggregation=aggregation,
                        distinct=distinct,
                        filters=filters,
                    ),
                )
                use.group_members.update(group_members)
                use.questions.add(q_idx)
                if where_sql:
                    use.where_examples[where_sql] += 1
                # Extract question-language terms as synonyms
                q_text = question.get("question", "")
                for token in re.sub(r"[^a-z ]", "", q_text.lower()).split():
                    if len(token) > 3 and token not in STOPWORDS:
                        use.question_terms.add(token)

        sql_relationships.extend(extract_join_relationships(tree, aliases, by_name, entity_by_table))

    return measures, dimension_cols, sql_relationships


def extract_join_relationships(
    tree: exp.Expression,
    aliases: dict[str, str],
    by_name: dict[tuple[str, str], ColumnInfo],
    entity_by_table: dict[str, str],
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for join in tree.find_all(exp.Join):
        on = join.args.get("on")
        if not on:
            continue
        for eq in on.find_all(exp.EQ):
            if not isinstance(eq.left, exp.Column) or not isinstance(eq.right, exp.Column):
                continue
            left = resolve_column(eq.left, aliases, by_name)
            right = resolve_column(eq.right, aliases, by_name)
            if not left or not right or left.table == right.table:
                continue
            output.append(
                {
                    "source": entity_by_table[left.table],
                    "target": entity_by_table[right.table],
                    "from": left.member_name,
                    "to": right.member_name,
                    "evidence": "gold_sql_join",
                }
            )
    return output


def yaml_scalar(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def write_yaml(value: Any, indent: int = 0) -> list[str]:
    prefix = " " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if item == []:
                lines.append(f"{prefix}{key}: []")
            elif item == {}:
                lines.append(f"{prefix}{key}: {{}}")
            elif isinstance(item, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.extend(write_yaml(item, indent + 2))
            else:
                lines.append(f"{prefix}{key}: {yaml_scalar(item)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                if not item:
                    lines.append(f"{prefix}- {{}}")
                    continue
                first_key = next(iter(item))
                first_value = item[first_key]
                rest = {key: val for key, val in item.items() if key != first_key}
                if first_value == []:
                    lines.append(f"{prefix}- {first_key}: []")
                elif first_value == {}:
                    lines.append(f"{prefix}- {first_key}: {{}}")
                elif isinstance(first_value, (dict, list)):
                    lines.append(f"{prefix}- {first_key}:")
                    lines.extend(write_yaml(first_value, indent + 4))
                else:
                    lines.append(f"{prefix}- {first_key}: {yaml_scalar(first_value)}")
                lines.extend(write_yaml(rest, indent + 2))
            elif isinstance(item, list):
                if not item:
                    lines.append(f"{prefix}- []")
                    continue
                lines.append(f"{prefix}-")
                lines.extend(write_yaml(item, indent + 2))
            else:
                lines.append(f"{prefix}- {yaml_scalar(item)}")
    else:
        lines.append(f"{prefix}{yaml_scalar(value)}")
    return lines


def semantic_type(column_type: str) -> str:
    if column_type in {"integer", "int", "number"}:
        return "number"
    if column_type in {"real", "float", "double", "decimal", "numeric"}:
        return "number"
    if column_type in {"date"}:
        return "date"
    if column_type in {"datetime", "timestamp"}:
        return "datetime"
    return "string"


def should_dimension(db_id: str, info: ColumnInfo, used_in_sql: bool) -> bool:
    if info.column_type in {"date", "datetime", "timestamp"}:
        return False
    if used_in_sql:
        return True
    if info.column_type not in {"text", "varchar", "string"}:
        return False
    cardinality = sqlite_cardinality(db_id, info.table, info.column)
    if not cardinality:
        return False
    total, distinct = cardinality
    if total == 0:
        return False
    return distinct <= 50 or distinct / max(total, 1) <= 0.1


def ai_context(db_id: str) -> str:
    domains = {
        "california_schools": "California school enrollment, FRPM eligibility, SAT scores, districts, counties, and charter attributes.",
        "card_games": "Magic card metadata, sets, rulings, foreign names, legality by format, and card attributes.",
        "codebase_community": "Stack Overflow-style community posts, users, votes, comments, badges, tags, and post history.",
        "debit_card_specializing": "Debit card customer segments, currencies, gas station transactions, products, and monthly fuel consumption.",
        "european_football_2": "European football matches, teams, leagues, countries, players, and player/team attributes.",
        "financial": "Banking accounts, clients, cards, loans, transactions, dispositions, districts, and orders.",
        "formula_1": "Formula 1 circuits, races, drivers, constructors, standings, lap times, pit stops, qualifying, and results.",
        "student_club": "Student club members, events, attendance, budgets, expenses, income, majors, and zip codes.",
        "superhero": "Superhero characters, powers, attributes, publishers, alignments, races, colors, and genders.",
        "thrombosis_prediction": "Patient demographics, diagnoses, examinations, admissions, and laboratory measurements for thrombosis prediction.",
        "toxicology": "Molecules, atoms, bonds, connectivity, and toxicology labels.",
    }
    return domains.get(db_id, f"BIRD benchmark database {db_id}.")


def build_model(db_id: str, schema: dict[str, Any], questions: list[tuple[int, dict[str, Any]]]) -> dict[str, Any]:
    by_table, by_index, by_name = schema_maps(schema)
    pk_indexes = flatten_primary_keys(schema.get("primary_keys", []))
    fk_pairs = [tuple(item) for item in schema.get("foreign_keys", []) if isinstance(item, list) and len(item) == 2]
    fk_indexes = {int(left) for left, _right in fk_pairs}
    referenced_indexes = {int(right) for _left, right in fk_pairs}
    entity_by_table = {table: member_name(table, set()) for table in schema["table_names_original"]}

    pre_dimension_by_col: dict[int, str] = {
        idx: info.member_name
        for idx, info in by_index.items()
        if idx not in pk_indexes and idx not in fk_indexes and idx not in referenced_indexes
    }
    mined, sql_dimension_cols, sql_relationships = mine_gold_sql(
        schema, questions, pre_dimension_by_col, entity_by_table, by_name
    )

    dimension_by_col: dict[int, str] = {}
    time_by_col: dict[int, str] = {}
    for idx, info in by_index.items():
        if info.column_type in {"date", "datetime", "timestamp"}:
            time_by_col[idx] = info.member_name
        elif (
            idx not in pk_indexes
            and idx not in fk_indexes
            and idx not in referenced_indexes
            and should_dimension(db_id, info, idx in sql_dimension_cols)
        ):
            dimension_by_col[idx] = info.member_name

    identifier_names_by_table: dict[str, set[str]] = defaultdict(set)
    for idx in pk_indexes | fk_indexes | referenced_indexes:
        if idx in by_index:
            info = by_index[idx]
            identifier_names_by_table[info.table].add(info.member_name)
    for idx, name in list(time_by_col.items()):
        info = by_index[idx]
        if name in identifier_names_by_table[info.table]:
            used = {item.member_name for item in by_table[info.table_idx]} | set(time_by_col.values())
            used.discard(name)
            time_by_col[idx] = member_name(f"time_{name}", used)

    valid_dimension_refs = {
        f"{entity_by_table[by_index[idx].table]}.{name}" for idx, name in dimension_by_col.items()
    } | {
        f"{entity_by_table[by_index[idx].table]}.{name}" for idx, name in time_by_col.items()
    }
    local_dimensions_by_entity: dict[str, set[str]] = defaultdict(set)
    for ref in valid_dimension_refs:
        entity_name, local_name = ref.split(".", 1)
        local_dimensions_by_entity[entity_name].add(local_name)

    entities: list[dict[str, Any]] = []
    default_time_ref: str | None = None
    view_entities: list[dict[str, Any]] = []
    certified_measure_names: dict[str, list[str]] = defaultdict(list)
    dimension_usage = Counter()
    for use in mined.values():
        for dimension in use.group_members:
            dimension_usage[dimension] += len(use.questions)

    for table_idx, table in enumerate(schema["table_names_original"]):
        entity_name = entity_by_table[table]
        identifiers = []
        for info in by_table[table_idx]:
            if info.index in pk_indexes or info.index in fk_indexes or info.index in referenced_indexes:
                identifiers.append(
                    {
                        "name": info.member_name,
                        "expr": info.column,
                        "type": semantic_type(info.column_type),
                        "primary_key": info.index in pk_indexes,
                    }
                )
        if not any(identifier["primary_key"] for identifier in identifiers) and identifiers:
            identifiers[0]["primary_key"] = True

        time_dimensions = []
        for idx, name in time_by_col.items():
            info = by_index[idx]
            if info.table != table:
                continue
            is_default = default_time_ref is None
            time_dimensions.append(
                {
                    "name": name,
                    "title": titleize(info.column),
                    "description": f"Time column {info.column} from {table}.",
                    "expr": info.column,
                    "type": semantic_type(info.column_type),
                    "default": is_default,
                    "granularities": ["day", "week", "month", "quarter", "year"],
                    "sensitivity": "public",
                    "public": True,
                }
            )
            if is_default:
                default_time_ref = f"{entity_name}.{name}"

        dimensions = []
        for idx, name in dimension_by_col.items():
            info = by_index[idx]
            if info.table != table:
                continue
            dimensions.append(
                {
                    "name": name,
                    "title": titleize(info.column),
                    "expr": info.column,
                    "type": semantic_type(info.column_type),
                    "synonyms": [titleize(info.column).lower()] if "_" in info.member_name else [],
                    "sensitivity": "public",
                    "public": True,
                }
            )

        measures = []
        used_measure_names: set[str] = set()
        for use in sorted(mined.values(), key=lambda item: (item.entity, item.aggregation, item.column_member)):
            if use.entity != entity_name or len(use.questions) <= 0:
                continue
            raw_name = (
                f"distinct_{use.column_member}_count"
                if use.distinct
                else f"{use.aggregation}_{use.column_member}"
            )
            name = member_name(raw_name, used_measure_names)
            filters = [
                {"member": member, "operator": operator, "values": list(values)}
                for member, operator, values in use.filters
                if member in local_dimensions_by_entity[entity_name]
            ]
            allowed_dimensions = sorted(
                dimension for dimension in use.group_members if dimension in valid_dimension_refs
            )
            measure = {
                "name": name,
                "title": titleize(name),
                "description": f"{use.aggregation.upper()} over {use.column} extracted from repeated BIRD gold SQL patterns.",
                "expr": use.column,
                "aggregation": use.aggregation,
                "type": "number",
                "status": "certified",
                "owner": OWNER,
                "public": True,
                "synonyms": sorted(set([
                    titleize(use.column).lower(),
                    use.column.lower(),
                    *use.question_terms,
                ])),
                "allowed_dimensions": allowed_dimensions,
                "filters": filters,
                "metadata": {
                    "source": "bird_gold_sql",
                    "question_count": len(use.questions),
                    "distinct": use.distinct,
                    "where_examples": [sql for sql, _count in use.where_examples.most_common(3)],
                },
            }
            measures.append(measure)
            certified_measure_names[entity_name].append(name)

        entity = {
            "name": entity_name,
            "title": titleize(table),
            "description": f"BIRD table {table} in the {db_id} database.",
            "status": "certified",
            "owner": OWNER,
            "public": False,
            "physical": {
                "table": table,
                "dialect": "sqlite",
                "grain": [identifier["name"] for identifier in identifiers if identifier["primary_key"]],
            },
            "identifiers": identifiers,
            "time_dimensions": time_dimensions,
            "dimensions": dimensions,
            "measures": measures,
            "segments": [],
        }
        entities.append(entity)
        includes = {
            "measures": certified_measure_names.get(entity_name, []),
            "dimensions": [dimension["name"] for dimension in dimensions],
            "time_dimensions": [dimension["name"] for dimension in time_dimensions],
            "segments": [],
        }
        view_entities.append({"join_path": entity_name, "includes": includes})

    relationships = build_relationships(schema, by_index, entity_by_table, sql_relationships)
    add_join_paths_to_view(view_entities, relationships)

    return {
        "version": 1,
        "domain": db_id,
        "metadata": {"source": "BIRD dev_20240627", "generator": Path(__file__).name},
        "entities": entities,
        "relationships": relationships,
        "views": [
            {
                "name": f"{db_id}_all",
                "title": f"{titleize(db_id)} All",
                "description": f"Governed semantic view for the BIRD {db_id} database.",
                "public": True,
                "ai_context": ai_context(db_id),
                "entities": view_entities,
                "folders": [
                    {
                        "name": "Certified Measures",
                        "includes": sorted(
                            f"{entity}.{measure}"
                            for entity, measures in certified_measure_names.items()
                            for measure in measures
                        ),
                    },
                    {
                        "name": "Frequently Used Dimensions",
                        "includes": [name for name, _count in dimension_usage.most_common(40)],
                    },
                ],
                "default_time_dimension": default_time_ref,
            }
        ],
        "policies": [],
    }


def build_relationships(
    schema: dict[str, Any],
    by_index: dict[int, ColumnInfo],
    entity_by_table: dict[str, str],
    sql_relationships: list[dict[str, str]],
) -> list[dict[str, Any]]:
    relationships: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    used_relationship_names: set[str] = set()

    def add(source: str, target: str, from_member: str, to_member: str, evidence: str) -> None:
        key = (source, target, from_member, to_member)
        relationship = relationships.setdefault(
            key,
            {
                "name": member_name(f"{source}_to_{target}_{from_member}_{to_member}", used_relationship_names),
                "source": source,
                "target": target,
                "from": f"{source}.{from_member}",
                "to": f"{target}.{to_member}",
                "relationship": "many_to_one",
                "join_type": "left",
                "row_preserving": "source",
                "fanout_risk": "low",
                "evidence": [],
            },
        )
        if evidence not in relationship["evidence"]:
            relationship["evidence"].append(evidence)

    for left, right in schema.get("foreign_keys", []):
        if left not in by_index or right not in by_index:
            continue
        source_col = by_index[int(left)]
        target_col = by_index[int(right)]
        add(
            entity_by_table[source_col.table],
            entity_by_table[target_col.table],
            source_col.member_name,
            target_col.member_name,
            "dev_tables_foreign_key",
        )

    for item in sql_relationships:
        add(item["source"], item["target"], item["from"], item["to"], item["evidence"])

    return sorted(relationships.values(), key=lambda item: item["name"])


def add_join_paths_to_view(view_entities: list[dict[str, Any]], relationships: list[dict[str, Any]]) -> None:
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for relationship in relationships:
        by_source[relationship["source"]].append(relationship)
    existing = {item["join_path"] for item in view_entities}
    for relationship in relationships:
        path = f"{relationship['source']}.{relationship['target']}"
        if path in existing:
            continue
        target = next((item for item in view_entities if item["join_path"] == relationship["target"]), None)
        if not target:
            continue
        view_entities.append(
            {
                "join_path": path,
                "prefix": True,
                "includes": target["includes"],
            }
        )
        existing.add(path)


def validate_model(path: Path) -> dict[str, Any]:
    semantic_src = Path.home() / "semantic_modeling" / "src"
    if semantic_src.exists() and str(semantic_src) not in sys.path:
        sys.path.insert(0, str(semantic_src))
    from semantic_engine.compiler.model_compiler import SemanticModelCompiler
    from semantic_engine.loader.yaml_loader import load_semantic_model_file

    model = load_semantic_model_file(path)
    snapshot = SemanticModelCompiler().compile(model)
    return snapshot.describe()


def generate_models(selected_db: str = "all") -> list[dict[str, Any]]:
    tables = {item["db_id"]: item for item in load_json(DEV_DIR / "dev_tables.json")}
    dev = load_json(DEV_DIR / "dev.json")
    questions_by_db: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for idx, question in enumerate(dev):
        questions_by_db[question["db_id"]].append((idx, question))

    db_ids = sorted(tables) if selected_db == "all" else [selected_db]
    summaries = []
    for db_id in db_ids:
        model = build_model(db_id, tables[db_id], questions_by_db[db_id])
        output_path = OUTPUT_DIR / db_id / "model.yml"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(write_yaml(model)) + "\n", encoding="utf-8")
        summary = validate_model(output_path)
        summary.update({"db_id": db_id, "path": str(output_path.relative_to(ROOT))})
        summaries.append(summary)
        print(
            f"validated {db_id}: entities={summary['entities']} "
            f"relationships={summary['relationships']} catalog_members={summary['catalog_members']}"
        )
    return summaries


def execute_sql(db_path: Path, sql: str, parameters: list[Any] | None = None) -> tuple[bool, list[tuple[Any, ...]], str]:
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(sql, parameters or []).fetchall()
        return True, rows, ""
    except sqlite3.Error as exc:
        return False, [], str(exc)


def semantic_only_eval(indices_path: Path | None, subset: int | None, baseline_path: Path | None) -> dict[str, Any]:
    semantic_src = Path.home() / "semantic_modeling" / "src"
    if semantic_src.exists() and str(semantic_src) not in sys.path:
        sys.path.insert(0, str(semantic_src))
    from semantic_engine.pipeline import SemanticPipeline

    dev = load_json(DEV_DIR / "dev.json")
    if indices_path:
        indices = load_json(indices_path)
    else:
        indices = list(range(len(dev)))
    if subset:
        indices = indices[:subset]

    pipelines: dict[str, SemanticPipeline] = {}
    details = []
    route_counts = Counter()
    passed = 0
    for position, idx in enumerate(indices, start=1):
        question = dev[idx]
        db_id = question["db_id"]
        pipelines.setdefault(db_id, SemanticPipeline(OUTPUT_DIR / db_id / "model.yml"))
        result = pipelines[db_id].process(question["question"])
        route = str(result.route)
        route_counts[route] += 1
        sql = result.compiled_query.sql if result.compiled_query else ""
        params = result.compiled_query.parameters if result.compiled_query else []
        db_path = DB_ROOT / db_id / f"{db_id}.sqlite"
        pred_ok, pred_rows, pred_error = execute_sql(db_path, sql, params) if sql else (False, [], "No deterministic SQL")
        gold_ok, gold_rows, gold_error = execute_sql(db_path, question["SQL"])
        match = bool(sql and pred_ok and gold_ok and set(pred_rows) == set(gold_rows))
        passed += int(match)
        details.append(
            {
                "idx": idx,
                "db_id": db_id,
                "difficulty": question.get("difficulty"),
                "route": route,
                "match": match,
                "sql": sql,
                "error": "" if match else (pred_error or gold_error),
                "gold_sql": question["SQL"],
            }
        )
        if position % 50 == 0 or position == len(indices):
            print(f"  [{position}/{len(indices)}] semantic-only EX={passed}/{position} ({passed / position * 100:.1f}%)")

    comparison = compare_baseline(indices, baseline_path)
    report = {
        "mode": "semantic_only",
        "total": len(indices),
        "passed": passed,
        "ex": round(passed / max(len(indices), 1) * 100, 2),
        "route_counts": dict(route_counts),
        "baseline": comparison,
        "results": details,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / "semantic_engine_eval.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)}")
    if comparison:
        print(
            f"baseline {comparison['path']}: EX={comparison['ex']}% "
            f"({comparison['passed']}/{comparison['total']})"
        )
    return report


def compare_baseline(indices: list[int], baseline_path: Path | None) -> dict[str, Any] | None:
    def display_path(path: Path) -> str:
        resolved = path.resolve()
        try:
            return str(resolved.relative_to(ROOT))
        except ValueError:
            return str(path)

    if baseline_path is None:
        candidates = sorted((RESULTS_DIR / "full_benchmarks").glob("*.json"))
        same_size = []
        for path in candidates:
            try:
                data = load_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("total") == len(indices) and "results" in data:
                same_size.append(path)
        baseline_path = same_size[-1] if same_size else None
    if baseline_path is None or not baseline_path.exists():
        return None
    data = load_json(baseline_path)
    rows = data.get("results", []) if isinstance(data, dict) else []
    matches_by_idx = {int(row.get("idx")): bool(row.get("match")) for row in rows if "idx" in row}
    baseline_total = int(data.get("total", 0))
    sample_local_indices = (
        baseline_total == len(indices)
        and matches_by_idx
        and max(matches_by_idx) == baseline_total - 1
        and max(indices or [0]) > baseline_total - 1
    )
    if matches_by_idx and not sample_local_indices:
        matched = [matches_by_idx.get(idx, False) for idx in indices]
        passed = sum(matched)
        total = len(indices)
    else:
        passed = int(data.get("passed", 0))
        total = baseline_total
    return {
        "path": display_path(baseline_path),
        "total": total,
        "passed": passed,
        "ex": round(passed / max(total, 1) * 100, 2),
        "config": data.get("config"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="all", help="BIRD db_id to generate, or all")
    parser.add_argument("--generate", action="store_true", help="Generate and validate semantic engine models")
    parser.add_argument("--eval", action="store_true", help="Run local semantic-only execution evaluation")
    parser.add_argument("--indices", type=Path, default=None, help="Optional JSON list of dev question indices")
    parser.add_argument("--subset", type=int, default=None, help="Limit evaluation to first N selected questions")
    parser.add_argument("--baseline", type=Path, default=None, help="Previous LLM-only result JSON for comparison")
    args = parser.parse_args()

    if not args.generate and not args.eval:
        args.generate = True

    if args.generate:
        summaries = generate_models(args.db)
        summary_path = OUTPUT_DIR / "_summary.json"
        summary_path.write_text(json.dumps(summaries, indent=2), encoding="utf-8")
        print(f"wrote {summary_path.relative_to(ROOT)}")
    if args.eval:
        semantic_only_eval(args.indices, args.subset, args.baseline)


if __name__ == "__main__":
    main()
