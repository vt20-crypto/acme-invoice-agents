"""Streamlit front end. All processing goes through the same functions as the CLI
(make_client, process_invoice, run_batch, review_hold, evaluate); this file only renders results.
Layout and colors follow the design mockup: warm neutrals, soft cards, pill status badges."""

from __future__ import annotations

import html
import os
import tempfile
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from invoice_agents.batch import collect_files, result_record, run_batch
from invoice_agents.db import Database
from invoice_agents.evaluation import evaluate
from invoice_agents.graph import Context, process_invoice
from invoice_agents.llm.client import LLMUnavailable, make_client
from invoice_agents.presentation import (
    decision_badge,
    extracted_lines,
    money,
    plain,
    provenance_label,
    timeline,
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
DB_PATH = os.environ.get("ACME_UI_DB", str(ROOT / "acme_ui.db"))
CACHE_DIR = ROOT / "cache" / "llm"

CSS = """
<style>
.stApp { background: #fcfcfb; }
html, body, [class*="st-"] { font-family: ui-sans-serif, -apple-system, "Segoe UI", sans-serif; }
.card { background:#f9f9f7; border:0.5px solid #e4e3dd; border-radius:12px; padding:1rem 1.25rem;
        margin-bottom:10px; color:#20201f; }
.card.ok { border-color:#aee0a9; } .card.hold { border-color:#f9c868; }
.card.no { border-color:#f4abab; } .card.dup { border-color:#c3c2b7; }
.pill { font-size:13px; padding:3px 10px; border-radius:8px; white-space:nowrap; font-weight:500; }
.pill.ok { background:#e5f4e4; color:#074506; } .pill.hold { background:#f9eeda; color:#734500; }
.pill.no { background:#fbebeb; color:#8e2626; } .pill.dup { background:#f3f3f0; color:#52514e; }
.row { display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap; }
.big { font-size:24px; font-weight:500; } .who { margin:10px 0 2px; font-size:15px; font-weight:500; }
.why { margin:0; font-size:14px; color:#6d6b67; }
.step { display:flex; gap:10px; font-size:14px; padding:5px 0; color:#20201f; }
.step .i { width:20px; text-align:center; font-weight:600; }
.step .check { color:#074506; } .step .warn { color:#734500; }
.step .fail { color:#8e2626; } .step .dash { color:#97958d; }
.m { background:#f3f3f0; border-radius:8px; padding:10px 12px; }
.m .k { font-size:12px; color:#6d6b67; margin:0; } .m .v { font-size:20px; font-weight:500; margin:0; }
.lbl { font-size:12px; color:#6d6b67; margin:1rem 0 6px; }
.mono { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size:12px; white-space:pre-wrap; }
</style>
"""

TONE = {"APPROVE": "ok", "HOLD": "hold", "REJECT": "no", "DUPLICATE": "dup"}
ICON = {"check": "✓", "warn": "⚠", "fail": "✗", "dash": "—"}
ICON_WORD = {"check": "done", "warn": "needs attention", "fail": "failed", "dash": "not applicable"}


def esc(x) -> str:
    return html.escape(str(x))


def sample_files() -> list[Path]:
    return collect_files(SAMPLE_DIRS)


def pill(decision: str, big: bool = False) -> str:
    style = ' style="font-size:15px;padding:4px 12px"' if big else ""
    return f'<span class="pill {TONE[decision]}"{style}>{esc(decision_badge(decision))}</span>'


def metric(label: str, value: str) -> str:
    return f'<div class="m"><p class="k">{esc(label)}</p><p class="v">{esc(value)}</p></div>'


# ---------------------------------------------------------------------------- sidebar


def sidebar() -> str:
    load_dotenv()
    has_key = bool(os.environ.get("LLM_API_KEY", "").strip())
    st.sidebar.header("Acme invoice agents")
    modes = ["cache", "none"] + (["live"] if has_key else [])
    mode = st.sidebar.selectbox(
        "LLM mode",
        modes,
        help="cache: replay recorded Grok responses. none: code only. live: call Grok (costs money).",
    )
    if not has_key:
        st.sidebar.caption("Live is unavailable: no API key is set.")
    if st.sidebar.button("Reset database"):
        st.session_state["confirm_reset"] = True
    if st.session_state.get("confirm_reset"):
        st.sidebar.warning("This wipes the ledger, payments and reviews.")
        if st.sidebar.button("Yes, reset now"):
            db = Database(DB_PATH)
            db.reset()
            db.close()
            for k in ("last_result", "dashboard", "confirm_reset"):
                st.session_state.pop(k, None)
            st.sidebar.success("Database reset.")
        if st.sidebar.button("Cancel"):
            st.session_state["confirm_reset"] = False
    return mode


def client_for(mode: str):
    try:
        return make_client(mode, CACHE_DIR)
    except LLMUnavailable as exc:
        st.error(f"The LLM is not available: {exc}")
        return None


# ---------------------------------------------------------------------------- process


def decision_card(rec: dict) -> str:
    d = rec["decision"]
    cur = rec["currency"] or "USD"
    who = " · ".join(x for x in (rec["vendor"], rec["invoice_number"]) if x) or "Unknown vendor"
    return (
        f'<div class="card {TONE[d]}"><div class="row">{pill(d, True)}'
        f'<span class="big">{esc(money(rec["total"], cur))}</span></div>'
        f'<p class="who">{esc(who)}</p><p class="why">{esc(rec["reason"])}</p></div>'
    )


def render_timeline(state: dict) -> None:
    rows = "".join(
        f'<div class="step"><span class="i {s["icon"]}" aria-label="{ICON_WORD[s["icon"]]}">'
        f"{ICON[s['icon']]}</span><span><b>{esc(s['step'])}</b> · {esc(s['line'])}</span></div>"
        for s in timeline(state)
    )
    st.markdown(f'<div style="margin-top:6px">{rows}</div>', unsafe_allow_html=True)


def render_result(state: dict, rec: dict) -> None:
    st.markdown(decision_card(rec), unsafe_allow_html=True)
    render_timeline(state)
    left, right = st.columns(2)
    text = state["doc"].raw_text
    with left:
        st.markdown('<div class="lbl">Source document</div>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="m mono">{esc(text[:1800])}{"…" if len(text) > 1800 else ""}</div>',
            unsafe_allow_html=True,
        )
    with right:
        st.markdown('<div class="lbl">Extracted fields</div>', unsafe_allow_html=True)
        body = "<br>".join(esc(x) for x in extracted_lines(state))
        st.markdown(f'<div class="m" style="font-size:13px">{body}</div>', unsafe_allow_html=True)
    if rec["decision"] == "REJECT" and rec["vendor_message"]:
        with st.expander("Message to the vendor (template, not sent)"):
            st.text(rec["vendor_message"])
    with st.expander("Technical details"):
        st.write(f"File: {state['path']}  \nFingerprint: {state['doc'].fingerprint}")
        calls = state.get("llm_calls", [])
        st.write(
            "LLM steps: "
            + (
                ", ".join(f"{c['agent']} ({provenance_label(c.get('provenance'))})" for c in calls)
                or provenance_label("code")
            )
        )
        for f in state.get("findings", []):
            st.write(f"- {plain(f)}  `{f.code}` ({f.severity}, {f.kind})")
        for step in state["steps"]:
            st.text(step["step"] + "\n  " + "\n  ".join(step["lines"]))
        res = state.get("resolve")
        if res and res.tool_trace:
            st.json(res.tool_trace)


def tab_process(mode: str) -> None:
    source = st.radio("Invoice source", ["Pick a sample", "Upload a file"], horizontal=True)
    path = None
    if source == "Pick a sample":
        files = sample_files()
        path = str(st.selectbox("Sample invoice", files, format_func=lambda p: p.name))
    else:
        up = st.file_uploader("Invoice file", type=["txt", "pdf", "csv", "json", "xml"])
        if up is not None:
            tmp = Path(tempfile.mkdtemp(prefix="invoice_upload_")) / Path(up.name).name
            tmp.write_bytes(up.getvalue())  # temp dir only, never data/
            path = str(tmp)
    if st.button("Run", disabled=path is None):
        client = client_for(mode)
        if mode != "none" and client is None:
            return
        db = Database(DB_PATH)
        try:
            ctx = Context(db=db, client=client, llm_mode=mode)
            state = process_invoice(path, ctx)
            st.session_state["last_result"] = (state, result_record(state, ctx.run_id))
        finally:
            db.close()
    last = st.session_state.get("last_result")
    if last:
        render_result(*last)


# ---------------------------------------------------------------------------- dashboard


def tab_dashboard(mode: str) -> None:
    st.caption("Runs every sample invoice in the defined order against a fresh database.")
    if st.button("Run all samples"):
        client = client_for(mode)
        if mode != "none" and client is None:
            return
        db = Database(DB_PATH)
        try:
            db.reset()
            results = run_batch(sample_files(), db, client, mode)
            st.session_state["dashboard"] = [result_record(s, c.run_id) for s, c in results]
        finally:
            db.close()
    recs = st.session_state.get("dashboard")
    if not recs:
        st.write("Nothing has been run yet.")
        return

    def total_of(decision: str) -> float:
        return sum(
            r["total"] or 0
            for r in recs
            if r["decision"] == decision and (r["currency"] or "USD") == "USD"
        )

    paid = sum(
        (r["payment"] or {}).get("amount", 0)
        for r in recs
        if r["payment"] and r["payment"]["status"] == "paid"
    )
    n = lambda d: sum(r["decision"] == d for r in recs)  # noqa: E731
    cards = [
        ("Processed", str(len(recs))),
        ("Auto-approved", str(n("APPROVE"))),
        ("Needs review", str(n("HOLD"))),
        ("Rejected", str(n("REJECT"))),
        ("$ paid", money(paid)),
        ("$ held", money(total_of("HOLD"))),
    ]
    cols = st.columns(len(cards))
    for col, (k, v) in zip(cols, cards, strict=True):
        col.markdown(metric(k, v), unsafe_allow_html=True)
    st.markdown('<div class="lbl">Invoices</div>', unsafe_allow_html=True)
    f1, f2 = st.columns([2, 3])
    wanted = f1.multiselect(
        "Show decisions", ["APPROVE", "HOLD", "REJECT", "DUPLICATE"],
        default=["APPROVE", "HOLD", "REJECT", "DUPLICATE"],
        format_func=lambda d: decision_badge(d),
    )  # fmt: skip
    needle = f2.text_input("Search vendor, invoice or reason").strip().lower()
    rows = []
    for r in recs:
        line = {
            "Invoice": r["invoice_number"] or Path(r["file"]).name,
            "Vendor": r["vendor"] or "—",
            "Decision": decision_badge(r["decision"]),
            "Total": money(r["total"], r["currency"] or "USD"),
            "Reason": r["reason"],
        }
        if r["decision"] in wanted and (needle in " ".join(map(str, line.values())).lower()):
            rows.append(line)
    st.dataframe(rows, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------- review queue


def suggestion(codes: set[str]) -> str:
    if "non_usd" in codes:
        return "Confirm with treasury, then pay manually. Payments only support US dollars."
    if "revision" in codes:
        return "Compare with the original invoice and confirm the corrected amount."
    if "stock_exceeded" in codes:
        return "Check with the warehouse whether the extra stock is on its way."
    if codes & {"unknown_items", "item_needs_mapping"}:
        return "Ask the vendor which catalogue item this is."
    return "Review the reasons and decide."


def tab_review() -> None:
    db = Database(DB_PATH)
    try:
        queue = review_queue(db)
        if not queue:
            st.write("Nothing is waiting for review.")
        for r in queue:
            reasons = reasons_for(r)
            elig = approval_eligibility(r)
            cur = r["currency"] or "USD"
            st.markdown(
                f'<div class="card hold"><div class="row"><b>{esc(r["vendor"] or "Unknown vendor")}'
                f" · {esc(r['invoice_number'] or '—')}</b>"
                f'<span class="big">{esc(money(r["total"], cur))}</span></div>'
                f'<p class="why">{esc("; ".join(plain(f) for f in reasons))}</p>'
                f'<p class="why"><b>Suggested:</b> {esc(suggestion({f["code"] for f in reasons}))}</p></div>',
                unsafe_allow_html=True,
            )
            if not elig.ok:
                st.warning("Approve is unavailable: " + " ".join(elig.reasons))
            with st.form(f"review_{r['id']}"):
                note = st.text_input("Reviewer note (required)", key=f"note_{r['id']}")
                confirm = st.checkbox(
                    f"I confirm paying {money(r['total'], cur)} to {r['vendor'] or 'this vendor'}",
                    key=f"confirm_{r['id']}",
                    disabled=not elig.ok,
                )
                c1, c2 = st.columns(2)
                approve = c1.form_submit_button("Approve and pay", disabled=not elig.ok)
                reject = c2.form_submit_button("Reject")
            if approve or reject:
                try:
                    if approve and not confirm:
                        raise ReviewError("Tick the confirmation box before paying.")
                    res = review_hold(db, r["id"], "approve" if approve else "reject", note)
                    st.success({"approved_paid": "Approved and paid.", "rejected": "Rejected.",
                                "approved_not_paid": "Approved; it had already been paid."}.get(res.outcome, res.outcome))  # fmt: skip
                except ReviewError as exc:
                    st.error(str(exc))
    finally:
        db.close()


# ---------------------------------------------------------------------------- evaluation


def tab_evaluation(mode: str) -> None:
    st.caption(
        "Expected decisions (tests/regression/expected.yaml) against what this mode actually does, "
        "for all sample invoices. Developer view."
    )
    if mode == "live":
        st.warning("Live mode makes paid API calls. Use cache mode for a free, repeatable check.")
    if st.button(f"Run evaluation ({mode} mode)"):
        client = client_for(mode)
        if mode != "none" and client is None:
            return
        st.session_state["eval"] = (mode, evaluate(mode, CACHE_DIR))
    got = st.session_state.get("eval")
    if not got:
        st.write("No evaluation has been run yet.")
        return
    ran_mode, ev = got
    bad_pay = [r["file"] for r in ev["rows"] if r["paid"] and r["actual"] != "APPROVE"]
    st.markdown(
        f'<div class="card"><b>{esc(ev["summary"])}</b> · mode {esc(ran_mode)} · '
        f"{len(bad_pay)} invalid payments</div>",
        unsafe_allow_html=True,
    )
    st.dataframe(
        [
            {
                "File": r["file"],
                "Expected": decision_badge(r["expected"]) if r["expected"] else "—",
                "Actual": decision_badge(r["actual"]),
                "Match": "✓ match" if r["match"] else "✗ differs",
                "Why": r["reason"],
            }
            for r in ev["rows"]
        ],
        use_container_width=True,
        hide_index=True,
    )


def main() -> None:
    st.set_page_config(page_title="Acme invoice agents", page_icon="\U0001f9fe", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    mode = sidebar()
    t1, t2, t3, t4 = st.tabs(["Process invoice", "Dashboard", "Review queue", "Evaluation"])
    with t1:
        tab_process(mode)
    with t2:
        tab_dashboard(mode)
    with t3:
        tab_review()
    with t4:
        tab_evaluation(mode)


if __name__ == "__main__":
    main()
