#!/usr/bin/env python3
"""
NL2SQL API Server — connects the BIRD workspace to SQL generation.

Endpoints:
  POST /api/query    — Submit question, get SQL + results
  GET  /api/database — Selected database schema and sample data
  GET  /api/history  — Past queries
  POST /api/confirm  — Mark query as correct (feeds pattern memory)
  GET  /api/stats    — Pattern memory stats

Run:
  .venv/bin/python scripts/nl2sql_api.py
  # Opens at http://localhost:8765
"""

import sys, os, json, time, sqlite3, re as regex, threading, uuid
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Load env
env_path = os.path.expanduser("~/.hermes/.env")
if os.path.exists(env_path):
    for line in open(env_path, encoding="utf-8"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1); k, v = k.strip(), v.strip().strip("'\"")
            if k == "DEEPSEEK_API_KEY" and not os.environ.get("DEEPSEEK_API_KEY"):
                os.environ[k] = v

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from src.semantic_registry.pipeline.semantic_judge import (
    DashScopeLLMClient,
    build_judge_prompt,
    parse_judge_response,
)

PORT = 8765
BIRD_ROOT = os.path.join(os.path.dirname(__file__), "..", "bird_bench", 
                          "dev", "dev_20240627", "databases", "dev_databases")

# Lazy-loaded singletons
_mem = None
_prov = None
_tasks = {}

EXPECTED_PIPELINE_STAGES = [
    ("schema_context", "Schema context"),
    ("pattern_retrieval", "Pattern & semantic retrieval"),
    ("sql_generation", "SQL candidate generation"),
    ("llm_judge", "Cross-model LLM judge"),
    ("sql_validation", "SQL validation"),
    ("preview_execution", "Preview execution"),
]


class UnavailablePatternMemory:
    """Safe, read-only fallback when local pattern storage is unavailable."""

    def __init__(self, reason):
        self.reason = str(reason)

    def retrieve(self, *args, **kwargs):
        return []

    def stats(self):
        return {"available": False, "reason": self.reason, "patterns": 0}

    def ingest(self, *args, **kwargs):
        return None

    def build_few_shot_prompt(self, question, schema, patterns):
        return ""

def mem():
    global _mem
    if _mem is None:
        try:
            from scripts.pattern_memory_v2 import PatternMemory
            _mem = PatternMemory()
        except Exception as exc:
            # The interactive BIRD explorer must remain usable even when the
            # optional local pattern-memory directory cannot be initialized.
            _mem = UnavailablePatternMemory(exc)
    return _mem

def prov():
    global _prov
    if _prov is None:
        from src.semantic_registry.pipeline.llm_gateway import DeepSeekProvider
        _prov = DeepSeekProvider(model="deepseek-v4-flash", reasoning_effort="xhigh")
    return _prov

# Lazy-loaded cross-model judge (DashScope Qwen)
_judge = None

def get_judge():
    global _judge
    if _judge is None:
        try:
            _judge = DashScopeLLMClient(model="qwen3.5-plus")
        except Exception as exc:
            _judge = exc
    return _judge


def pending_pipeline_stages():
    return [
        {
            "id": stage_id,
            "title": title,
            "status": "pending",
            "summary": "Waiting to start.",
            "detail": "",
        }
        for stage_id, title in EXPECTED_PIPELINE_STAGES
    ]


def update_stage(task_id, stage_id, status, summary, detail=""):
    task = _tasks.get(task_id)
    if not task:
        return
    task["stages"] = [
        {
            **stage,
            "status": status,
            "summary": summary,
            "detail": detail,
        }
        if stage["id"] == stage_id else stage
        for stage in task["stages"]
    ]
    task["updated_at"] = time.time()


def snapshot_task(task_id):
    task = _tasks.get(task_id)
    if not task:
        return None
    return {
        "task_id": task_id,
        "status": task.get("status", "running"),
        "stages": [dict(stage) for stage in task.get("stages", [])],
        "result": task.get("result"),
        "error": task.get("error"),
    }


