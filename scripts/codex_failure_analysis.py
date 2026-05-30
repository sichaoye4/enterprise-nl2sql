#!/usr/bin/env python3
"""BIRD-SQL gold-query failure and difficulty analysis.

This script intentionally uses only Python stdlib modules so it can run in the
benchmark environment without extra setup.
"""

import json
import math
import os
import re
import sqlite3
from collections import Counter, defaultdict


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
DEV_PATH = os.path.join(REPO_ROOT, "bird_bench", "dev", "dev_20240627", "dev.json")
DB_ROOT = os.path.join(
    REPO_ROOT,
    "bird_bench",
    "dev",
    "dev_20240627",
    "databases",
    "dev_databases",
)
RESULTS_DIR = os.path.join(REPO_ROOT, "bird_bench", "results")
BENCHMARK_DIR = os.path.join(RESULTS_DIR, "full_benchmarks")
REPORT_PATH = os.path.join(RESULTS_DIR, "failure_analysis_report.md")
CLASSIFICATIONS_PATH = os.path.join(RESULTS_DIR, "question_classifications.json")
SQLITE_PROGRESS_OPS = 1000
SQLITE_PROGRESS_LIMIT = 100000
ROW_FETCH_LIMIT = 10000

DIFF_SCORE = {"simple": 1, "moderate": 2, "challenging": 3}
DIFF_ORDER = ["simple", "moderate", "challenging"]

AGG_RE = re.compile(r"\b(COUNT|SUM|AVG|MAX|MIN)\s*\(", re.IGNORECASE)
SQL_FUNC_RE = re.compile(
    r"\b(SUBSTR|SUBSTRING|REPLACE|UPPER|LOWER|TRIM|LTRIM|RTRIM|LENGTH|INSTR)\s*\(",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"\b(DATE|DATETIME|JULIANDAY|STRFTIME|TIME)\s*\(", re.IGNORECASE)
