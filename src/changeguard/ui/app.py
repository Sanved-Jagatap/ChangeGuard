"""Streamlit front end. Talks to the API only (no business logic here).
Run:  streamlit run src/changeguard/ui/app.py   (API must be running; set CG_API_URL)"""
from __future__ import annotations

import json
import os
import uuid

import pandas as pd
import requests
import streamlit as st

API = os.environ.get("CG_API_URL", "http://localhost:8000").rstrip("/")
st.set_page_config(page_title="ChangeGuard", layout="wide")


def call(method: str, path: str, **kw):
    headers = {"X-API-Token": st.session_state.get("token", "")} if st.session_state.get("token") else {}
    if "idem" in kw:
        headers["Idempotency-Key"] = kw.pop("idem")
    try:
        r = requests.request(method, f"{API}{path}", headers=headers, timeout=180, **kw)
    except requests.RequestException as e:
        st.error(f"API unreachable at {API}: {type(e).__name__}")
        st.stop()
    if r.status_code >= 400:
        try:
            detail = r.json()
        except ValueError:
            detail = r.text
        st.error(f"{r.status_code}: {detail}")
        return None
    return r.json()


with st.sidebar:
    st.title("ChangeGuard")
    st.caption("The model investigates. Code decides. Humans approve.")
    st.session_state["token"] = st.text_input("API token (optional)", type="password", value=st.session_state.get("token", ""))
    who = call("GET", "/whoami")
    health = call("GET", "/health")
    if who and health:
        st.write(f"**Role:** {who['role']}  \n**Model:** {health['llm_provider']} {health['llm_model'] or ''}")
        if health["llm_provider"].startswith("mock"):
            st.warning("Offline mock policy: not a real language model.")
    page = st.radio("Page", ["Investigate", "Approvals", "Audit trail", "Evaluation", "Data quality"])


def show_report(run: dict):
    rep = run.get("report") or {}
    facts = run.get("facts") or {}
    st.subheader(f"Result: {rep.get('outcome', '?')}  ({run['status']}, {run.get('stop_reason')})")
    st.write(rep.get("summary", ""))
    k = rep.get("key_figures") or {}
    cols = st.columns(4)
    for col, (label, key) in zip(cols, [("Affected items", "affected_count"), ("Demand exposure (units)", "exposure_units"),
                                        ("Available stock", "available_units"), ("Shortage (units)", "shortage_units")], strict=True):
        col.metric(label, "unavailable" if k.get(key) is None else k[key])
    if rep.get("recommended_component_id"):
        st.success(f"Candidate for engineering review: **{rep['recommended_component_id']}** (not an approved substitute)")
    if rep.get("refused_requests"):
        st.warning("Refused: " + "; ".join(rep["refused_requests"]))
    if facts.get("candidates"):
        st.markdown("**Deterministic eligibility (computed by code, not the model)**")
        st.dataframe(pd.DataFrame([{"candidate": c, "eligibility": v["eligibility"], "reasons": ", ".join(v["reasons"]),
                                    "best lead time (d)": v["best_lead_time_days"]} for c, v in facts["candidates"].items()]),
                     hide_index=True, use_container_width=True)
    if rep.get("unresolved"):
        st.markdown("**Unresolved / missing evidence**")
        st.write(", ".join(f"`{u}`" for u in rep["unresolved"]))
    cc = run.get("claim_check") or {}
    if cc.get("total"):
        st.caption(f"Evidence check: {cc['supported']}/{cc['total']} claims verified against tool outputs.")
        if cc["unsupported"]:
            st.error(f"Unsupported claims: {cc['unsupported']}")
    for label, key in (("Next steps", "next_steps"), ("Assumptions", "assumptions")):
        if rep.get(key):
            st.markdown(f"**{label}**")
            for x in rep[key]:
                st.write("- " + x)


def show_trace(run_id: str):
    steps = call("GET", f"/runs/{run_id}/steps") or []
    st.markdown("**Run trace**")
    st.dataframe(pd.DataFrame([{"#": s["idx"], "kind": s["kind"], "name": s["name"], "attempt": s["attempt"], "status": s["status"],
                                "ms": s["duration_ms"], "tokens in/out": f"{s['input_tokens'] or '-'}/{s['output_tokens'] or '-'}",
                                "error": s["error_kind"] or ""} for s in steps]), hide_index=True, use_container_width=True)
    for s in steps:
        if s["kind"] in ("tool", "guard"):
            with st.expander(f"S{s['idx']} {s['name']} [{s['status']}]"):
                st.json({"input": s["input"], "output": s["output"], "error": s["error_message"]})


def show_actions(change_id: str):
    acts = call("GET", f"/changes/{change_id}/actions") or []
    if not acts:
        st.info("No action was proposed for this change.")
    for a in acts:
        with st.container(border=True):
            st.markdown(f"**{a['action_type']}** `{a['action_id']}`  status: **{a['status']}**")
            st.json(a["payload"])
            st.code(f"payload hash: {a['payload_hash']}")
            if a["status"] == "proposed":
                c1, c2, c3 = st.columns(3)
                reason = st.text_input("Reason", key=f"r{a['action_id']}")
                if c1.button("Approve this exact payload", key=f"ap{a['action_id']}"):
                    call("POST", f"/actions/{a['action_id']}/decision", json={"decision": "approved", "payload_hash": a["payload_hash"], "reason": reason})
                    st.rerun()
                if c2.button("Reject", key=f"rj{a['action_id']}"):
                    call("POST", f"/actions/{a['action_id']}/decision", json={"decision": "rejected", "payload_hash": a["payload_hash"], "reason": reason})
                    st.rerun()
            if a["status"] == "approved" and st.button("Execute in sandbox", key=f"ex{a['action_id']}"):
                call("POST", f"/actions/{a['action_id']}/execute")
                st.rerun()
            if a.get("execution"):
                e = a["execution"]
                st.write(f"Execution: {e['outcome']}; verification: **{e['verification_status']}** ({e['verification_detail']})")