def run_query_task(task_id, question, db_id):
    start = time.time()
    try:
        update_stage(task_id, "schema_context", "loading", "Loading SQLite schema context.")
        schema = get_schema(db_id) if db_id else ""
        update_stage(
            task_id,
            "schema_context",
            "complete" if schema else "warning",
            f"Loaded {len(schema.splitlines())} schema lines for {db_id or 'no database'}.",
            schema[:12000],
        )

        update_stage(task_id, "pattern_retrieval", "loading", "Retrieving matching prior query patterns.")
        patterns = mem().retrieve(question, db_id=db_id, top_k=3) if db_id else []
        update_stage(
            task_id,
            "pattern_retrieval",
            "complete",
            f"Found {len(patterns)} relevant prior query patterns.",
            "\n".join(f"- {p.question}" for p in patterns) or "No prior patterns matched; using schema context.",
        )

        if patterns:
            prompt = mem().build_few_shot_prompt(question, schema, patterns)
        else:
            parts = [
                "You are a SQLite expert. Generate a single SELECT statement.",
                f"Database Schema:\n{schema}" if schema else "",
                f"Question: {question}",
                'Return ONLY: {"sql":"SELECT...","assumptions":[],"tables_used":[],"columns_used":[],"confidence":"high|medium|low","reasoning_summary":"..."}',
            ]
            prompt = "\n\n".join(p for p in parts if p)

        sql, error = "", None
        update_stage(task_id, "sql_generation", "loading", "Generating a SQLite SQL candidate.")
        try:
            raw = prov().generate(f"Return ONLY valid JSON.\n\n{prompt}")
            sql = extract_sql(raw)
            if not sql:
                error = "Could not extract SQL"
        except Exception as e:
            error = str(e)
        update_stage(
            task_id,
            "sql_generation",
            "error" if error else "complete",
            error or "Generated one SQLite SQL candidate.",
            sql or error or "No SQL was produced.",
        )

        judge_result = None
        if sql and not error:
            update_stage(task_id, "llm_judge", "loading", "Reviewing the SQL with the cross-model judge.")
            judge_client = get_judge()
            if isinstance(judge_client, Exception):
                update_stage(
                    task_id,
                    "llm_judge",
                    "warning",
                    f"Judge unavailable: {judge_client}",
                    str(judge_client),
                )
            else:
                try:
                    judge_prompt = build_judge_prompt(question, sql, None, None)
                    judge_raw = judge_client.generate(judge_prompt)
                    judge_result = parse_judge_response(judge_raw)
                    update_stage(
                        task_id,
                        "llm_judge",
                        "complete" if judge_result.pass_ else "warning",
                        f"{'PASS (no issues found)' if judge_result.pass_ else 'ISSUES DETECTED'} — confidence {judge_result.confidence:.0%}",
                        f"Reasoning: {judge_result.reasoning}\n\nConfidence: {judge_result.confidence:.0%}",
                    )
                except Exception as e:
                    update_stage(
                        task_id,
                        "llm_judge",
                        "warning",
                        f"Judge error: {e}",
                        str(e),
                    )
        else:
            update_stage(
                task_id,
                "llm_judge",
                "skipped",
                "Skipped because no SQL candidate was generated.",
                "",
            )

        update_stage(task_id, "sql_validation", "loading", "Checking query shape before execution.")
        validation_error = None
        if sql and not regex.match(r"^\s*(SELECT|WITH)\b", sql, regex.IGNORECASE):
            validation_error = "Only SELECT or WITH queries can be previewed."
        update_stage(
            task_id,
            "sql_validation",
            "error" if validation_error else ("complete" if sql else "skipped"),
            validation_error or ("Read-only query accepted for SQLite preview." if sql else "Skipped because no SQL was generated."),
            validation_error or "Checked query shape before execution.",
        )
        if validation_error:
            error = validation_error

        results, cols, exec_err = None, None, None
        update_stage(task_id, "preview_execution", "loading", "Executing a read-only preview against SQLite.")
        if sql and not error and db_id:
            dp = os.path.join(BIRD_ROOT, db_id, f"{db_id}.sqlite")
            if os.path.exists(dp):
                try:
                    conn = sqlite3.connect(dp)
                    conn.row_factory = sqlite3.Row
                    c = conn.cursor()
                    c.execute(sql)
                    cols = [d[0] for d in c.description] if c.description else []
                    rows = c.fetchall()
                    results = [list(r) for r in rows]
                    conn.close()
                except Exception as e:
                    exec_err = str(e)
        update_stage(
            task_id,
            "preview_execution",
            "error" if exec_err else ("complete" if results is not None else "skipped"),
            exec_err or (f"Returned {len(results)} rows." if results is not None else "Skipped because no database was selected."),
            exec_err or "Executed against the selected BIRD SQLite database with a read-only preview.",
        )

        elapsed = time.time() - start
        resp = {
            "question": question,
            "db_id": db_id,
            "sql": sql,
            "results": results,
            "columns": cols,
            "execution_error": exec_err,
            "error": error,
            "patterns_used": [{"intent": p.metadata.business_intent, "question": p.question[:50]} for p in patterns],
            "elapsed": round(elapsed, 2),
            "pipeline_stages": snapshot_task(task_id)["stages"],
        }
        save_history(resp)
        task = _tasks[task_id]
        task["result"] = resp
        task["status"] = "complete"
        task["updated_at"] = time.time()
    except Exception as exc:
        task = _tasks.get(task_id)
        if task:
            task["status"] = "error"
            task["error"] = str(exc)
            task["updated_at"] = time.time()


