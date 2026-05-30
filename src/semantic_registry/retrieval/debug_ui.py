from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from src.semantic_registry.retrieval.hybrid import RetrievalResult


router = APIRouter()


class RetrievalSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    domain: str | None = None
    top_k: int = Field(default=10, ge=1, le=50)


DEBUG_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Retrieval Debug</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 32px; color: #172026; }
    form { display: flex; gap: 8px; margin-bottom: 20px; }
    input, select, button { font: inherit; padding: 8px 10px; }
    input { min-width: 420px; }
    table { border-collapse: collapse; width: 100%; margin: 16px 0 28px; }
    th, td { border-bottom: 1px solid #d7dde2; padding: 8px; text-align: left; vertical-align: top; }
    th { background: #f4f6f8; }
    .top { background: #eef7f1; }
    code { white-space: pre-wrap; }
  </style>
</head>
<body>
  <h1>Retrieval Debug</h1>
  <form id="search-form">
    <input id="query" name="query" placeholder="Ask a natural language question" required>
    <select id="domain" name="domain">
      <option value="">All domains</option>
      <option value="finance">finance</option>
      <option value="sales">sales</option>
      <option value="marketing">marketing</option>
      <option value="product">product</option>
    </select>
    <button type="submit">Search</button>
  </form>
  <div id="results"></div>
  <script>
    const categories = [
      ["candidate_tables", "table"],
      ["candidate_metrics", "metric"],
      ["candidate_dimensions", "dimension"],
      ["candidate_concepts", "concept"],
    ];
    function row(candidate, type, index, breakdown) {
      const key = `${type}:${candidate.name}`;
      const scores = breakdown[key] || {};
      return `<tr class="${index < 5 ? "top" : ""}">
        <td>${candidate.name}</td>
        <td>${type}</td>
        <td>${candidate.score.toFixed(3)}</td>
        <td><code>${JSON.stringify(scores, null, 2)}</code></td>
      </tr>`;
    }
    document.getElementById("search-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const payload = {
        query: document.getElementById("query").value,
        domain: document.getElementById("domain").value || null,
        top_k: 10,
      };
      const response = await fetch("/debug/retrieval/search", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      document.getElementById("results").innerHTML = categories.map(([field, type]) => {
        const rows = (data[field] || []).map((candidate, index) => row(candidate, type, index, data.score_breakdown || {})).join("");
        return `<h2>${type}s</h2><table>
          <thead><tr><th>Name</th><th>Type</th><th>Score</th><th>Breakdown</th></tr></thead>
          <tbody>${rows || "<tr><td colspan='4'>No candidates</td></tr>"}</tbody>
        </table>`;
      }).join("");
    });
  </script>
</body>
</html>
"""


@router.get("/debug/retrieval", response_class=HTMLResponse)
async def retrieval_debug_page() -> HTMLResponse:
    return HTMLResponse(DEBUG_HTML)


@router.post("/debug/retrieval/search")
async def retrieval_debug_search(payload: RetrievalSearchRequest, request: Request) -> dict:
    retriever = getattr(request.app.state, "hybrid_retriever", None) or getattr(request.app.state, "retrieval_retriever", None)
    if retriever is None:
        return RetrievalResult().model_dump()
    result = retriever.retrieve(payload.query, domain=payload.domain, top_k=payload.top_k)
    return result.model_dump()


__all__ = ["RetrievalSearchRequest", "router"]
