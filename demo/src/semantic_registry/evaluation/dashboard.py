from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi.responses import HTMLResponse

from src.semantic_registry.evaluation.models import EvalResult


def render_eval_dashboard(runs: dict[str, EvalResult]) -> HTMLResponse:
    latest = next(reversed(runs.values()), None) if runs else None
    success_rate = latest.success_rate if latest else 0.0
    recent_rows = []
    failing_rows = []
    domain_counts: Counter[str] = Counter()

    for run_id, result in reversed(list(runs.items())[-10:]):
        recent_rows.append(
            f"<tr><td>{run_id}</td><td>{result.total_cases}</td><td>{result.passed}</td>"
            f"<td>{result.failed}</td><td>{result.success_rate:.1%}</td></tr>"
        )
        for case in result.case_results:
            domain = str((case.generated_plan or case.expected_plan).get("domain") or "unknown")
            domain_counts[domain] += 1
            if not case.passed:
                failing_rows.append(
                    f"<tr><td>{case.case_id}</td><td>{domain}</td><td>{'; '.join(case.errors[:2])}</td></tr>"
                )

    domain_items = "".join(f"<li>{domain}: {count}</li>" for domain, count in sorted(domain_counts.items()))
    html = f"""
    <!doctype html>
    <html>
      <head>
        <title>Evaluation Dashboard</title>
        <style>
          body {{ font-family: Arial, sans-serif; margin: 32px; color: #1f2937; }}
          h1 {{ font-size: 28px; margin-bottom: 8px; }}
          h2 {{ font-size: 18px; margin-top: 28px; }}
          .metric {{ font-size: 44px; font-weight: 700; margin: 12px 0; }}
          table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
          th, td {{ border: 1px solid #d1d5db; padding: 8px; text-align: left; vertical-align: top; }}
          th {{ background: #f3f4f6; }}
          .chart {{ height: 120px; border: 1px dashed #9ca3af; display: grid; place-items: center; color: #6b7280; }}
        </style>
      </head>
      <body>
        <h1>Evaluation Dashboard</h1>
        <div>Overall success rate</div>
        <div class="metric">{success_rate:.1%}</div>
        <h2>Per-Domain Breakdown</h2>
        <ul>{domain_items or "<li>No eval runs yet</li>"}</ul>
        <h2>Recent Eval Runs</h2>
        <table><thead><tr><th>Run ID</th><th>Total</th><th>Passed</th><th>Failed</th><th>Success Rate</th></tr></thead>
        <tbody>{''.join(recent_rows) or "<tr><td colspan='5'>No runs yet</td></tr>"}</tbody></table>
        <h2>Top Failing Cases</h2>
        <table><thead><tr><th>Case</th><th>Domain</th><th>Error</th></tr></thead>
        <tbody>{''.join(failing_rows[:10]) or "<tr><td colspan='3'>No failures</td></tr>"}</tbody></table>
        <h2>Trend</h2>
        <div class="chart">Trend chart placeholder</div>
      </body>
    </html>
    """
    return HTMLResponse(content=html)


def aggregate_run_metrics(runs: dict[str, EvalResult]) -> dict[str, Any]:
    if not runs:
        return {"runs": 0, "success_rate": 0.0, "metrics": {}}
    metric_totals: dict[str, list[float]] = {}
    total_cases = 0
    total_passed = 0
    for result in runs.values():
        total_cases += result.total_cases
        total_passed += result.passed
        for key, value in result.metrics.items():
            metric_totals.setdefault(key, []).append(value)
    return {
        "runs": len(runs),
        "success_rate": (total_passed / total_cases) if total_cases else 0.0,
        "metrics": {
            key: sum(values) / len(values)
            for key, values in sorted(metric_totals.items())
            if values
        },
    }

