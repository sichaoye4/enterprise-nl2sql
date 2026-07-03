#!/usr/bin/env python3
"""
NL2SQL API Server — connects UI to pattern memory + SQL generation.

Endpoints:
  POST /api/query    — Submit question, get SQL + results
  GET  /api/history  — Past queries
  POST /api/confirm  — Mark query as correct (feeds pattern memory)
  GET  /api/stats    — Pattern memory stats

Run:
  .venv/bin/python scripts/nl2sql_api.py
  # Opens at http://localhost:8765
"""

import sys, os, json, time, sqlite3, re as regex
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Load env
env_path = os.path.expanduser("~/.hermes/.env")
for line in open(env_path):
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1); k, v = k.strip(), v.strip().strip("'\"")
        if k == "DEEPSEEK_API_KEY" and not os.environ.get("DEEPSEEK_API_KEY"):
            os.environ[k] = v

from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

PORT = 8765
BIRD_ROOT = os.path.join(os.path.dirname(__file__), "..", "bird_bench", 
                          "dev", "dev_20240627", "databases", "dev_databases")

# Lazy-loaded singletons
_mem = None
_prov = None

def mem():
    global _mem
    if _mem is None:
        from scripts.pattern_memory_v2 import PatternMemory
        _mem = PatternMemory()
    return _mem

def prov():
    global _prov
    if _prov is None:
        from src.semantic_registry.pipeline.llm_gateway import DeepSeekProvider
        _prov = DeepSeekProvider(model="deepseek-v4-flash", reasoning_effort="xhigh")
    return _prov


def get_schema(db_id):
    db_path = os.path.join(BIRD_ROOT, db_id, f"{db_id}.sqlite")
    if not os.path.exists(db_path): return ""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL")
    schemas = [row[0] for row in c.fetchall()]
    conn.close()
    return "\n\n".join(schemas)


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
        elif p == "/api/history":
            self._json(load_history())
        elif p == "/api/stats":
            self._json(mem().stats())
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
        with open(hp) as f:
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
        
        start = time.time()
        schema = get_schema(db_id) if db_id else ""
        patterns = mem().retrieve(question, db_id=db_id, top_k=3) if db_id else []
        
        if patterns:
            prompt = mem().build_few_shot_prompt(question, schema, patterns)
        else:
            parts = ["You are a SQLite expert. Generate a single SELECT statement.",
                     f"Database Schema:\n{schema}" if schema else "",
                     f"Question: {question}",
                     'Return ONLY: {"sql":"SELECT...","assumptions":[],"tables_used":[],"columns_used":[],"confidence":"high|medium|low","reasoning_summary":"..."}']
            prompt = "\n\n".join(p for p in parts if p)
        
        sql, error = "", None
        try:
            raw = prov().generate(f"Return ONLY valid JSON.\n\n{prompt}")
            sql = extract_sql(raw)
            if not sql: error = "Could not extract SQL"
        except Exception as e:
            error = str(e)
        
        results, cols, exec_err = None, None, None
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
        
        elapsed = time.time() - start
        resp = {
            "question": question, "db_id": db_id, "sql": sql,
            "results": results, "columns": cols,
            "execution_error": exec_err, "error": error,
            "patterns_used": [{"intent": p.metadata.business_intent, "question": p.question[:50]} for p in patterns],
            "elapsed": round(elapsed, 2),
        }
        save_history(resp)
        self._json(resp)
    
    def _handle_confirm(self, data):
        q, s, d = data.get("question",""), data.get("sql",""), data.get("db_id","")
        if q and s and d:
            mem().ingest(q, s, d)
            self._json({"status": "confirmed"})
        else:
            self._json({"error": "Missing fields"}, 400)
    
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[NL2SQL] {args[0]} {args[1]} {args[2]}\n")


if __name__ == "__main__":
    print(f"NL2SQL API at http://localhost:{PORT}")
    print(f"Open in browser: http://localhost:{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
