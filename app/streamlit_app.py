"""Streamlit front end. All processing goes through the same functions as the CLI
(make_client, process_invoice, review_hold); this file only renders results.
Colours come from the theme in .streamlit/config.toml (light, dark or system). Our own HTML uses
inherited text colour and translucent tints, so it reads correctly in every theme."""

from __future__ import annotations

import html
import json
import os
import tempfile
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from invoice_agents.batch import collect_files, run_new, split_loaded
from invoice_agents.db import Database
from invoice_agents.export import (
    compare_expected,
    expected_template,
    result_rows,
    summary_line,
    to_csv,
    to_json,
)
from invoice_agents.graph import Context, process_invoice
from invoice_agents.llm.client import LLMUnavailable, make_client
from invoice_agents.presentation import (
    SORT_OPTIONS,
    call_provenance,
    decision_badge,
    display_number,
    extracted_lines,
    filter_rows,
    format_local,
    ledger_details,
    ledger_reason,
    money,
    plain,
    sort_rows,
    timeline,
    usd_bounds,
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
UPLOAD_TYPES = ["txt", "pdf", "csv", "json", "xml"]
RULES_ONLY_NOTE = (
    "Processed with rules only (no recorded AI response for this file and no API key configured)."
)

# Tints are translucent so they sit correctly on a light or a dark page; text is inherited.
CSS = """
<style>
.card { background:rgba(128,128,128,.07); border:1px solid rgba(128,128,128,.30);
        border-radius:12px; padding:.9rem 1.2rem; margin-bottom:10px; }
.card.ok { border-color:rgba(46,158,79,.60); } .card.hold { border-color:rgba(217,138,0,.65); }
.card.no { border-color:rgba(214,69,69,.60); } .card.dup { border-color:rgba(128,128,128,.55); }
.pill { font-size:13px; padding:3px 10px; border-radius:8px; white-space:nowrap; font-weight:500;
        background:rgba(128,128,128,.16); border:1px solid rgba(128,128,128,.40); }
.pill.ok { background:rgba(46,158,79,.18); border-color:rgba(46,158,79,.55); }
.pill.hold { background:rgba(217,138,0,.18); border-color:rgba(217,138,0,.60); }
.pill.no { background:rgba(214,69,69,.18); border-color:rgba(214,69,69,.55); }
.row { display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap; }
.row .right { display:flex; align-items:center; gap:12px; }
.big { font-size:24px; font-weight:500; } .who { margin:10px 0 2px; font-size:15px; font-weight:500; }
.why { margin:0; font-size:14px; opacity:.74; }
.step { display:flex; gap:10px; font-size:14px; padding:5px 0; }
.step .i { width:20px; text-align:center; font-weight:600; }
.step .check { color:#2e9e4f; } .step .warn { color:#d98a00; }
.step .fail { color:#d64545; } .step .dash { opacity:.55; }
.m { background:rgba(128,128,128,.10); border-radius:8px; padding:10px 12px; }
.m .k { font-size:12px; opacity:.72; margin:0; }
.m .v { font-size:19px; font-weight:500; margin:0; white-space:nowrap; }
.lbl { font-size:12px; opacity:.72; margin:1rem 0 6px; }
.tbl { width:100%; border-collapse:collapse; font-size:14px; table-layout:auto; }
.tbl th { text-align:left; font-weight:500; font-size:13px; padding:8px 10px;
          border-bottom:1px solid rgba(128,128,128,.35); background:rgba(128,128,128,.10); }
.tbl td { padding:8px 10px; border-bottom:1px solid rgba(128,128,128,.18); vertical-align:top;
          overflow-wrap:anywhere; }
.tbl td.nw { white-space:nowrap; overflow-wrap:normal; }
.mono { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size:12px; white-space:pre-wrap; }
.tight p { margin-bottom:.25rem; }
</style>
"""

TONE = {"APPROVE": "ok", "HOLD": "hold", "REJECT": "no", "DUPLICATE": "dup"}
ICON = {"check": "✓", "warn": "⚠", "fail": "✗", "dash": "—"}
ICON_WORD = {"check": "done", "warn": "needs attention", "fail": "failed", "dash": "not applicable"}

ENGINES = [
    (
        "auto",
        "Auto (recommended)",
        "Uses recorded Grok responses when available, otherwise live Grok if an API key is "
        "configured, otherwise rules only.",
    ),
    ("cache", "Recorded responses only", "Replays saved Grok outputs; no key needed."),
    ("live", "Live Grok", "Calls the xAI API; needs a key."),
    ("none", "Rules only", "No AI; shows the code-only baseline."),
]


def esc(x) -> str:
    # "$" becomes an entity so st.markdown does not pair two dollar amounts into a LaTeX span.
    return html.escape(str(x)).replace("$", "&#36;")


def md(text: str) -> str:
    """Escape dollar signs in plain markdown text (captions, toasts, dialogs)."""
    return text.replace("$", "\\$")


def sample_files() -> list[Path]:
    return collect_files(SAMPLE_DIRS)


def pill(decision: str, big: bool = False) -> str:
    style = ' style="font-size:15px;padding:4px 12px"' if big else ""
    return f'<span class="pill {TONE[decision]}"{style}>{esc(decision_badge(decision))}</span>'


def html_table(rows: list[dict], nowrap: tuple[str, ...] = ()) -> str:
    """Plain HTML table so long reasons wrap instead of being cut off or scrolling sideways."""
    if not rows:
        return ""
    head = "".join(f"<th>{esc(k)}</th>" for k in rows[0])
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="nw">{esc(v)}</td>' if k in nowrap else f"<td>{esc(v)}</td>"
            for k, v in r.items()
        )
        + "</tr>"
        for r in rows
    )
    return f'<table class="tbl"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def metric(label: str, value: str) -> str:
    return f'<div class="m"><p class="k">{esc(label)}</p><p class="v">{esc(value)}</p></div>'


