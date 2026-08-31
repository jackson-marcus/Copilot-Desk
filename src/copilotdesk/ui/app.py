"""Streamlit demo: ask the analyst, watch the filter trace, see the chart + narrative."""

from __future__ import annotations

import os

import httpx
import pandas as pd
import plotly.express as px
import streamlit as st

API_URL = os.environ.get("COPILOTDESK_API_URL", "http://localhost:8480")

SAMPLES = [
    "What is total revenue?",
    "Show revenue by region",
    "What is the revenue trend over time?",
    "Top 5 categories by revenue",
    "Top 3 regions by revenue",
    "Average order value by region",
]

st.set_page_config(page_title="copilotdesk", page_icon="📊", layout="wide")
st.title("📊 copilotdesk")
st.caption(
    "Filter pipeline: plan → SQL (guarded) → execute → chart → reconcile → narrate, fully traced"
)


def _ok() -> bool:
    try:
        return httpx.get(f"{API_URL}/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


if not _ok():
    st.error(f"API not reachable at {API_URL}. Start it with `make api`.")
    st.stop()

tab_ask, tab_report, tab_schema = st.tabs(["💬 Ask", "📈 Eval report", "🗄️ Schema"])

with tab_ask:
    question = st.selectbox("Sample questions", SAMPLES)
    custom = st.text_input("...or type your own", "")
    q = custom.strip() or question
    if st.button("Ask the analyst", type="primary"):
        r = httpx.post(f"{API_URL}/ask", json={"question": q}, timeout=60)
        if r.status_code != 200:
            st.error(r.json().get("detail", r.text))
        else:
            body = r.json()
            st.markdown(f"### {body['narrative']}")
            _badge = {"verified": "✅", "qualified": "⚠️", "unverified": "❓"}
            st.caption(
                f"{_badge.get(body['verdict'], '')} reconciliation: **{body['verdict']}** "
                f"— {len(body['checks'])} check(s) run, "
                f"{len(body['unchecked'])} not applicable"
            )
            df = pd.DataFrame(body["data"])
            if body["chart"] == "line" and len(df.columns) >= 2:
                st.plotly_chart(
                    px.line(df, x=df.columns[0], y=df.columns[1]), use_container_width=True
                )
            elif body["chart"] == "bar" and len(df.columns) >= 2:
                st.plotly_chart(
                    px.bar(df, x=df.columns[0], y=df.columns[1]), use_container_width=True
                )
            elif not df.empty:
                st.metric(df.columns[-1], f"{df.iloc[0][df.columns[-1]]:,.2f}")
            st.code(body["sql"], language="sql")
            with st.expander("🧮 Reconciliation"):
                for check in body["checks"]:
                    icon = "✅" if check["status"] == "ok" else "⚠️"
                    st.markdown(f"{icon} **{check['check']}** — {check['detail']}")
                    if check["evidence"]:
                        st.json(check["evidence"], expanded=False)
                for skipped in body["unchecked"]:
                    st.markdown(f"➖ **{skipped['check']}** — not applicable: {skipped['reason']}")
            with st.expander("🔍 Filter trace"):
                for step in body["trace"]:
                    st.markdown(f"**{step['agent']}** — {step.get('duration_ms', 0):.1f} ms")
                    st.json(step["output"])

with tab_report:
    body = httpx.get(f"{API_URL}/report", timeout=30).json()
    m = body["metrics"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Planner intent accuracy", f"{m['intent_accuracy']:.0%}")
    c2.metric("Guardrail pass rate", f"{m['guardrail_pass_rate']:.0%}")
    c3.metric("Execution rate", f"{m['execution_rate']:.0%}")
    c4.metric("Verified answers", f"{m.get('verified_rate', 0):.0%}")
    st.dataframe(
        pd.DataFrame(body["results"])[
            ["question", "expected_intent", "planned_intent", "executed", "verdict"]
        ],
        hide_index=True,
        use_container_width=True,
    )

with tab_schema:
    body = httpx.get(f"{API_URL}/schema", timeout=30).json()
    for table, cols in body["tables"].items():
        st.subheader(table)
        st.dataframe(pd.DataFrame(cols), hide_index=True, use_container_width=True)
