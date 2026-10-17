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
from invoice_agents.graph import Context, process_invoice
from invoice_agents.llm.client import LLMUnavailable
from invoice_agents.presentation import (
    SEVERITY_BADGE,
    decision_badge,
    decision_label,
    group_findings,
    key_codes,
    paid_text,
    provenance_label,
    split_reasons,
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


def render_result(state) -> None:
    decision = state["decision"].value
    findings_all = state.get("findings", [])
    st.subheader(f"Decision: {decision_badge(decision)}")
    label = decision_label(decision, findings_all)
    if label != decision:
        st.write(label)
    pay = state.get("payment")
    if pay and pay["status"] == "paid":
        st.write(f"Payment: paid {pay['amount']:,.2f} to {pay['vendor']}")
    elif pay:
        st.write(f"Payment: not made ({pay['status']}: {pay['detail']})")
    else:
        st.write("Payment: none")

    split = split_reasons(findings_all)
    if split["execution"]:
        st.markdown("**\U0001f6e0\ufe0f System issues (not vendor or invoice defects)**")
        for line in split["execution"]:
            st.markdown(f"- {line}")
    groups = group_findings([f for f in findings_all if f.kind == "business"])
    st.markdown("**Business findings**")
    shown = False
    for sev in ("reject", "hold", "note"):
        if groups[sev]:
            shown = True
            st.markdown(f"{SEVERITY_BADGE[sev]}")
            for line in groups[sev]:
                st.markdown(f"- {line}")
    if not shown:
        st.write("No business findings.")

    st.markdown("**Step-by-step trace**")
    for i, step in enumerate(state["steps"], 1):
        with st.expander(
            f"{i}. {step['step']}", expanded=step["step"] in ("ingest", "resolve_items")
        ):
            for line in step["lines"]:
                st.text(line)
    inv = state.get("invoice")
    if inv:
        with st.expander("Extracted fields"):
            st.json(inv.model_dump(mode="json"))
    res = state.get("resolve")
    if res and res.tool_trace:
        with st.expander("search_inventory tool calls"):
            st.json(res.tool_trace)
    if res:
        st.markdown("**Item resolution tiers:** " + ", ".join(res.tiers))

    st.markdown("**LLM provenance by step**")
    calls = state.get("llm_calls", [])
    if not calls:
        st.write(f"- all steps: {provenance_label('code')}")
    for c in calls:
        label = provenance_label(c.get("provenance")) if c.get("provenance") else c.get("outcome")
        st.write(f"- {c['agent']} / {c['step']}: {label}")


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
            st.session_state["last_result"] = (state, ctx.run_id)
        finally:
            db.close()
    last = st.session_state.get("last_result")
    if last:
        state, run_id = last
        st.caption(f"Run {run_id}")
        render_result(state)


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
                    "Amount": r["total"],
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
                        "Total": r["total"],
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
                    st.markdown(f"- {f['message']}")
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


def main() -> None:
    st.set_page_config(page_title="Acme invoice agents", page_icon="\U0001f9fe", layout="wide")
    st.title("Acme invoice agents")
    mode, db_path = render_sidebar()
    t1, t2, t3 = st.tabs(["Process", "Batch", "Ledger & review queue"])
    with t1:
        tab_process(mode, db_path)
    with t2:
        tab_batch(mode, db_path)
    with t3:
        tab_ledger(db_path)


if __name__ == "__main__":
    main()
