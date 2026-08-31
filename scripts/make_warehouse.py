"""Build a small DuckDB star schema + a labeled NL-question eval set.

Schema: dim_customers, dim_products, fact_orders (with date, region, category).
Questions carry an intent tag (trend / breakdown / top_n / kpi) so the planner's
routing accuracy is measurable.

Usage:
    uv run python scripts/make_warehouse.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from copilotdesk.settings import get_config, resolve_path

REGIONS = ["north", "south", "east", "west"]
CATEGORIES = ["electronics", "apparel", "home", "grocery", "toys"]
SEGMENTS = ["consumer", "smb", "enterprise"]


def build_frames(cfg: dict, rng) -> dict:
    n_cust, n_orders = cfg["n_customers"], cfg["n_orders"]
    customers = pd.DataFrame(
        {
            "customer_id": np.arange(1, n_cust + 1),
            "segment": rng.choice(SEGMENTS, n_cust, p=[0.6, 0.3, 0.1]),
            "region": rng.choice(REGIONS, n_cust),
        }
    )
    products = pd.DataFrame(
        {
            "product_id": np.arange(1, 41),
            "category": rng.choice(CATEGORIES, 40),
            "unit_price": np.round(rng.uniform(5, 400, 40), 2),
        }
    )
    days = pd.date_range("2025-01-01", periods=365)
    day_idx = rng.integers(0, len(days), n_orders)
    product_id = rng.integers(1, 41, n_orders)
    seasonal = 1 + 0.3 * np.sin(2 * np.pi * day_idx / 365)
    qty = rng.integers(1, 6, n_orders)
    unit = products.set_index("product_id").loc[product_id, "unit_price"].to_numpy()
    orders = pd.DataFrame(
        {
            "order_id": np.arange(1, n_orders + 1),
            "customer_id": rng.integers(1, n_cust + 1, n_orders),
            "product_id": product_id,
            "order_date": days[day_idx],
            "quantity": qty,
            "revenue": np.round(unit * qty * seasonal * rng.uniform(0.9, 1.1, n_orders), 2),
        }
    )
    orders["region"] = (
        customers.set_index("customer_id").loc[orders["customer_id"], "region"].to_numpy()
    )
    orders["category"] = (
        products.set_index("product_id").loc[orders["product_id"], "category"].to_numpy()
    )
    return {"dim_customers": customers, "dim_products": products, "fact_orders": orders}


QUESTIONS = [
    {"q": "What is total revenue?", "intent": "kpi"},
    {"q": "How many orders were placed?", "intent": "kpi"},
    {"q": "Show revenue by region", "intent": "breakdown"},
    {"q": "Break down revenue by category", "intent": "breakdown"},
    {"q": "What is the revenue trend over time?", "intent": "trend"},
    {"q": "Show monthly revenue", "intent": "trend"},
    {"q": "Top 5 categories by revenue", "intent": "top_n"},
    {"q": "Which regions have the most orders?", "intent": "breakdown"},
    {"q": "Average order value", "intent": "kpi"},
    {"q": "Revenue by segment", "intent": "breakdown"},
    {"q": "Top 3 regions by revenue", "intent": "top_n"},
    {"q": "Daily revenue over the year", "intent": "trend"},
    {"q": "Average order value by region", "intent": "breakdown"},
]


def main() -> None:
    cfg = get_config()["data"]
    rng = np.random.default_rng(cfg["seed"])
    out = resolve_path(cfg["processed_dir"])
    out.mkdir(parents=True, exist_ok=True)
    frames = build_frames(cfg, rng)

    db_path = resolve_path(cfg["db_path"])
    db_path.unlink(missing_ok=True)
    con = duckdb.connect(str(db_path))
    for name, df in frames.items():
        con.register(f"_{name}", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _{name}")
    con.close()

    (out / "questions.json").write_text(json.dumps(QUESTIONS), encoding="utf-8")
    print(
        json.dumps(
            {
                "tables": list(frames),
                "orders": len(frames["fact_orders"]),
                "questions": len(QUESTIONS),
            }
        )
    )


if __name__ == "__main__":
    main()