if page == "Investigate":
    st.header("Investigate an engineering change")
    scen = call("GET", "/scenarios") or []
    cols = st.columns(len(scen) or 1)
    for col, s in zip(cols, scen, strict=False):
        if col.button(s["title"], help=s["blurb"], use_container_width=True):
            st.session_state["form"] = s["request"]
    f = st.session_state.get("form", {"component_id": "CMP-104", "change_type": "discontinued", "target_market": "EU", "urgency": "normal",
                                      "description": "Supplier end-of-life notice received for this part."})
    c1, c2, c3, c4 = st.columns(4)
    comp = c1.text_input("Component ID", f["component_id"])
    ctype = c2.selectbox("Change type", ["discontinued", "supplier_change", "revision", "compliance_change", "shortage"],
                         index=["discontinued", "supplier_change", "revision", "compliance_change", "shortage"].index(f["change_type"]))
    market = c3.selectbox("Target market", ["EU", "US", "UK", "(any)"], index=["EU", "US", "UK"].index(f["target_market"]) if f.get("target_market") in ("EU", "US", "UK") else 3)
    urgency = c4.selectbox("Urgency", ["low", "normal", "high", "critical"], index=["low", "normal", "high", "critical"].index(f.get("urgency", "normal")))
    desc = st.text_area("Description", f["description"], height=90)
    mlt = st.number_input("Max lead time (days, 0 = none)", min_value=0, max_value=365, value=0)
    runner = st.radio("Runner", ["agent", "baseline"], horizontal=True, help="agent = model-driven loop; baseline = fixed pipeline")
    if st.button("Submit and investigate", type="primary"):
        body = {"component_id": comp, "change_type": ctype, "description": desc, "urgency": urgency,
                "target_market": None if market == "(any)" else market, "constraints": {"max_lead_time_days": int(mlt)} if mlt else {}}
        ch = call("POST", "/changes", json=body, idem=str(uuid.uuid4()))
        if ch:
            with st.spinner("Investigating..."):
                run = call("POST", f"/changes/{ch['change_id']}/runs", json={"runner": runner})
            if run:
                st.session_state.update(change_id=ch["change_id"], run=run)
    if st.session_state.get("run"):
        run = st.session_state["run"]
        t1, t2, t3 = st.tabs(["Report", "Trace", "Proposed action"])
        with t1:
            show_report(run)
        with t2:
            show_trace(run["run_id"])
            st.caption(f"{run['step_count']} steps, tokens {run.get('input_tokens')}/{run.get('output_tokens')}, cost {run.get('cost_usd') if run.get('cost_usd') is not None else 'unavailable'}")
        with t3:
            show_actions(st.session_state["change_id"])

elif page == "Approvals":
    st.header("Changes and their proposed actions")
    changes = call("GET", "/changes") or []
    if changes:
        pick = st.selectbox("Change", [f"{c['change_id']}  {c['component_id']}  {c['status']}" for c in changes])
        show_actions(pick.split()[0])

elif page == "Audit trail":
    st.header("Audit trail (append-only)")
    ev = call("GET", "/audit") or []
    st.dataframe(pd.DataFrame([{"seq": e["seq"], "time": e["ts"], "actor": e["actor"], "event": e["event_type"], "change": e["change_id"],
                                "action": e["action_id"], "detail": json.dumps(e["detail"])[:160]} for e in ev]), hide_index=True, use_container_width=True)

elif page == "Evaluation":
    st.header("Benchmark results")
    pubs = call("GET", "/bench/published") or []
    if not pubs:
        st.info("No published result files found in benchmarks/published/.")
    for p in pubs:
        a, m = p["aggregate"]["overall"], p["meta"]
        title = "Offline mock (plumbing only)" if m.get("plumbing_only") else f"{m['runner']} {m['provider']} {m['model']}"
        with st.container(border=True):
            st.subheader(title)
            c = st.columns(5)
            c[0].metric("Task success", f"{100 * a['success_rate']:.0f}%", f"CI {a['ci95'][0]:.2f}-{a['ci95'][1]:.2f}")
            c[1].metric("Unsafe recommendations", f"{100 * (a['unsafe_recommendation_rate'] or 0):.0f}%")
            c[2].metric("Unauthorised writes", a["unauthorized_writes"])
            c[3].metric("Mean turns", "n/a" if a["mean_llm_turns"] is None else f"{a['mean_llm_turns']:.1f}")
            c[4].metric("Cost (USD)", "n/a" if a["total_cost_usd"] is None else f"{a['total_cost_usd']:.4f}")
            st.dataframe(pd.DataFrame([{"family": k, "passed": v["success"], "total": v["n"]} for k, v in p["aggregate"]["by_family"].items()]), hide_index=True)
            st.caption(f"dataset {m['dataset_version']} / {m['dataset_hash']}, {m['n_cases']} cases, git {m.get('git')}")

else:
    st.header("Data quality checks")
    st.dataframe(pd.DataFrame(call("GET", "/quality") or []), hide_index=True, use_container_width=True)
    st.caption("Some findings are intentional: the seed data contains anomalies the system must handle.")
