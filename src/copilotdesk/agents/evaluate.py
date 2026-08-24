"""Evaluate the pipeline on the labeled question set; log to MLflow.

Usage:
    python -m copilotdesk.agents.evaluate
"""

from __future__ import annotations

import json
import logging
import pickle

import mlflow

from copilotdesk.agents.pipeline import answer
from copilotdesk.agents.planner import plan
from copilotdesk.settings import get_config, get_settings, resolve_path

logger = logging.getLogger(__name__)


def evaluate() -> dict:
    cfg = get_config()
    questions = json.loads(resolve_path(cfg["agent"]["eval_path"]).read_text(encoding="utf-8"))

    intent_correct = 0
    executed = 0
    guard_ok = 0
    results = []
    for item in questions:
        plan_obj = plan(item["q"])
        intent_correct += int(plan_obj["intent"] == item["intent"])
        result = answer(item["q"])
        ok = "error" not in result
        guard_ok += int(ok)
        executed += int(ok and len(result.get("data", [])) >= 0)
        results.append(
            {
                "question": item["q"],
                "expected_intent": item["intent"],
                "planned_intent": plan_obj["intent"],
                "executed": ok,
                "sql": result.get("sql"),
                "narrative": result.get("narrative"),
            }
        )

    n = len(questions)
    metrics = {
        "intent_accuracy": round(intent_correct / n, 4),
        "execution_rate": round(executed / n, 4),
        "guardrail_pass_rate": round(guard_ok / n, 4),
        "n_questions": n,
    }

    mlflow.set_tracking_uri(get_settings().mlflow_tracking_uri)
    mlflow.set_experiment(cfg["eval"]["experiment_name"])
    with mlflow.start_run(run_name="analyst-eval"):
        mlflow.log_metrics(metrics)
    logger.info("analyst-eval %s", metrics)

    artifacts = resolve_path(cfg["data"]["artifacts_dir"])
    artifacts.mkdir(parents=True, exist_ok=True)
    with open(artifacts / "report.pkl", "wb") as f:
        pickle.dump({"metrics": metrics, "results": results}, f)
    return metrics


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    evaluate()
