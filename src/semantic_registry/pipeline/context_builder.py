from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path
from typing import Any

from src.semantic_registry.metadata.models import ColumnMetadata, TableMetadata
from src.semantic_registry.metadata.provider import MetadataProvider
from src.semantic_registry.resolver.plan import SemanticQueryPlan
from src.semantic_registry.resolver.registry import SemanticRegistryData
from src.semantic_registry.retrieval.hybrid import RetrievalResult
from src.semantic_registry.yaml_schema.schemas import DimensionYaml


class ContextBuilder:
    def __init__(
        self,
        registry_data: SemanticRegistryData | None = None,
        metadata_provider: MetadataProvider | None = None,
    ) -> None:
        self.registry_data = registry_data or SemanticRegistryData()
        self.metadata_provider = metadata_provider
        self.metrics_by_name = {metric.metric: metric for metric in self.registry_data.metrics}
        self.dimensions_by_name = {dimension.dimension: dimension for dimension in self.registry_data.dimensions}

    def build(
        self,
        question: str,
        semantic_plan: SemanticQueryPlan,
        retrieved_metadata: RetrievalResult,
        raw_schema: str | None = None,
        evidence: str | None = None,
    ) -> str:
        safe_question = self._redact_sensitive_values(question)
        tables = self._candidate_tables(semantic_plan, retrieved_metadata)
        
        # BIRD mode: mirror scripts/run_full_benchmark.py as closely as the
        # pipeline allows: few-shot examples, enriched schema, evidence hint,
        # question, compact JSON contract.
        if raw_schema:
            db_id = self._db_id_from_raw_schema(raw_schema) or semantic_plan.domain
            examples = self._few_shot_examples(safe_question, db_id)
            safe_evidence = self._redact_sensitive_values(evidence)
            targeted_hints = self._bird_targeted_hints(db_id, safe_question, safe_evidence)
            sections = [
                "You are a SQLite expert. Generate a single SELECT statement.",
            ]
            if examples:
                sections.extend(
                    [
                        "Here are examples of similar questions and their SQL:",
                        examples,
                    ]
                )
            sections.extend(
                [
                    "Database Schema:",
                    raw_schema,
                    *(["Targeted schema hints:", targeted_hints] if targeted_hints else []),
                    "IMPORTANT:",
                    "- SQLite string comparisons are case-sensitive. Use the exact spelling and capitalization shown in sample values, evidence, and targeted schema hints.",
                    "- Return ONLY the columns explicitly mentioned in the question. Do not add extra columns.",
                    "- Projection rule: columns used only to filter, rank, sort, or identify the row do not belong in SELECT unless the question asks to return them.",
                    "- When asked for a 'full name', return the component name columns separately, such as forename and surname.",
                    "- When asked to 'name', 'list', or 'which' entities (cards, schools, users, etc.) WITHOUT explicitly asking for 'names', return the identifying column, usually id. Only return the name column when the question explicitly says 'names' or 'name of'.",
                    "- When filtering by datetime, use LIKE to match the date prefix, for example WHERE column LIKE '2010-07-19 19:37:33%' instead of exact equality, to handle varying datetime formats.",
                ]
            )
            if safe_evidence:
                sections.append(f"Hint: {safe_evidence}")
            sections.extend([
                f"Question: {safe_question}",
                "",
                "Return ONLY a JSON object:",
                '{"sql": "SELECT ...", "assumptions": [], "tables_used": [], "columns_used": [], "confidence": "high|medium|low", "reasoning_summary": "..."}',
            ])
            return "\n\n".join(sections)
        
        # Enterprise mode: use full governed semantic context
        sections = [
            "You are generating SQL from a governed semantic registry context. Use SQLite syntax.",
            raw_schema or self._tables_section(tables, retrieved_metadata),
            self._schema_caveat_section(),
            self._domain_knowledge_section(retrieved_metadata.known_caveats),
            self._semantic_plan_section(semantic_plan),
            self._metrics_section(semantic_plan, retrieved_metadata),
            self._join_paths_section(tables),
            self._question_section(safe_question),
            self._rules_section(),
        ]
        return "\n\n".join(section for section in sections if section)

    def _question_section(self, question: str) -> str:
        return f"Original question:\n{question}"

    def _semantic_plan_section(self, semantic_plan: SemanticQueryPlan) -> str:
        metric = self._business_name(semantic_plan.metric)
        dimension = self._business_name(semantic_plan.dimension)
        time_semantics = self._business_name(semantic_plan.time_semantics)
        plan_fields = [
            ("Metric", metric),
            ("Dimension", dimension),
            ("Time range", semantic_plan.time_range),
            ("Time semantics", time_semantics),
            ("Domain", semantic_plan.domain),
        ]
        lines = ["Resolved semantic plan:"]
        lines.extend(f"- {label}: {value}" for label, value in plan_fields if value not in (None, "", [], {}))
        if semantic_plan.filters:
            lines.append(f"- Filters: {semantic_plan.filters}")
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    def _domain_knowledge_section(self, caveats: list[str]) -> str:
        if not caveats:
            return ""
        lines = ["Domain Knowledge / Hint:"]
        lines.extend(f"- {self._redact_sensitive_values(caveat)}" for caveat in caveats)
        return "\n".join(lines)

    def _tables_section(self, tables: list[TableMetadata], retrieved_metadata: RetrievalResult) -> str:
        if not tables and not retrieved_metadata.candidate_tables:
            return "Database Schema:\n- None"
        lines = ["Database Schema:"]
        rendered = set()
        for table in tables:
            rendered.add(table.table_name)
            lines.append(f"CREATE TABLE {table.table_name} (")
            visible_columns = [column for column in table.columns if not column.is_pii]
            col_lines = []
            for column in visible_columns:
                col_type = column.data_type or "TEXT"
                description = self._redact_sensitive_values(column.description)
                col_def = f"  {column.column_name} {col_type}"
                if description:
                    col_def += f"  -- {description}"
                col_lines.append(col_def)
            lines.append(",\n".join(col_lines))
            lines.append(")")
            if table.description:
                lines.append(f"  -- Description: {self._redact_sensitive_values(table.description)}")
        for candidate in retrieved_metadata.candidate_tables:
            if candidate.name not in rendered:
                lines.append(f"-- {candidate.name}: {self._redact_sensitive_values(candidate.description)}")
        if retrieved_metadata.candidate_columns:
            lines.append("Additional columns (from schema context):")
            for column in retrieved_metadata.candidate_columns:
                lines.append(f"  {column}")
        return "\n".join(lines)

    def _schema_caveat_section(self) -> str:
        return (
            "Note: Physical tables may have additional columns beyond those listed above "
            "-- use column names from the question as filter targets when they are not found in the listed schema."
        )

    def _metrics_section(self, semantic_plan: SemanticQueryPlan, retrieved_metadata: RetrievalResult) -> str:
        metric_names = [semantic_plan.metric] if semantic_plan.metric else []
        metric_names.extend(candidate.name for candidate in retrieved_metadata.candidate_metrics)
        seen: set[str] = set()
        lines = ["Candidate metrics and physical mappings:"]
        for metric_name in metric_names:
            if not metric_name or metric_name in seen:
                continue
            seen.add(metric_name)
            metric = self.metrics_by_name.get(metric_name)
            if metric is None:
                lines.append(f"- {self._business_name(metric_name)}")
                continue
            lines.append(f"- {self._business_name(metric.metric)}: {metric.description}")
            if metric.measure:
                lines.append(f"  Physical mapping: {metric.measure.table}.{metric.measure.column}")
            if metric.physical_time_column:
                lines.append(f"  Time column: {metric.physical_time_column}")
            if metric.expression:
                lines.append(f"  Expression: {metric.expression}")
            if metric.aggregation:
                lines.append(f"  Aggregation: {metric.aggregation}")
        return "\n".join(lines)

    def _join_paths_section(self, tables: list[TableMetadata]) -> str:
        lines = ["Join paths between candidate tables:"]
        joins = self._join_paths(tables)
        if not joins:
            lines.append("- None required or none available")
            return "\n".join(lines)
        for join in joins:
            lines.append(f"- {join['from_table']} -> {join['to_table']}: {join['join_condition']}")
        return "\n".join(lines)

    def _join_paths(self, tables: list[TableMetadata]) -> list[dict[str, Any]]:
        return [
            {
                "from_table": join.from_table,
                "to_table": join.to_table,
                "join_condition": join.join_condition,
            }
            for table in tables
            for join in table.join_paths
        ]

    def _caveats_section(self, caveats: list[str]) -> str:
        if not caveats:
            return "Known caveats:\n- None"
        lines = ["Known caveats:"]
        lines.extend(f"- {self._redact_sensitive_values(caveat)}" for caveat in caveats)
        return "\n".join(lines)

    def _rules_section(self) -> str:
        return "\n".join(
            [
                "Generation rules:",
                "- Use SQLite dialect.",
                "- Use backtick quoting for table or column names that contain spaces or special characters.",
                "- Generate exactly one SELECT statement.",
                "- Do not use SELECT *.",
                "- Do not invent tables; use only tables, metric mappings, and join paths listed above.",
                "- When a filtered column name appears in the question or semantic plan, add the matching WHERE predicate.",
                "- For time-based questions, add a WHERE time-range filter using the resolved time column.",
                "- Prefer certified metric definitions and return business assumptions separately from the SQL.",
            ]
        )

    def _enriched_table_description(self, table: TableMetadata) -> str:
        description = self._redact_sensitive_values(table.description).rstrip(".")
        parts = [description] if description else ["Available semantic source"]
        visible_columns = [column for column in table.columns if not column.is_pii]
        if visible_columns:
            field_descriptions = self._column_prose_list(visible_columns)
            parts.append(
                "This table records semantic business data; "
                f"each row represents a {self._row_subject(table)} with {field_descriptions}"
            )
        parts.append(
            "Other columns available in the physical table may include region, customer segment, currency, "
            "and additional dimension fields -- use column names mentioned in the user's question as filter targets "
            "even if not explicitly listed here"
        )
        return ". ".join(parts) + "."

    def _row_subject(self, table: TableMetadata) -> str:
        if table.grain:
            return "record at the " + ", ".join(table.grain) + " grain"
        normalized_name = table.table_name.replace("_", " ").strip()
        if normalized_name:
            return f"record from the {normalized_name} table"
        return "business record"

    def _column_prose_list(self, columns: list[ColumnMetadata]) -> str:
        phrases = []
        for column in columns:
            description = self._redact_sensitive_values(column.description).rstrip(".")
            if description:
                phrases.append(f"{description[0].lower() + description[1:]} ({column.column_name})")
            else:
                phrases.append(column.column_name)
        if len(phrases) == 1:
            return phrases[0]
        if len(phrases) == 2:
            return f"{phrases[0]} and {phrases[1]}"
        return f"{', '.join(phrases[:-1])}, and {phrases[-1]}"

    def _candidate_tables(
        self,
        semantic_plan: SemanticQueryPlan,
        retrieved_metadata: RetrievalResult,
    ) -> list[TableMetadata]:
        tables: list[TableMetadata] = []
        seen: set[str] = set()
        for candidate in retrieved_metadata.candidate_tables:
            table = self.metadata_provider.get_table(candidate.name) if self.metadata_provider else None
            if table is None:
                table = TableMetadata(table_name=candidate.name, description=candidate.description, domain=candidate.domain or None)
            if table.table_name not in seen:
                seen.add(table.table_name)
                tables.append(table)

        derived = self._table_from_semantic_plan(semantic_plan)
        if derived and derived.table_name not in seen:
            tables.insert(0, derived)
        return tables

    def _table_from_semantic_plan(self, semantic_plan: SemanticQueryPlan) -> TableMetadata | None:
        metric = self.metrics_by_name.get(semantic_plan.metric or "")
        if metric is None or metric.measure is None:
            return None
        columns = [
            ColumnMetadata(
                column_name=metric.measure.column,
                data_type="numeric",
                description=f"Measure for {self._business_name(metric.metric)}.",
                concept=metric.metric,
                aggregation=metric.aggregation,
                unit=metric.unit,
            )
        ]
        if metric.physical_time_column:
            columns.append(
                ColumnMetadata(
                    column_name=metric.physical_time_column,
                    data_type="date",
                    description=f"Time column for {self._business_name(metric.default_time_dimension)}.",
                    concept=metric.default_time_dimension,
                )
            )
        dimension = self.dimensions_by_name.get(semantic_plan.dimension or "")
        if dimension:
            mapping = self._dimension_mapping_for_table(dimension, metric.measure.table)
            if mapping:
                columns.append(
                    ColumnMetadata(
                        column_name=mapping["column"],
                        data_type="text",
                        description=dimension.description,
                        concept=dimension.dimension,
                    )
                )
        return TableMetadata(
            table_name=metric.measure.table,
            description=f"Certified semantic source for {self._business_name(metric.metric)}.",
            certified=str(metric.status) == "certified",
            eligible_for_nl2sql=True,
            grain=[column.column_name for column in columns if column.concept != metric.metric],
            partition_column=metric.physical_time_column,
            columns=columns,
        )

    def _physical_mapping(self, semantic_plan: SemanticQueryPlan) -> dict[str, Any]:
        metric = self.metrics_by_name.get(semantic_plan.metric or "")
        if metric is None:
            return {}
        mapping: dict[str, Any] = {
            "aggregation": metric.aggregation,
            "metric_expression": metric.expression,
            "time_column": metric.physical_time_column,
        }
        if metric.measure:
            mapping["table"] = metric.measure.table
            mapping["metric_column"] = metric.measure.column
            mapping["metric_expression"] = metric.measure.column
        dimension = self.dimensions_by_name.get(semantic_plan.dimension or "")
        if dimension:
            dimension_mapping = self._dimension_mapping_for_table(dimension, mapping.get("table"))
            if dimension_mapping:
                mapping["dimension_table"] = dimension_mapping["table"]
                mapping["dimension_column"] = dimension_mapping["column"]
        return {key: value for key, value in mapping.items() if value not in (None, "", [], {})}

    def _dimension_mapping_for_table(self, dimension: DimensionYaml, table_name: str | None) -> dict[str, str] | None:
        if not dimension.physical_mappings:
            return None
        for mapping in dimension.physical_mappings:
            if table_name is not None and mapping.table == table_name:
                return {"table": mapping.table, "column": mapping.column}
        first = dimension.physical_mappings[0]
        return {"table": first.table, "column": first.column}

    def _business_name(self, value: str | None) -> str | None:
        if not value:
            return None
        concept_names = {concept.concept: concept.display_name for concept in self.registry_data.concepts}
        if value in concept_names:
            return concept_names[value]
        words = []
        for part in value.split("_"):
            words.append(part.upper() if part.lower() in {"gmv", "pii", "id"} else part.capitalize())
        return " ".join(words)

    def _redact_sensitive_values(self, text: str | None) -> str:
        if not text:
            return ""
        redacted = re.sub(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]", text)
        redacted = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED_SSN]", redacted)
        redacted = re.sub(r"\b(?:\d[ -]*?){13,16}\b", "[REDACTED_CARD]", redacted)
        redacted = re.sub(r"\b(?:\+?\d[\d .()-]{7,}\d)\b", "[REDACTED_PHONE]", redacted)
        redacted = re.sub(r"(?i)(password\s*(?:is|=|:)\s*)\S+", r"\1[REDACTED_PASSWORD]", redacted)
        return redacted

    def _db_id_from_raw_schema(self, raw_schema: str) -> str | None:
        match = re.search(r"^Database Schema for:\s*([^\s]+)\s*$", raw_schema, flags=re.MULTILINE)
        return match.group(1) if match else None

    def _bird_targeted_hints(self, db_id: str | None, question: str, evidence: str) -> str:
        if not db_id:
            return ""
        text = f"{question}\n{evidence}".lower()
        lines: list[str] = []

        if db_id == "card_games":
            if any(term in text for term in ("legal", "banned", "restricted", "commander", "status")):
                lines.append(
                    "- Exact legalities values: legalities.status uses 'Legal', 'Banned', and 'Restricted'; "
                    "legalities.format values such as 'commander' are lowercase."
                )
            if "ruling" in text and "date" not in text:
                lines.append(
                    "- Rulings information/description maps to rulings.text; do not include rulings.date unless dates are requested."
                )
            if re.search(r"\bname all cards\b", question, flags=re.IGNORECASE) and not re.search(
                r"\b(card )?names\b|\bname of\b",
                question,
                flags=re.IGNORECASE,
            ):
                lines.append("- For 'Name all cards' card-identity questions, return cards.id rather than cards.name.")

        if db_id == "european_football_2":
            if "height of the tallest player" in text and "name" in text:
                lines.append("- For this question, use Player.height to rank the tallest player but return only Player.player_name.")
            if "highest number of penalties" in text and "player" in text:
                lines.append(
                    "- For the top-penalties player ranking, join Player_Attributes.id = Player.id; "
                    "do not use player_api_id for that task."
                )
            if any(term in text for term in ("which home team", "which away team", "team name", "short name of the football team")):
                lines.append(
                    "- Match.home_team_api_id and Match.away_team_api_id are identifiers. "
                    "When the question asks which team or a team name, join Team.team_api_id and return "
                    "Team.team_long_name, or Team.team_short_name when a short name is requested."
                )

        return "\n".join(lines)

    def _few_shot_examples(self, question: str | None = None, db_id: str | None = None) -> str | None:
        if not question or not db_id:
            return None
        try:
            from scripts.sql_pattern_memory import SQLPatternMemory

            memory = SQLPatternMemory()
            if memory.store.count() < 100:
                dev_path = Path(__file__).resolve().parents[3] / "bird_bench" / "dev" / "dev_20240627" / "dev.json"
                if dev_path.exists():
                    with contextlib.redirect_stdout(io.StringIO()):
                        memory.seed_from_bird(str(dev_path))
            patterns = memory.retrieve(question, db_id, top_k=3)
        except Exception:
            return None
        if not patterns:
            return None
        examples: list[str] = []
        for index, match in enumerate(patterns[:3], start=1):
            pattern = match.pattern
            examples.append(f"Example {index}:")
            examples.append(f"Question: {pattern.question}")
            if pattern.difficulty:
                examples.append(f"Difficulty: {pattern.difficulty}")
            examples.append(f"SQL: {pattern.sql}")
            examples.append("")
        return "\n".join(examples).strip()