def get_schema(db_id):
    db_path = os.path.join(BIRD_ROOT, db_id, f"{db_id}.sqlite")
    if not os.path.exists(db_path): return ""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL")
    schemas = [row[0] for row in c.fetchall()]
    conn.close()
    return "\n\n".join(schemas)


def get_db_path(db_id):
    return os.path.join(BIRD_ROOT, db_id, f"{db_id}.sqlite")


def quote_identifier(value):
    return '"' + str(value).replace('"', '""') + '"'


def json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return f"<binary {len(value)} bytes>"
    return str(value)


def get_database_preview(db_id, row_limit=20):
    db_path = get_db_path(db_id)
    if not db_id or not os.path.exists(db_path):
        return {"error": "Database not found"}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        tables = []
        for table_name, create_sql in cursor.fetchall():
            cursor.execute(f"PRAGMA table_info({quote_identifier(table_name)})")
            columns = [
                {
                    "name": row["name"],
                    "type": row["type"] or "TEXT",
                    "primary_key": bool(row["pk"]),
                    # SQLite's PRAGMA can report NOT NULL as false for an
                    # INTEGER PRIMARY KEY, even though it cannot be null.
                    "nullable": not (bool(row["notnull"]) or bool(row["pk"])),
                }
                for row in cursor.fetchall()
            ]
            cursor.execute(f"SELECT COUNT(*) FROM {quote_identifier(table_name)}")
            row_count = cursor.fetchone()[0]
            cursor.execute(f"SELECT * FROM {quote_identifier(table_name)} LIMIT ?", (row_limit,))
            sample_rows = [
                {key: json_value(value) for key, value in dict(row).items()}
                for row in cursor.fetchall()
            ]
            tables.append({
                "name": table_name,
                "columns": columns,
                "row_count": row_count,
                "sample_rows": sample_rows,
                "create_sql": create_sql or "",
            })
        return {
            "id": db_id,
            "name": db_id.replace("_", " ").title(),
            "table_count": len(tables),
            "tables": tables,
        }
    finally:
        conn.close()


def get_dbs():
    dbs = []
    if not os.path.exists(BIRD_ROOT): return dbs
    for d in sorted(os.listdir(BIRD_ROOT)):
        dp = os.path.join(BIRD_ROOT, d, f"{d}.sqlite")
        if os.path.exists(dp):
            try:
                conn = sqlite3.connect(dp)
                c = conn.cursor()
                c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
                tc = c.fetchone()[0]
                conn.close()
                dbs.append({"id": d, "name": d.replace("_", " ").title(), "tables": tc})
            except:
                dbs.append({"id": d, "name": d.replace("_", " ").title(), "tables": 0})
    return dbs


