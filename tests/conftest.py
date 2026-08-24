"""Offline fixtures: build the warehouse + evaluate into tmp artifacts."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import duckdb
import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from make_warehouse import QUESTIONS, build_frames  # noqa: E402

from copilotdesk.settings import get_config, get_settings  # noqa: E402


@pytest.fixture(scope="session")
def warehouse(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("copilotdesk")
    (tmp / "processed").mkdir()

    cfg = get_config()
    originals = (
        cfg["data"]["processed_dir"],
        cfg["data"]["artifacts_dir"],
        cfg["data"]["db_path"],
        cfg["agent"]["eval_path"],
    )
    cfg["data"]["processed_dir"] = str(tmp / "processed")
    cfg["data"]["artifacts_dir"] = str(tmp / "artifacts")
    cfg["data"]["db_path"] = str(tmp / "processed" / "warehouse.duckdb")
    cfg["agent"]["eval_path"] = str(tmp / "processed" / "questions.json")

    rng = np.random.default_rng(7)
    frames = build_frames({"n_customers": 300, "n_orders": 4000}, rng)
    con = duckdb.connect(cfg["data"]["db_path"])
    for name, df in frames.items():
        con.register(f"_{name}", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _{name}")
    con.close()
    Path(cfg["agent"]["eval_path"]).write_text(json.dumps(QUESTIONS), encoding="utf-8")

    old_uri = os.environ.get("MLFLOW_TRACKING_URI")
    os.environ["MLFLOW_TRACKING_URI"] = f"sqlite:///{tmp / 'mlflow.db'}"
    get_settings.cache_clear()

    from copilotdesk.agents.evaluate import evaluate

    metrics = evaluate()
    yield {"metrics": metrics, "tmp": tmp}

    (
        cfg["data"]["processed_dir"],
        cfg["data"]["artifacts_dir"],
        cfg["data"]["db_path"],
        cfg["agent"]["eval_path"],
    ) = originals
    if old_uri is None:
        os.environ.pop("MLFLOW_TRACKING_URI", None)
    else:
        os.environ["MLFLOW_TRACKING_URI"] = old_uri
    get_settings.cache_clear()


@pytest.fixture
def api_client(warehouse):
    from fastapi.testclient import TestClient

    from copilotdesk.api import routes
    from copilotdesk.api.main import app

    routes._report.cache_clear()
    try:
        yield TestClient(app)
    finally:
        routes._report.cache_clear()