# ---------------------------------------------------------------------------- settings


def has_api_key() -> bool:
    load_dotenv()
    return bool(os.environ.get("LLM_API_KEY", "").strip())


def reset_database() -> None:
    db = Database(DB_PATH)
    db.reset()
    db.close()
    for k in ("last_result", "confirm_reset"):
        st.session_state.pop(k, None)
    st.session_state["upload_gen"] = st.session_state.get("upload_gen", 0) + 1
    flash("Database reset.")


def settings_popover() -> str:
    """The AI engine choice and Reset database. Returns the engine mode for this run."""
    key_ok = has_api_key()
    available = [e for e in ENGINES if e[0] != "live" or key_ok]
    with st.popover("⚙ Settings", use_container_width=True):
        st.markdown("**AI engine**")
        mode = st.radio(
            "AI engine",
            [e[0] for e in available],
            format_func=dict((e[0], e[1]) for e in available).get,
            captions=[e[2] for e in available],
            key="engine",
            label_visibility="collapsed",
        )
        if not key_ok:
            st.caption("Live Grok is unavailable: set LLM_API_KEY in .env (see .env.example).")
        st.divider()
        if not st.session_state.get("confirm_reset"):
            if st.button("Reset database", use_container_width=True):
                st.session_state["confirm_reset"] = True
                st.rerun()
        else:
            st.warning("This wipes the ledger, payments and reviews. Inventory is reseeded.")
            c1, c2 = st.columns(2)
            if c1.button("Yes, reset", type="primary", use_container_width=True):
                reset_database()
            if c2.button("Cancel", use_container_width=True):
                st.session_state["confirm_reset"] = False
                st.rerun()
    return mode or "auto"


def client_for(mode: str):
    try:
        return make_client(mode, CACHE_DIR)
    except LLMUnavailable as exc:
        st.error(f"The AI engine is not available: {exc}")
        return None


def process_files(
    paths: list[Path], mode: str, only_new: bool = False
) -> tuple[list[dict], int] | None:
    """Process files in the given order into the ledger. Returns (final states, number skipped
    because the exact file was already loaded), or None if the chosen engine is unavailable.
    ``only_new`` is for the demo set; uploads always count as real submissions."""
    client = client_for(mode)
    if mode != "none" and client is None:
        return None
    db = Database(DB_PATH)
    try:
        if only_new:
            results, skipped = run_new(paths, db, client, mode)
            return [state for state, _ in results], skipped
        states = []
        for path in paths:
            ctx = Context(db=db, client=client, llm_mode=mode)
            states.append(process_invoice(str(path), ctx))
        return states, 0
    finally:
        db.close()


