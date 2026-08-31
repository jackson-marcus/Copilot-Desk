"""Evaluate the pipeline on the labeled question set; log to MLflow.

Usage:
    python -m copilotdesk.agents.evaluate
"""

from __future__ import annotations

import json
import logging
import pickle

import mlflow

from copilotdesk.pipeline import build_analyst_pipeline
from copilotdesk.settings import get_config, get_settings, resolve_path

logger = logging.getLogger(__name__)


def evaluate() -> dict:
    cfg = get_config()
    questions = json.loads(resolve_path(cfg["agent"]["eval_path"]).read_text(encoding="utf-8"))

    analyst = build_analyst_pipeline()

    intent_correct = 0
    executed = 0
    guard_ok = 0
    verified = 0
    results = []
    for item in questions:
        # One pass down the pipe yields every measurement: the planner's routing
        # is read off the envelope rather than recomputed on the side.
        env = analyst(item["q"])
        planned_intent = env.get("intent")
        intent_correct += int(planned_intent == item["intent"])
        ok = not env.halted
        guard_ok += int(ok)
        executed += int(ok and env.get("data") is not None)
        verdict = env.get("verdict", "unverified")
        verified += int(verdict == "verified")
        results.append(
            {
                "question": item["q"],
                "expected_intent": item["intent"],
                "planned_intent": planned_intent,
                "executed": ok,
                "verdict": verdict,
                "checks": [c["check"] for c in env.get("checks", [])],
                "sql": env.get("sql"),
                "narrative": env.get("narrative"),
            }
        )

    n = len(questions)
    metrics = {
        "intent_accuracy": round(intent_correct / n, 4),
        "execution_rate": round(executed / n, 4),
        "guardrail_pass_rate": round(guard_ok / n, 4),
        # Answers every applicable cross-check agreed with. An answer nothing
        # could check counts as unverified, not as a pass.
        "verified_rate": round(verified / n, 4),
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
