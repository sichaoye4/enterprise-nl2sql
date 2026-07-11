from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_bird_eval_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "run_bird_full_eval.py"
    spec = importlib.util.spec_from_file_location("run_bird_full_eval_for_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_result_set_match_detail_explains_same_sample_but_different_full_sets() -> None:
    bird_eval = _load_bird_eval_module()
    predicted = {
        "ok": True,
        "rows": [(1,), (2,), (4,), (6,), (7,), (10,)],
        "row_count": 6,
        "error": "",
    }
    gold = {
        "ok": True,
        "rows": [(1,), (2,), (4,), (6,), (7,), (11,)],
        "row_count": 6,
        "error": "",
    }

    assert bird_eval.result_sets_match(predicted, gold) is False

    detail = bird_eval.result_set_match_detail(predicted, gold)

    assert detail == {
        "reason": "result_set_diff",
        "predicted_row_count": 6,
        "gold_row_count": 6,
        "predicted_only_sample": [[10]],
        "gold_only_sample": [[11]],
    }