def used_rules_only(state) -> bool:
    return any(c.get("outcome") == "rules_only" for c in state.get("llm_calls", []))


# ---------------------------------------------------------------------------- process


def decision_card(rec: dict) -> str:
    d = rec["decision"]
    cur = rec["currency"] or "USD"
    number = display_number(rec["invoice_number"]) if rec["invoice_number"] else None
    who = " · ".join(x for x in (rec["vendor"], number) if x) or "Unknown vendor"
    hint = (
        '<p class="why">Use Reset database in Settings to start over.</p>'
        if d == "DUPLICATE"
        else ""
    )
    when = (
        f'<p class="why">Processed {esc(format_local(rec["processed_at"]))}</p>'
        if rec.get("processed_at")
        else ""
    )
    return (
        f'<div class="card {TONE[d]}"><div class="row">{pill(d, True)}'
        f'<span class="big">{esc(money(rec["total"], cur))}</span></div>'
        f'<p class="who">{esc(who)}</p><p class="why">{esc(rec["reason"])}</p>{hint}{when}</div>'
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
    if used_rules_only(state):
        st.info(RULES_ONLY_NOTE)
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
        inv = state.get("invoice")
        raw_number = inv.invoice_number if inv and inv.invoice_number else "not found"
        st.write(
            f"File: {state['path']}  \nInvoice number as written: {raw_number}  \n"
            f"Fingerprint: {state['doc'].fingerprint}"
        )
        calls = state.get("llm_calls", [])
        st.write(
            "AI steps: "
            + (
                ", ".join(f"{c['agent']} ({call_provenance(c)})" for c in calls)
                or call_provenance({"provenance": "code"})
            )
        )
        for f in state.get("findings", []):
            earlier = f.data.get("original_file")
            seen = f" Earlier file: {earlier}." if earlier else ""
            st.write(md(f"- {plain(f)}  `{f.code}` ({f.severity}, {f.kind}).{seen}"))
        for step in state["steps"]:
            st.text(step["step"] + "\n  " + "\n  ".join(step["lines"]))
        res = state.get("resolve")
        if res and res.tool_trace:
            st.json(res.tool_trace)


def tab_process(mode: str) -> None:
    from invoice_agents.batch import result_record

    source = st.radio("Invoice source", ["Pick a sample", "Upload a file"], horizontal=True)
    path = None
    if source == "Pick a sample":
        files = sample_files()
        path = str(st.selectbox("Sample invoice", files, format_func=lambda p: p.name))
    else:
        up = st.file_uploader("Invoice file", type=UPLOAD_TYPES)
        if up is not None:
            tmp = Path(tempfile.mkdtemp(prefix="invoice_upload_")) / Path(up.name).name
            tmp.write_bytes(up.getvalue())  # temp dir only, never data/
            path = str(tmp)
    if st.button("Run", disabled=path is None, type="primary"):
        done = process_files([Path(path)], mode)
        if done:
            state = done[0][0]
            rec = result_record(state, "ui")
            db = Database(DB_PATH)
            try:
                row = db.get_ledger(state["ledger_id"])
            finally:
                db.close()
            rec["processed_at"] = row["processed_at"] if row else None
            st.session_state["last_result"] = (state, rec)
    last = st.session_state.get("last_result")
    if last:
        render_result(*last)


# ---------------------------------------------------------------------------- dashboard

SEGMENTS = [
    ("all", "All"),
    ("APPROVE", "Approved"),
    ("HOLD", "Needs review"),
    ("REJECT", "Rejected"),
    ("DUPLICATE", "Duplicate"),
]


def flash(message: str) -> None:
    st.session_state["toast"] = message
    st.rerun()


def add_invoices(rows: list[dict], mode: str) -> None:
    db = Database(DB_PATH)
    try:
        pending, loaded = split_loaded(sample_files(), db)
    finally:
        db.close()
    with st.expander("Add invoices", expanded=not rows):
        if not rows:
            st.write(
                "The ledger is empty. Upload your own invoices to process them, or load the "
                "sample set to see how the dashboard works."
            )
        gen = st.session_state.get("upload_gen", 0)
        ups = st.file_uploader(
            "Upload invoices",
            type=UPLOAD_TYPES,
            accept_multiple_files=True,
            key=f"dash_upload_{gen}",
        )
        st.caption("Files already processed will be marked as duplicates.")
        c1, c2 = st.columns(2)
        label = f"Process {len(ups)} uploaded invoice{'s' if len(ups) != 1 else ''}"
        if c1.button(label if ups else "Process uploaded invoices", type="primary",
                     disabled=not ups, use_container_width=True):  # fmt: skip
            folder = Path(tempfile.mkdtemp(prefix="invoice_upload_"))
            for up in ups:
                (folder / Path(up.name).name).write_bytes(up.getvalue())  # temp dir only
            paths = sorted(folder.iterdir(), key=lambda p: p.name)
            done = process_files(paths, mode)
            if done is not None:
                states = done[0]
                st.session_state["upload_gen"] = gen + 1
                rules = sum(used_rules_only(x) for x in states)
                extra = (
                    f" {rules} used rules only (no recorded AI response, no API key)."
                    if rules
                    else ""
                )
                flash(f"Processed {len(states)} invoice{'s' if len(states) != 1 else ''}.{extra}")
        all_loaded = not pending
        if c2.button("Load sample invoices", disabled=all_loaded, use_container_width=True):
            done = process_files(sample_files(), mode, only_new=True)
            if done is not None:
                states, skipped = done
                flash(
                    f"Loaded {len(states)} new, skipped {skipped} already loaded."
                    if skipped
                    else f"Loaded {len(states)} sample invoices."
                )
        if all_loaded:
            c2.caption(
                f"All {len(loaded)} sample invoices are already loaded. "
                "Use Reset database in Settings to reload."
            )
        elif loaded:
            c2.caption(f"{len(loaded)} of {len(loaded) + len(pending)} samples are already loaded.")


def tab_dashboard(mode: str) -> None:
    db = Database(DB_PATH)
    try:
        rows, payments = db.ledger(), db.payments()
    finally:
        db.close()
    add_invoices(rows, mode)
    if not rows:
        return

    def count(decision: str) -> int:
        return sum(r["decision"] == decision for r in rows)

    paid = sum(p["amount"] for p in payments if p["status"] == "sent" and p["currency"] == "USD")
    held = sum(
        r["total"] or 0
        for r in rows
        if r["decision"] == "HOLD" and not r["review_status"] and (r["currency"] or "USD") == "USD"
    )
    cards = [
        ("Processed", str(len(rows))),
        ("Approved", str(count("APPROVE"))),
        ("Needs review", str(count("HOLD"))),
        ("Rejected", str(count("REJECT"))),
        ("$ paid", money(paid)),
        ("$ held", money(held)),
    ]
    for col, (k, v) in zip(st.columns(len(cards)), cards, strict=True):
        col.markdown(metric(k, v), unsafe_allow_html=True)

    counts = {"all": len(rows)} | {k: count(k) for k, _ in SEGMENTS[1:]}
    names = dict(SEGMENTS)
    chosen = st.segmented_control(
        "Show",
        [k for k, _ in SEGMENTS],
        format_func=lambda k: f"{names[k]} ({counts[k]})",
        default="all",
        selection_mode="single",
        key="dash_filter",
    )
    bounds = usd_bounds(rows)
    f1, f2, f3 = st.columns([3, 3, 2], vertical_alignment="bottom")
    needle = f1.text_input("Search", placeholder="Search vendor, invoice or reason").strip()
    amount = bounds
    if bounds and bounds[0] < bounds[1]:
        with f2:
            amount = st.slider(
                "Amount (USD)",
                bounds[0],
                bounds[1],
                bounds,
                step=1,
                format="%d",
                key="dash_amount",
            )
    how = f3.selectbox("Sort by", SORT_OPTIONS, key="dash_sort")
    shown = sort_rows(filter_rows(rows, chosen, needle, amount, bounds), how)
    st.caption(f"Showing {len(shown)} of {len(rows)} invoices")
    table = [
        {
            "Invoice": display_number(r["invoice_number"]),
            "Vendor": r["vendor"] or "—",
            "Decision": decision_badge(r["decision"]),
            "Total": money(r["total"], r["currency"] or "USD"),
            "Reason": ledger_reason(r),
            "Processed": format_local(r["processed_at"]),
        }
        for r in shown
    ]
    if table:
        st.markdown(
            html_table(table, nowrap=("Invoice", "Decision", "Total", "Processed")),
            unsafe_allow_html=True,
        )
    else:
        st.caption("No invoices match these filters.")
    export_rows = result_rows(rows, payments)
    d1, d2, _ = st.columns([1.3, 1.3, 4])
    d1.download_button(
        "Download results (CSV)",
        to_csv(export_rows),
        file_name="invoice_results.csv",
        mime="text/csv",
        use_container_width=True,
    )
    d2.download_button(
        "Download results (JSON)",
        to_json(export_rows),
        file_name="invoice_results.json",
        mime="application/json",
        use_container_width=True,
    )
    check_expected(export_rows)


def check_expected(export_rows: list[dict]) -> None:
    """Compare the processed invoices with a CSV of expected decisions (file,expected_decision)."""
    with st.expander("Check against expected"):
        st.caption(
            "Upload a CSV with the columns file and expected_decision. Files are matched by "
            "name; case and spacing in the decision are ignored."
        )
        st.download_button(
            "Download expected-results template",
            expected_template([r["file"] for r in export_rows]),
            file_name="expected_results_template.csv",
            mime="text/csv",
        )
        up = st.file_uploader("Expected results (CSV)", type=["csv"], key="expected_csv")
        if up is None:
            return
        try:
            cmp = compare_expected(export_rows, up.getvalue().decode("utf-8", errors="replace"))
        except ValueError as exc:
            st.error(str(exc))
            return
        st.markdown(f"**{summary_line(cmp)}**")
        if cmp["rows"]:
            table = [
                {
                    "File": r["file"],
                    "Expected": r["expected"] or "—",
                    "Actual": r["actual"],
                    "Match": "✓" if r["match"] else "✗",
                }
                for r in cmp["rows"]
            ]
            st.markdown(html_table(table, nowrap=("Match",)), unsafe_allow_html=True)
        if cmp["not_processed"]:
            st.warning("Expected but not processed: " + ", ".join(cmp["not_processed"]))


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


OUTCOME_TEXT = {
    "approved_paid": "Approved and paid.",
    "approved_not_paid": "Approved; it had already been paid.",
    "rejected": "Rejected.",
}


def do_review(ledger_id: int, action: str, note: str) -> None:
    db = Database(DB_PATH)
    try:
        res = review_hold(db, ledger_id, action, note)
    except ReviewError as exc:
        st.error(str(exc))
        return
    finally:
        db.close()
    flash(OUTCOME_TEXT.get(res.outcome, res.detail or res.outcome))


@st.dialog("Confirm payment")
def confirm_pay(ledger_id: int, vendor: str, amount: str, note: str) -> None:
    st.write(md(f"Pay {amount} to {vendor}?"))
    st.caption("The payment goes through the payment tool once. It cannot be undone from here.")
    c1, c2 = st.columns(2)
    if c1.button("Confirm", type="primary", use_container_width=True):
        do_review(ledger_id, "approve", note)
    if c2.button("Cancel", use_container_width=True):
        st.rerun()


def review_card(r: dict) -> None:
    rid = r["id"]
    reasons = reasons_for(r)
    elig = approval_eligibility(r)
    cur = r["currency"] or "USD"
    vendor = r["vendor"] or "Unknown vendor"
    amount = money(r["total"], cur)
    codes = {f["code"] for f in reasons}
    with st.container(border=True):
        st.markdown(
            f'<div class="row"><b>{esc(vendor)} · {esc(display_number(r["invoice_number"]))}</b>'
            f'<span class="right"><span class="big">{esc(amount)}</span>{pill("HOLD")}</span></div>'
            f'<p class="who" style="font-weight:400">{esc("; ".join(plain(f) for f in reasons))}</p>'
            f'<p class="why"><b>Suggested:</b> {esc(suggestion(codes))}</p>'
            f'<p class="why" style="margin-bottom:.6rem">Received {esc(format_local(r["processed_at"]))}</p>',
            unsafe_allow_html=True,
        )
        with st.expander("Details"):
            lines = ledger_details(r).get("lines", [])
            if lines:
                st.markdown(
                    html_table(
                        [
                            {
                                "Item as written": li["raw_name"],
                                "SKU": li["sku"] or "—",
                                "Qty": f"{li['quantity']:g}",
                                "Unit price": money(li["unit_price"], cur),
                                "Line total": money(li["line_total"], cur),
                            }
                            for li in lines
                        ],
                        nowrap=("SKU", "Qty", "Unit price", "Line total"),
                    ),
                    unsafe_allow_html=True,
                )
            else:
                st.caption("No line items stored for this invoice.")
            st.markdown("**Findings**")
            for f in json.loads(r.get("findings_json") or "[]"):
                if f["severity"] != "note":
                    st.markdown(md(f"- {plain(f)} `{f['code']}`"))
        note = st.text_input(
            "Reviewer note (required)",
            key=f"note_{rid}",
            placeholder="Why are you approving or rejecting this invoice?",
        )
        c1, c2, _ = st.columns([1.2, 1, 3])
        approve = c1.button(
            "Approve and pay", key=f"approve_{rid}", type="primary", disabled=not elig.ok,
            use_container_width=True,
        )  # fmt: skip
        reject = c2.button("Reject", key=f"reject_{rid}", use_container_width=True)
        if not elig.ok:
            st.caption(md("Approve is unavailable: " + " ".join(elig.reasons)))
        if (approve or reject) and not note.strip():
            st.caption(":red[Add a reviewer note first.]")
        elif approve:
            confirm_pay(rid, vendor, amount, note)
        elif reject:
            do_review(rid, "reject", note)


def recently_reviewed(db: Database) -> None:
    ledger = {r["id"]: r for r in db.ledger()}
    done = [
        rv for rv in db.reviews() if rv["outcome"] in OUTCOME_TEXT and rv["ledger_id"] in ledger
    ]
    if not done:
        return
    with st.expander(f"Recently reviewed ({len(done)})"):
        for rv in reversed(done):
            r = ledger[rv["ledger_id"]]
            who = f"{r['vendor'] or 'Unknown vendor'} · {display_number(r['invoice_number'])}"
            when = "Reviewed " + format_local(rv["ts"])
            st.markdown(
                md(
                    f"**{who}** · {money(r['total'], r['currency'] or 'USD')} · "
                    f"{OUTCOME_TEXT[rv['outcome']]}  \n"
                    f"Note: {rv['note']} · {when}"
                )
            )


def tab_review() -> None:
    db = Database(DB_PATH)
    try:
        queue = review_queue(db)
        if not queue:
            st.markdown("#### Nothing waiting for review 🎉")
            st.caption(
                "Invoices land here when a person needs to decide: a price change, an unknown "
                "item, not enough stock, or a currency we cannot pay."
            )
        for r in queue:
            review_card(r)
        recently_reviewed(db)
    finally:
        db.close()


def main() -> None:
    st.set_page_config(
        page_title="Acme invoice agents",
        page_icon="\U0001f9fe",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(CSS, unsafe_allow_html=True)
    if msg := st.session_state.pop("toast", None):
        st.toast(md(msg))
    title, gear = st.columns([5, 1], vertical_alignment="center")
    title.markdown("### Acme invoice agents")
    with gear:
        mode = settings_popover()
    t1, t2, t3 = st.tabs(["Process invoice", "Dashboard", "Review queue"])
    with t1:
        tab_process(mode)
    with t2:
        tab_dashboard(mode)
    with t3:
        tab_review()


if __name__ == "__main__":
    main()