def extract_sql(raw):
    if not raw: return ""
    try:
        start = raw.find("{")
        if start >= 0:
            depth, instr, quote = 0, False, ""
            for i in range(start, len(raw)):
                c = raw[i]
                if instr:
                    if c == "\\": pass
                    elif c == quote: instr = False
                elif c in ("'", '"'): instr = True; quote = c
                elif c == "{": depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        cand = regex.sub(r",\s*([}\]])", r"\1", raw[start:i+1])
                        d = json.loads(cand)
                        if "sql" in d: return d["sql"]
    except: pass
    m = regex.search(r"SELECT\s+.*?(?:;|$)", raw, regex.DOTALL | regex.IGNORECASE)
    return m.group(0).strip().rstrip(";") if m else ""


def save_history(data):
    try:
        hp = os.path.join(os.path.dirname(__file__), "..", "bird_bench", "ui", "history.json")
        os.makedirs(os.path.dirname(hp), exist_ok=True)
        hist = []
        if os.path.exists(hp):
            with open(hp) as f: hist = json.load(f)
        hist.insert(0, {"question": data["question"], "db_id": data["db_id"], 
                        "sql": data["sql"], "timestamp": time.time()})
        with open(hp, "w") as f: json.dump(hist[:50], f, indent=2)
    except: pass


def load_history():
    hp = os.path.join(os.path.dirname(__file__), "..", "bird_bench", "ui", "history.json")
    if os.path.exists(hp):
        with open(hp) as f: return json.load(f)
    return []


class Handler(BaseHTTPRequestHandler):
    
    def do_OPTIONS(self):
        self._headers(200)
        self.end_headers()
    
    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            self._serve_ui()
        elif p == "/api/databases":
            self._json(get_dbs())
        elif p == "/api/database":
            db_id = parse_qs(urlparse(self.path).query).get("db_id", [""])[0]
            preview = get_database_preview(db_id)
            self._json(preview, 404 if preview.get("error") else 200)
        elif p == "/api/history":
            self._json(load_history())
        elif p == "/api/stats":
            self._json(mem().stats())
        elif p.startswith("/api/query/task/"):
            task_id = p.rsplit("/", 1)[-1]
            task = snapshot_task(task_id)
            self._json(task if task else {"error": "Task not found"}, 200 if task else 404)
        else:
            self.send_error(404)
    
    def do_POST(self):
        p = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        data = json.loads(body) if body else {}
        
        if p == "/api/query":
            self._handle_query(data)
        elif p == "/api/confirm":
            self._handle_confirm(data)
        else:
            self.send_error(404)
    
    def _headers(self, code=200):
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Type", "application/json")
    
    def _json(self, data, code=200):
        self._headers(code)
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
    
    def _serve_ui(self):
        hp = os.path.join(os.path.dirname(__file__), "..", "bird_bench", "ui", "index.html")
        if not os.path.exists(hp):
            self._json({"error": "UI not built"}, 404)
            return
        with open(hp, encoding="utf-8") as f:
            html = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(html.encode())
    
    def _handle_query(self, data):
        question = data.get("question", "").strip()
        db_id = data.get("db_id", "")
        if not question:
            self._json({"error": "Question required"}, 400)
            return
        task_id = str(uuid.uuid4())
        _tasks[task_id] = {
            "status": "running",
            "stages": pending_pipeline_stages(),
            "result": None,
            "error": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        threading.Thread(target=run_query_task, args=(task_id, question, db_id), daemon=True).start()
        self._json({"task_id": task_id})
    
    def _handle_confirm(self, data):
        q, s, d = data.get("question",""), data.get("sql",""), data.get("db_id","")
        if q and s and d:
            mem().ingest(q, s, d)
            self._json({"status": "confirmed"})
        else:
            self._json({"error": "Missing fields"}, 400)
    
    def log_message(self, fmt, *args):
        try:
            message = fmt % args
        except Exception:
            message = " ".join(str(arg) for arg in args)
        sys.stderr.write(f"[NL2SQL] {message}\n")


if __name__ == "__main__":
    print(f"NL2SQL API at http://localhost:{PORT}")
    print(f"Open in browser: http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
