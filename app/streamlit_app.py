"""Thin Streamlit front end. All processing goes through the same functions as the CLI
(make_client, process_invoice, run_batch, review_hold); this file only renders results."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from invoice_agents.batch import collect_files, result_record, run_batch
from invoice_agents.cli import make_client
from invoice_agents.db import Database
from invoice_agents.evaluation import CLASSES, counts, evaluate
from invoice_agents.graph import TIER_TEXT, Context, process_invoice
from invoice_agents.llm.client import LLMUnavailable
from invoice_agents.presentation import (
    SEVERITY_BADGE,
    decision_badge,
    decision_label,
    key_codes,
    money,
    paid_text,
    plain,
    provenance_label,
)
from invoice_agents.review import (
    ReviewError,
    approval_eligibility,
    reasons_for,
    review_hold,
    review_queue,
)

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIRS = [ROOT / "data" / "invoices", ROOT / "data" / "synthetic"]
FIXTURES = str(ROOT / "fixtures" / "llm")


def sample_files() -> list[Path]:
    return collect_files(SAMPLE_DIRS)


def open_db(path: str) -> Database:
    return Database(path)


def render_sidebar() -> tuple[str, str]:
    load_dotenv()
    st.sidebar.header("Settings")
    has_key = bool(os.environ.get("LLM_API_KEY", "").strip())
    modes = ["fixture", "none", "live"]
    mode = st.sidebar.selectbox(
        "LLM mode",
        modes,
        help="fixture: replay hand-written (authored) fixtures. none: code only. live: real model.",
    )
    if mode == "live" and not has_key:
        st.sidebar.error("Live mode is unavailable: LLM_API_KEY is not set (see .env.example).")
    db_path = st.sidebar.text_input("Database path", value=str(ROOT / "acme_ui.db"))
    if st.sidebar.button("Reset database"):
        st.session_state["confirm_reset"] = True
    if st.session_state.get("confirm_reset"):
        st.sidebar.warning("This wipes the ledger, payments, reviews and audit trail.")
        if st.sidebar.button("Yes, reset the database now"):
            db = open_db(db_path)
            db.reset()
            db.close()
            st.session_state["confirm_reset"] = False
            st.session_state.pop("last_result", None)
            st.sidebar.success("Database reset and reseeded.")
        if st.sidebar.button("Cancel reset"):
            st.session_state["confirm_reset"] = False
    return mode, db_path


def get_client(mode: str):
    try:
        return make_client(mode, FIXTURES)
    except LLMUnavailable as exc:
        st.error(f"LLM unavailable: {exc}")
        return None


def _tagged(f) -> str:
    d = f.model_dump()
    return f"{plain(d)}  `{d['code']}`"


def render_result(state, ctx_info: dict | None = None) -> None:
    decision = state["decision"].value
    findings_all = state.get("findings", [])
    st.subheader(f"Decision: {decision_badge(decision)}")
    label = decision_label(decision, findings_all)
    if label != decision:
        st.write(label)
    pay = state.get("payment")
    if pay and pay["status"] == "paid":
        st.write(
            f"Payment: paid {money(pay['amount'], pay.get('currency', 'USD'))} to {pay['vendor']}"
        )
        if pay.get("output"):
            st.caption(f"mock_payment output: {pay['output']}")
    elif pay:
        st.write(f"Payment: not made ({pay['status']}: {pay['detail']})")
    else:
        st.write("Payment: none")

    sys_issues = [f for f in findings_all if f.kind == "execution" and f.severity != "note"]
    if sys_issues:
        st.markdown("**\U0001f6e0\ufe0f System issues (not vendor or invoice defects)**")
        for f in sys_issues:
            st.markdown(f"- {_tagged(f)}")
    st.markdown("**Business findings**")
    shown = False
    for sev in ("reject", "hold", "note"):
        items = [f for f in findings_all if f.kind == "business" and f.severity == sev]
        if items:
            shown = True
            st.markdown(SEVERITY_BADGE[sev])
            for f in items:
                st.markdown(f"- {_tagged(f)}")
    if not shown:
        st.write("No business findings.")

    inv = state.get("invoice")
    res = state.get("resolve")
    if inv:
        cur = inv.currency
        st.markdown("**Line items and resolved SKUs**")
        rows = []
        for i, li in enumerate(inv.line_items):
            sku = res.proposed[i] if res else None
            tier = res.tiers[i] if res else "unresolved"
            rows.append(
                {
                    "Item as written": li.raw_name,
                    "Resolved SKU": sku or "not resolved",
                    "How": TIER_TEXT[tier],
                    "Qty": li.quantity,
                    "Unit price": money(li.unit_price, cur),
                    "Line total": money(li.line_total, cur),
                }
            )
        st.dataframe(rows, use_container_width=True)
        st.write(
            f"Subtotal {money(inv.subtotal, cur)}, tax {money(inv.tax, cur)}, "
            f"total {money(inv.total, cur)}"
        )

    st.markdown("**Step-by-step trace**")
    for i, step in enumerate(state["steps"], 1):
        with st.expander(
            f"{i}. {step['step']}", expanded=step["step"] in ("ingest", "resolve_items")
        ):
            for line in step["lines"]:
                st.text(line)
    if res and res.tool_trace:
        with st.expander("search_inventory tool calls"):
            st.json(res.tool_trace)

    st.markdown("**LLM provenance by step**")
    calls = state.get("llm_calls", [])
    if not calls:
        st.write(f"- all steps: {provenance_label('code')}")
    for c in calls:
        label = provenance_label(c.get("provenance")) if c.get("provenance") else c.get("outcome")
        st.write(f"- {c['agent']} / {c['step']}: {label}")

    with st.expander("Developer details (collapsed by default)"):
        if inv:
            st.json(inv.model_dump(mode="json"))
        st.write(f"Absolute path: {Path(state['path']).resolve()}")
        st.write(f"Document fingerprint: {state['doc'].fingerprint}")
        if ctx_info:
            st.write(f"Run id: {ctx_info['run_id']}  |  Database: {ctx_info['db']}")


def tab_process(mode: str, db_path: str) -> None:
    source = st.radio("Invoice source", ["Sample file", "Upload a file"], horizontal=True)
    path: str | None = None
    if source == "Sample file":
        files = sample_files()
        choice = st.selectbox(
            "Sample invoice", files, format_func=lambda p: f"{p.parent.name}/{p.name}"
        )
        path = str(choice)
    else:
        up = st.file_uploader("Invoice file", type=["txt", "pdf", "csv", "json", "xml"])
        if up is not None:
            tmp = Path(tempfile.mkdtemp(prefix="invoice_upload_")) / Path(up.name).name
            tmp.write_bytes(up.getvalue())  # temp dir only, never data/
            path = str(tmp)
    if st.button(
        "Run", disabled=path is None or (mode == "live" and not os.environ.get("LLM_API_KEY"))
    ):
        client = get_client(mode) if mode != "none" else None
        if mode != "none" and client is None:
            return
        db = open_db(db_path)
        try:
            ctx = Context(db=db, client=client, llm_mode=mode)
            state = process_invoice(path, ctx)
            st.session_state["last_result"] = (state, {"run_id": ctx.run_id, "db": db_path})
        finally:
            db.close()
    last = st.session_state.get("last_result")
    if last:
        state, info = last
        render_result(state, info)


def tab_batch(mode: str, db_path: str) -> None:
    fresh = st.checkbox("Reset the database before the batch", value=True)
    st.caption("Order: data/invoices then data/synthetic, each sorted by file name.")
    if st.button("Run all samples"):
        client = get_client(mode) if mode != "none" else None
        if mode != "none" and client is None:
            return
        db = open_db(db_path)
        try:
            if fresh:
                db.reset()
            results = run_batch(sample_files(), db, client, mode)
            st.session_state["batch"] = [result_record(s, c.run_id) for s, c in results]
        finally:
            db.close()
    rows = st.session_state.get("batch")
    if rows:
        st.dataframe(
            [
                {
                    "File": Path(r["file"]).name,
                    "Decision": decision_badge(r["decision"]),
                    "Cause": r["cause"],
                    "Key findings": key_codes(r),
                    "Amount": money(r["total"], r["currency"] or "USD"),
                    "Paid?": paid_text(r),
                }
                for r in rows
            ],
            use_container_width=True,
        )


def tab_ledger(db_path: str) -> None:
    db = open_db(db_path)
    try:
        st.markdown("**Ledger**")
        ledger = db.ledger()
        if ledger:
            st.dataframe(
                [
                    {
                        "ID": r["id"],
                        "File": Path(r["source_file"]).name,
                        "Vendor": r["vendor"],
                        "Total": money(r["total"], r["currency"] or "USD"),
                        "Decision": decision_badge(r["decision"]),
                        "Review": r["review_status"] or "-",
                        "Duplicate of": r["duplicate_of"],
                    }
                    for r in ledger
                ],
                use_container_width=True,
            )
        else:
            st.write("The ledger is empty.")

        st.markdown("**Review queue (HOLD)**")
        queue = review_queue(db)
        if not queue:
            st.write("Nothing is waiting for review.")
        for r in queue:
            name = Path(r["source_file"]).name
            with st.expander(f"#{r['id']} {name} ({r['vendor'] or 'unknown vendor'})"):
                for f in reasons_for(r):
                    st.markdown(f"- {plain(f)}  `{f['code']}`")
                elig = approval_eligibility(r)
                if not elig.ok:
                    st.warning("\u26d4 Approve is disabled: " + " ".join(elig.reasons))
                with st.form(f"review_{r['id']}"):
                    reviewer = st.text_input(
                        "Reviewer name", value="reviewer", key=f"who_{r['id']}"
                    )
                    note = st.text_input("Reviewer note (required)", key=f"note_{r['id']}")
                    approve = st.form_submit_button("Approve anyway", disabled=not elig.ok)
                    reject = st.form_submit_button("Reject")
                if approve or reject:
                    try:
                        res = review_hold(
                            db, r["id"], "approve" if approve else "reject", note, reviewer
                        )
                        st.success(f"Recorded: {res.outcome}. {res.detail}")
                    except ReviewError as exc:
                        st.error(str(exc))
        reviews = db.reviews()
        if reviews:
            st.markdown("**Review history**")
            st.dataframe(reviews, use_container_width=True)
    finally:
        db.close()


def tab_evaluation() -> None:
    st.markdown("**Developer / evaluation view.** Not part of invoice processing.")
    st.caption(
        "Runs all 22 sample documents in one execution mode in a throwaway database and compares "
        "each decision with the PLAN section 4 baseline (a documented policy baseline, not ground "
        "truth). Modes are never mixed: each is run and shown separately."
    )
    has_key = bool(os.environ.get("LLM_API_KEY", "").strip())
    mode = st.radio("Execution mode to evaluate", ["none", "fixture", "live"], horizontal=True)
    if mode == "live":
        if not has_key:
            st.info("Live: not run \u2013 no API key (LLM_API_KEY is not set).")
        else:
            st.warning("Live evaluation is not implemented in this view yet.")
        return
    cache = st.session_state.setdefault("eval_cache", {})
    if st.button(f"Run evaluation ({mode} mode)"):
        cache[mode] = evaluate(mode)
    rows = cache.get(mode)
    if not rows:
        st.write("No evaluation has been run for this mode yet.")
        return
    st.subheader(f"Mode: {mode}")
    st.dataframe(
        [
            {
                "File": r["file"],
                "Actual": decision_badge(r["actual"]),
                "\u00a74 baseline": r["baseline"],
                "Class": r["class"],
                "Cause": r["cause"],
                "Codes": ", ".join(r["codes"]) or "-",
            }
            for r in rows
        ],
        use_container_width=True,
    )
    c = counts(rows)
    st.write(
        "Counts: "
        + ", ".join(f"{k} {c.get(k, 0)}" for k in CLASSES if c.get(k, 0))
        + f" (total {sum(c.values())})"
    )


def main() -> None:
    st.set_page_config(page_title="Acme invoice agents", page_icon="\U0001f9fe", layout="wide")
    st.title("Acme invoice agents")
    mode, db_path = render_sidebar()
    t1, t2, t3, t4 = st.tabs(
        ["Process", "Batch", "Ledger & review queue", "Evaluation (vs \u00a74 baseline)"]
    )
    with t1:
        tab_process(mode, db_path)
    with t2:
        tab_batch(mode, db_path)
    with t3:
        tab_ledger(db_path)
    with t4:
        tab_evaluation()


if __name__ == "__main__":
    main()