MATH_RE = re.compile(r"(?<![<>=!])[-+*/%](?![<>=])")


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def strip_sql_comments(sql):
    sql = re.sub(r"--.*?(?=\n|$)", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return sql


def mask_string_literals(sql):
    """Replace single/double-quoted strings with placeholders for regex scans."""
    return re.sub(r"('([^']|'')*'|\"([^\"]|\"\")*\")", " 'STR' ", sql)


def normalized_sql(sql):
    return re.sub(r"\s+", " ", strip_sql_comments(sql)).strip()


def upper_no_literals(sql):
    return mask_string_literals(normalized_sql(sql)).upper()


def count_where_conditions(sql_upper):
    count = 0
    for where_clause in re.findall(
        r"\bWHERE\b(.*?)(?=\bGROUP\s+BY\b|\bHAVING\b|\bORDER\s+BY\b|\bLIMIT\b|\bUNION\b|$)",
        sql_upper,
        flags=re.DOTALL,
    ):
        and_or = len(re.findall(r"\bAND\b|\bOR\b", where_clause))
        count += 1 + and_or
    return count


def count_subquery_levels(sql_upper):
    return max(0, len(re.findall(r"\bSELECT\b", sql_upper)) - 1)


def count_tables(sql):
    """Approximate table count from FROM/JOIN clauses, ignoring subquery aliases."""
    s = upper_no_literals(sql)
    tables = []
    for match in re.finditer(r"\b(?:FROM|JOIN)\s+([`\"\[]?[\w.]+[`\"\]]?)", s):
        name = match.group(1).strip("`\"[]")
        if name not in {"SELECT"}:
            tables.append(name)
    return len(set(tables)) if tables else 0


def has_order_by_calculated(sql_upper):
    for order_clause in re.findall(
        r"\bORDER\s+BY\b(.*?)(?=\bLIMIT\b|\bUNION\b|$)",
        sql_upper,
        flags=re.DOTALL,
    ):
        if AGG_RE.search(order_clause) or re.search(r"\bCAST\s*\(", order_clause):
            return True
        if MATH_RE.search(order_clause):
            return True
    return False


def classify_pattern(features):
    if features["has_subquery"]:
        if features["has_join"] or features["has_group_by"] or features["has_case"]:
            return "complex_multi"
        return "subquery"
    if features["has_join"] and features["has_agg"]:
        return "join_agg"
    if features["has_join"]:
        return "join_simple"
    if features["has_agg"] and features["has_group_by"] and features["has_having"]:
        return "agg_group_having"
    if features["has_agg"] and features["has_group_by"]:
        return "agg_group_by"
    if features["has_agg"] and features["has_where"]:
        return "agg_filter"
    if features["has_agg"]:
        return "simple_agg"
    return "simple_select"


def classify_sql(sql):
    s = upper_no_literals(sql)
    join_count = len(re.findall(r"\bJOIN\b", s))
    agg_count = len(AGG_RE.findall(s))
    subquery_levels = count_subquery_levels(s)
    where_conditions = count_where_conditions(s)

    features = {
        "has_join": join_count > 0,
        "has_subquery": subquery_levels > 0,
        "has_agg": agg_count > 0,
        "has_group_by": bool(re.search(r"\bGROUP\s+BY\b", s)),
        "has_having": bool(re.search(r"\bHAVING\b", s)),
        "has_order_by": bool(re.search(r"\bORDER\s+BY\b", s)),
        "has_limit": bool(re.search(r"\bLIMIT\b", s)),
        "has_distinct": bool(re.search(r"\bDISTINCT\b", s)),
        "has_case": bool(re.search(r"\bCASE\b", s)),
        "has_cast": bool(re.search(r"\bCAST\s*\(", s)),
        "has_in_subquery": bool(re.search(r"\bIN\s*\(\s*SELECT\b", s)),
        "has_between": bool(re.search(r"\bBETWEEN\b", s)),
        "has_like": bool(re.search(r"\bLIKE\b", s)),
        "has_string_op": bool(SQL_FUNC_RE.search(s) or "||" in s),
        "has_date_op": bool(DATE_RE.search(s)),
        "has_math": bool(MATH_RE.search(s)),
        "has_where": bool(re.search(r"\bWHERE\b", s)),
        "has_order_by_calculated": has_order_by_calculated(s),
    }
    features["table_count"] = count_tables(sql)
    features["join_count"] = join_count
    features["subquery_levels"] = subquery_levels
    features["agg_count"] = agg_count
    features["where_condition_count"] = where_conditions

    score = 1
    score += join_count
    score += subquery_levels
    score += agg_count
    score += where_conditions // 3
    score += 1 if features["has_having"] else 0
    score += 1 if features["has_case"] else 0
    score += 1 if features["has_distinct"] and features["has_join"] else 0
    score += 1 if features["has_order_by_calculated"] else 0
    features["complexity_score"] = max(1, min(10, score))
    features["pattern_type"] = classify_pattern(features)
    return features


def find_db_path(db_id):
    path = os.path.join(DB_ROOT, db_id, db_id + ".sqlite")
    if os.path.exists(path):
        return path
    fallback = os.path.join(DB_ROOT, db_id, "database.sqlite")
    if os.path.exists(fallback):
        return fallback
    return path


def execute_gold_sql(db_id, sql):
    db_path = find_db_path(db_id)
    if not os.path.exists(db_path):
        return {
            "ok": False,
            "row_count": None,
            "row_count_capped": False,
            "error_type": "missing_database",
            "error": "Database file not found: " + db_path,
        }

    uri = "file:{}?mode=ro".format(os.path.abspath(db_path))
    try:
        con = sqlite3.connect(uri, uri=True, timeout=30)
        progress_calls = [0]

        def progress_handler():
            progress_calls[0] += 1
            if progress_calls[0] > SQLITE_PROGRESS_LIMIT:
                return 1
            return 0

        con.set_progress_handler(progress_handler, SQLITE_PROGRESS_OPS)
        cur = con.cursor()
        cur.execute(sql)
        rows = cur.fetchmany(ROW_FETCH_LIMIT + 1)
        row_count_capped = len(rows) > ROW_FETCH_LIMIT
        row_count = ROW_FETCH_LIMIT if row_count_capped else len(rows)
        con.close()
        return {
            "ok": True,
            "row_count": row_count,
            "row_count_capped": row_count_capped,
            "error_type": None,
            "error": None,
        }
    except sqlite3.Error as exc:
        try:
            con.close()
        except Exception:
            pass
        return {
            "ok": False,
            "row_count": None,
            "row_count_capped": False,
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        }


def ascii_bar(value, max_value, width=28):
    if max_value <= 0:
        return ""
    filled = int(round(width * value / max_value))
    return "#" * filled + "." * (width - filled)


def table(headers, rows):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    out = [sep]
    out.append("| " + " | ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers)) + " |")
    out.append(sep)
    for row in rows:
        out.append("| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)) + " |")
    out.append(sep)
    return "\n".join(out)


def pct(n, d):
    return "0.0%" if d == 0 else "{:.1f}%".format(n * 100.0 / d)


def mean(values):
    return sum(values) / len(values) if values else 0.0


def pearson(xs, ys):
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    mx = mean(xs)
    my = mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mx) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - my) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return 0.0
    return num / (den_x * den_y)


def phi(feature_present, challenging):
    n11 = sum(1 for f, c in zip(feature_present, challenging) if f and c)
    n10 = sum(1 for f, c in zip(feature_present, challenging) if f and not c)
    n01 = sum(1 for f, c in zip(feature_present, challenging) if not f and c)
    n00 = sum(1 for f, c in zip(feature_present, challenging) if not f and not c)
    den = math.sqrt((n11 + n10) * (n01 + n00) * (n11 + n01) * (n10 + n00))
    return 0.0 if den == 0 else (n11 * n00 - n10 * n01) / den


def load_benchmark_summary():
    summaries = []
    if not os.path.isdir(BENCHMARK_DIR):
        return summaries
    for name in sorted(os.listdir(BENCHMARK_DIR)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(BENCHMARK_DIR, name)
        try:
            data = load_json(path)
        except Exception:
            continue
        summaries.append(
            {
                "file": name,
                "total": data.get("total"),
                "passed": data.get("passed"),
                "ex": data.get("ex"),
                "time_min": data.get("time_min"),
            }
        )
    return summaries


def difficulty_counts(items):
    c = Counter(item["difficulty"] for item in items)
    return "{}/{}/{}".format(c.get("simple", 0), c.get("moderate", 0), c.get("challenging", 0))


def representative_score(item, wanted):
    f = item["features"]
    score = item["difficulty_score"] * 10 + f["complexity_score"]
    if wanted == "multi_table_join_aggregation":
        score += f["join_count"] * 5 + f["agg_count"] * 4 + f["table_count"]
    elif wanted == "filter_value_mismatch":
        strings = re.findall(r"'([^']+)'", item["sql"])
        score += len(strings) * 2
        score += sum(6 for value in strings if " " in value and len(value) >= 10)
        if any(value == "Continuation School" for value in strings):
            score += 100
    elif wanted == "missing_order_by_limit":
        score += 15 if f["has_order_by"] and f["has_limit"] else 0
    elif wanted == "where_vs_having":
        score += 20 if f["has_having"] else 0
    elif wanted == "cast_type_conversion":
        score += 20 if f["has_cast"] else 0
    elif wanted == "case_when_logic":
        score += 20 if f["has_case"] else 0
    elif wanted == "distinct_after_join":
        score += 20 if f["has_distinct"] and f["has_join"] else 0
    elif wanted == "correlated_subquery":
        score += 20 if f["has_in_subquery"] else 0
    return score


def choose_examples(classified):
    categories = [
        (
            "multi_table_join_aggregation",
            "Multi-table JOIN with aggregation",
            lambda x: x["features"]["has_join"] and x["features"]["has_agg"],
            "Add a join+aggregate few-shot that first lists grain, join keys, and aggregate grain before writing SQL.",
        ),
        (
            "filter_value_mismatch",
            "Filter value mismatch",
            lambda x: bool(re.search(r"'[^']{4,}'", x["sql"])) and x["features"]["has_where"] and not x["features"]["has_case"],
            "Add a value-grounding instruction: copy exact categorical values from evidence/schema examples and avoid shortening labels.",
        ),
        (
            "missing_order_by_limit",
            "Missing ORDER BY + LIMIT for ranking",
            lambda x: x["features"]["has_order_by"] and x["features"]["has_limit"],
            "Add a ranking few-shot mapping superlatives/top-N/lowest/highest to ORDER BY direction plus LIMIT.",
        ),
        (
            "where_vs_having",
            "WHERE vs HAVING confusion potential",
            lambda x: x["features"]["has_group_by"] and x["features"]["has_having"],
            "Add a GROUP BY few-shot that uses WHERE for row filters and HAVING only for aggregate predicates.",
        ),
        (
            "cast_type_conversion",
            "CAST/type conversion needed",
            lambda x: x["features"]["has_cast"],
            "Add a numeric-comparison rule: CAST text-coded numeric fields before ratios, sorting, or arithmetic.",
        ),
        (
            "case_when_logic",
            "CASE WHEN logic",
            lambda x: x["features"]["has_case"],
            "Add a CASE few-shot for categorical bucketing and require preserving every branch from the question.",
        ),
        (
            "distinct_after_join",
            "DISTINCT needed after JOIN",
            lambda x: x["features"]["has_distinct"] and x["features"]["has_join"],
            "Add a de-duplication few-shot: when a many-side join returns entities, SELECT DISTINCT the requested entity columns.",
        ),
        (
            "correlated_subquery",
            "Correlated subquery (IN/SELECT)",
            lambda x: x["features"]["has_in_subquery"] or re.search(r"\bEXISTS\s*\(", upper_no_literals(x["sql"])),
            "Add an IN/EXISTS few-shot where the inner query selects qualifying ids and the outer query returns requested rows.",
        ),
    ]

    examples = []
    used = set()
    for key, label, predicate, recommendation in categories:
        candidates = [x for x in classified if predicate(x) and x["question_id"] not in used]
        if not candidates:
            examples.append((label, None, recommendation))
            continue
        best = max(candidates, key=lambda x: representative_score(x, key))
        used.add(best["question_id"])
        examples.append((label, best, recommendation))
    return examples


def build_report(classified, execution_failures):
    total = len(classified)
    lines = []
    lines.append("# BIRD-SQL Failure Root Cause Analysis")
    lines.append("")
    lines.append("Dataset: {} questions from `bird_bench/dev/dev_20240627/dev.json`.".format(total))
    lines.append("Model outputs were not saved, so difficulty correlations use BIRD's built-in `difficulty` label as the proxy target.")
    lines.append("")

    summaries = load_benchmark_summary()
    if summaries:
        rows = []
        for s in summaries:
            ex = s["ex"]
            if isinstance(ex, float):
                ex = "{:.1f}%".format(ex * 100 if ex <= 1 else ex)
            rows.append([s["file"], s["passed"], s["total"], ex, s["time_min"]])
        lines.append("## Benchmark Aggregates")
        lines.append(table(["File", "Passed", "Total", "EX", "Time min"], rows))
        lines.append("")

    diff_rows = []
    diff_counter = Counter(x["difficulty"] for x in classified)
    max_diff = max(diff_counter.values()) if diff_counter else 1
    for d in DIFF_ORDER:
        n = diff_counter.get(d, 0)
        diff_rows.append([d, n, pct(n, total), ascii_bar(n, max_diff)])
    lines.append("## BIRD Difficulty Distribution")
    lines.append(table(["Difficulty", "Count", "Share", "Bar"], diff_rows))
    lines.append("")

    ok_count = total - len(execution_failures)
    lines.append("## Gold SQL Execution Validation")
    lines.append("Executed all {} gold SQL statements against read-only SQLite connections.".format(total))
    lines.append("A SQLite progress handler stops runaway statements after about {:,} virtual-machine operations; those are reported as `OperationalError: interrupted`.".format(SQLITE_PROGRESS_OPS * SQLITE_PROGRESS_LIMIT))
    lines.append("")
    lines.append(table(["Status", "Count", "Share"], [["executed_ok", ok_count, pct(ok_count, total)], ["execution_failed", len(execution_failures), pct(len(execution_failures), total)]]))
    if execution_failures:
        rows = []
        for item in execution_failures[:20]:
            rows.append([item["question_id"], item["db_id"], item["execution"]["error_type"], item["execution"]["error"][:80]])
        lines.append("")
        lines.append("First execution failures:")
        lines.append(table(["QID", "DB", "Error type", "Error"], rows))
    lines.append("")

    feature_keys = [
        "has_join",
        "has_subquery",
        "has_agg",
        "has_group_by",
        "has_having",
        "has_order_by",
        "has_limit",
        "has_distinct",
        "has_case",
        "has_cast",
        "has_in_subquery",
        "has_between",
        "has_like",
        "has_string_op",
        "has_date_op",
        "has_math",
    ]
    challenging = [x["difficulty"] == "challenging" for x in classified]
    feature_rows = []
    for key in feature_keys:
        present = [bool(x["features"][key]) for x in classified]
        present_items = [x for x in classified if x["features"][key]]
        absent_items = [x for x in classified if not x["features"][key]]
        c_present = sum(1 for x in present_items if x["difficulty"] == "challenging")
        c_absent = sum(1 for x in absent_items if x["difficulty"] == "challenging")
        present_rate = c_present / len(present_items) if present_items else 0.0
        absent_rate = c_absent / len(absent_items) if absent_items else 0.0
        lift = present_rate - absent_rate
        feature_rows.append(
            [
                key,
                len(present_items),
                pct(c_present, len(present_items)),
                "{:+.1f}pp".format(lift * 100),
                "{:+.3f}".format(phi(present, challenging)),
            ]
        )
    feature_rows.sort(key=lambda r: float(r[3].replace("pp", "")), reverse=True)
    lines.append("## Structural Features Most Correlated With Challenging Labels")
    lines.append(table(["Feature", "Present", "Challenging rate", "Lift vs absent", "Phi"], feature_rows))
    lines.append("")

    db_groups = defaultdict(list)
    for item in classified:
        db_groups[item["db_id"]].append(item)
    db_rows = []
    for db_id, items in sorted(db_groups.items()):
        challenging_n = sum(1 for x in items if x["difficulty"] == "challenging")
        avg_complexity = mean([x["features"]["complexity_score"] for x in items])
        exec_failed = sum(1 for x in items if not x["execution"]["ok"])
        db_rows.append([db_id, len(items), difficulty_counts(items), pct(challenging_n, len(items)), "{:.2f}".format(avg_complexity), exec_failed])
    db_rows.sort(key=lambda r: (float(r[3].strip("%")), float(r[4])), reverse=True)
    lines.append("## By-Database Difficulty Proxy")
    lines.append("Failure rate here means BIRD `challenging` label rate, because per-question model predictions were not saved.")
    lines.append(table(["Database", "N", "S/M/C", "Proxy fail rate", "Avg complexity", "Gold exec fails"], db_rows))
    lines.append("")

    pattern_groups = defaultdict(list)
    for item in classified:
        pattern_groups[item["features"]["pattern_type"]].append(item)
    pattern_rows = []
    for pattern, items in pattern_groups.items():
        challenging_n = sum(1 for x in items if x["difficulty"] == "challenging")
        avg_complexity = mean([x["features"]["complexity_score"] for x in items])
        pattern_rows.append([pattern, len(items), difficulty_counts(items), pct(challenging_n, len(items)), "{:.2f}".format(avg_complexity)])
    pattern_rows.sort(key=lambda r: (float(r[3].strip("%")), float(r[4])), reverse=True)
    lines.append("## Pattern-Type Difficulty Ranking")
    lines.append(table(["Pattern type", "N", "S/M/C", "Challenging rate", "Avg complexity"], pattern_rows))
    lines.append("")

    complexity_scores = [x["features"]["complexity_score"] for x in classified]
    difficulty_scores = [x["difficulty_score"] for x in classified]
    challenging_ints = [1 if x["difficulty"] == "challenging" else 0 for x in classified]
    lines.append("## Complexity Score vs Difficulty")
    lines.append("Pearson(score, difficulty 1-3): {:+.3f}".format(pearson(complexity_scores, difficulty_scores)))
    lines.append("Pearson(score, challenging 0/1): {:+.3f}".format(pearson(complexity_scores, challenging_ints)))
    lines.append("")
    score_groups = defaultdict(list)
    for item in classified:
        score_groups[item["features"]["complexity_score"]].append(item)
    score_rows = []
    for score in sorted(score_groups):
        items = score_groups[score]
        challenging_n = sum(1 for x in items if x["difficulty"] == "challenging")
        score_rows.append([score, len(items), difficulty_counts(items), pct(challenging_n, len(items)), ascii_bar(challenging_n, max(1, len(items)), 20)])
    lines.append(table(["Score", "N", "S/M/C", "Challenging rate", "Bar"], score_rows))
    lines.append("")

    lines.append("## Representative Examples and Targeted Fixes")
    for label, item, recommendation in choose_examples(classified):
        lines.append("")
        lines.append("### " + label)
        if item is None:
            lines.append("No matching gold SQL found.")
        else:
            lines.append("- Question id: `{}`".format(item["question_id"]))
            lines.append("- Database: `{}`".format(item["db_id"]))
            lines.append("- Difficulty: `{}`".format(item["difficulty"]))
            lines.append("- Question: {}".format(item["question"]))
            if item.get("evidence"):
                lines.append("- Evidence: {}".format(item["evidence"]))
            lines.append("- Gold SQL:")
            lines.append("```sql")
            lines.append(item["sql"])
            lines.append("```")
        lines.append("- Targeted fix: {}".format(recommendation))

    lines.append("")
    lines.append("## Output Artifacts")
    lines.append("- Full classified metadata: `bird_bench/results/question_classifications.json`")
    lines.append("- This report: `bird_bench/results/failure_analysis_report.md`")
    return "\n".join(lines) + "\n"


def main():
    dev = load_json(DEV_PATH)
    classified = []

    for idx, q in enumerate(dev):
        sql = q["SQL"]
        features = classify_sql(sql)
        execution = execute_gold_sql(q["db_id"], sql)
        question_id = q.get("question_id", idx)
        item = {
            "idx": idx,
            "question_id": question_id,
            "db_id": q["db_id"],
            "difficulty": q.get("difficulty", ""),
            "difficulty_score": DIFF_SCORE.get(q.get("difficulty", ""), 0),
            "question": q.get("question", ""),
            "evidence": q.get("evidence", ""),
            "sql": sql,
            "features": features,
            "execution": execution,
        }
        classified.append(item)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(CLASSIFICATIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(classified, f, indent=2, ensure_ascii=False)

    execution_failures = [x for x in classified if not x["execution"]["ok"]]
    report = build_report(classified, execution_failures)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)

    print(report)


if __name__ == "__main__":
    main()
