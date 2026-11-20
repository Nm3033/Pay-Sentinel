"""Pay Sentinel console (Streamlit) for the PocketPay campus wallet. UI ONLY: every decision is made in src/.

    streamlit run app.py

Signed out -> full-screen login page only. Signed in (support staff) -> multipage console: start here,
flagged payments, investigation, student app, staff review, add payments, results, scam alerts, audit log.
The session token is verified on every rerun.
"""
from __future__ import annotations

import html
import re
import shutil
import time
from pathlib import Path

import os

import altair as alt
import pandas as pd
import streamlit as st

# Hosted deployments (Streamlit Community Cloud) keep keys in "secrets". Copy them into the environment
# BEFORE src.config is imported, because config reads LLM settings and staff passwords from env vars.
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, (str, int, float, bool)) and _k not in os.environ:
            os.environ[_k] = str(_v)
except Exception:  # noqa: BLE001 - no secrets file locally: .env is used instead
    pass

from src import auth, config, seed_data, services, workflow  # noqa: E402
from src.graph import (OUTCOME_LABEL, campaign_dot, campaign_verdict, case_outcome, detect_scam_campaigns)
from src.report import visible_text
from src.schemas import CaseFile, PaymentState, Route, StaffApproval
from src.services import ApprovalRequired, InvalidTransition
from src.slang import SIGNAL_TEXT
from src.telemetry import campaign_precision, compute_telemetry

BRAND = "Pay Sentinel"
TAGLINE = "Scam defense for the PocketPay campus wallet"
st.set_page_config(page_title=BRAND, page_icon="🛡️", layout="wide", initial_sidebar_state="expanded")

DARK = st.context.theme.type == "dark"
SERIES = ["#3987e5", "#d95926", "#199e70"] if DARK else ["#2a78d6", "#eb6834", "#1baf7a"]
SURFACE = "#0e131a" if DARK else "#f6f7f9"
ROUTE_PILL = {"CLEAR": ("pill-clear", "✓", "Clear"),
              "HOLD_FOR_CONFIRMATION": ("pill-review", "⏸", "Hold for confirmation"),
              "ESCALATE": ("pill-gate", "⛨", "Escalate to staff")}
STATE_TEXT = {"CLEARED": "🟢 Cleared", "PENDING_CONFIRMATION": "🟡 Waiting for student", "ESCALATED": "🔴 With staff",
              "SENT": "✅ Sent (confirmed)", "CANCELLED": "↩️ Cancelled", "EXPIRED": "⌛ Expired, refunded",
              "": "⚪ Not triaged"}
TAGS = {"[CODE]": ("tag-code", "CODE"), "[LLM ]": ("tag-llm", "AI"), "[HUMAN]": ("tag-human", "HUMAN")}

# ---- styling ---------------------------------------------------------------------------------------
st.markdown("""
<style>
.block-container {padding-top: 1.4rem; padding-bottom: 3rem; max-width: 1400px;}
.ps-hero {background: linear-gradient(120deg, #0b2239 0%, #0f4c81 60%, #1b6aa8 100%); color: #f3f7fb;
  border-radius: 14px; padding: 18px 26px; margin-bottom: 18px; box-shadow: 0 6px 24px rgba(15,42,68,.18);}
.ps-hero .ps-kicker {font-size: .74rem; letter-spacing: .12em; text-transform: uppercase; opacity: .75; font-weight: 600;}
.ps-hero h1 {color: #fff; font-size: 1.55rem; font-weight: 700; margin: 2px 0 2px 0; padding: 0; letter-spacing: -.01em;}
.ps-hero p {margin: 0; opacity: .85; font-size: .92rem;}
.ps-chips {margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap;}
.ps-chip {background: rgba(255,255,255,.12); border: 1px solid rgba(255,255,255,.22); color: #f3f7fb;
  border-radius: 999px; padding: 3px 12px; font-size: .76rem; font-weight: 500;}
.pill {display: inline-flex; align-items: center; gap: 6px; border-radius: 999px; padding: 3px 12px;
  font-size: .82rem; font-weight: 600; border: 1px solid transparent; white-space: nowrap;}
.pill-clear {background: rgba(12,163,12,.12); color: #0a7d0a; border-color: rgba(12,163,12,.35);}
.pill-review {background: rgba(250,178,25,.16); color: #8a5a00; border-color: rgba(250,178,25,.5);}
.pill-gate {background: rgba(208,59,59,.12); color: #b42f2f; border-color: rgba(208,59,59,.4);}
.ps-label {font-size: .74rem; text-transform: uppercase; letter-spacing: .06em; opacity: .65; margin-bottom: 2px;}
.ps-value {font-size: 1rem; font-weight: 600;}
.trace {border-left: 2px solid rgba(127,127,127,.25); margin-left: 6px; padding-left: 14px;}
.trace-row {position: relative; padding: 5px 0; font-size: .86rem; line-height: 1.35;}
.trace-row::before {content: ""; position: absolute; left: -20px; top: 11px; width: 10px; height: 10px;
  border-radius: 50%; background: currentColor; opacity: .35;}
.tag {display: inline-block; min-width: 54px; text-align: center; font-size: .68rem; font-weight: 700;
  letter-spacing: .05em; border-radius: 6px; padding: 2px 6px; margin-right: 8px;}
.tag-code {background: rgba(42,120,214,.14); color: #2a78d6;}
.tag-llm {background: rgba(98,80,214,.16); color: #6250d6;}
.tag-human {background: rgba(235,104,52,.16); color: #d95926;}
.ps-side-brand {font-weight: 700; font-size: 1.15rem; margin: 0;}
.ps-side-sub {opacity: .7; font-size: .8rem; margin: 0 0 6px 0;}
.ps-user {border-radius: 10px; padding: 10px 12px; background: rgba(255,255,255,.07);
  border: 1px solid rgba(255,255,255,.12); font-size: .85rem; line-height: 1.4;}
.ps-user b {font-size: .95rem;}
.ps-help {border-left: 3px solid #2f7fd1; background: rgba(47,127,209,.08); border-radius: 8px;
  padding: 10px 14px; font-size: .9rem; line-height: 1.45; margin: 0 0 16px 0;}
.ps-step {border-radius: 12px; padding: 14px 14px 12px 14px; border: 1px solid rgba(127,127,127,.25); height: 100%;}
.ps-step .n {font-weight: 700; font-size: .75rem; opacity: .55; letter-spacing: .08em;}
.ps-step .t {font-weight: 700; font-size: 1rem; margin: 4px 0 6px 0;}
.ps-step .d {font-size: .84rem; opacity: .85; line-height: 1.4;}
.ps-phone {max-width: 380px; margin: 0 auto; border-radius: 28px; padding: 18px 18px 10px 18px;
  border: 8px solid #1b2330; background: linear-gradient(180deg, rgba(47,127,209,.10), rgba(47,127,209,.02));}
.ps-phone .app {font-weight: 700; font-size: .95rem; margin-bottom: 6px;}
.ps-phone .amt {font-size: 1.6rem; font-weight: 700; margin: 4px 0;}
.ps-phone .msg {font-size: .9rem; line-height: 1.45; margin: 8px 0 4px 0;}
.ps-warn {border-radius: 10px; padding: 8px 10px; background: rgba(208,59,59,.12); font-size: .82rem; margin: 6px 0;}
</style>
""", unsafe_allow_html=True)
if DARK:  # follow Streamlit's chosen theme, not the OS setting
    st.markdown("<style>.pill-clear{color:#5fd35f}.pill-review{color:#fcc75a}.pill-gate{color:#f08a8a}"
                ".tag-llm{color:#9085e9}.tag-code{color:#5b9be0}.tag-human{color:#ec835a}"
                ".ps-phone{border-color:#2a3442}</style>", unsafe_allow_html=True)

ss = st.session_state
ss.setdefault("ver", 0)
ss.setdefault("token", None)
ss.setdefault("selected_alert", None)


# ---- helpers -----------------------------------------------------------------------------------------
def refresh() -> None:
    ss.ver += 1


def data_version() -> tuple:
    """Cache key shared by ALL sessions: changes whenever the database or saved case files change,
    so no viewer (or a reloaded tab) ever sees stale data from another session's cache."""
    def mtime(p) -> int:
        try:
            return Path(p).stat().st_mtime_ns
        except OSError:
            return 0
    files = list(workflow.CASES_DIR.glob("*.json")) if workflow.CASES_DIR.exists() else []
    return (mtime(config.DB_PATH), len(files), max((mtime(f) for f in files), default=0))


def pill(route: str) -> str:
    cls, icon, label = ROUTE_PILL[route]
    return f'<span class="pill {cls}">{icon} {label}</span>'


def fmt_reason(r: str) -> str:
    code = re.match(r"[A-Z_]+", r)
    return f"- `{code.group()}` {r[code.end():].lstrip(': ')}" if code else f"- {r}"


def field(label: str, value: str) -> str:
    return f'<div class="ps-label">{html.escape(label)}</div><div class="ps-value">{html.escape(value)}</div>'


def trace_html(lines: list[str]) -> str:
    rows = []
    for line in lines:
        cls, name = next(((c, n) for t, (c, n) in TAGS.items() if line.startswith(t)), ("tag-code", "·"))
        text = line.split("]", 1)[1].strip() if line.startswith("[") else line
        rows.append(f'<div class="trace-row"><span class="tag {cls}">{name}</span>{html.escape(text)}</div>')
    return f'<div class="trace">{"".join(rows)}</div>'


def how_to(text: str) -> None:
    """Short 'how to use this page' callout. `text` is trusted static HTML written in this file."""
    st.markdown(f'<div class="ps-help">💡 {text}</div>', unsafe_allow_html=True)


def hero(title: str, subtitle: str, chips: list[str] = ()) -> None:
    chip_html = "".join(f'<span class="ps-chip">{html.escape(c)}</span>' for c in chips)
    st.markdown(f'<div class="ps-hero"><div class="ps-kicker">🛡️ {BRAND}</div><h1>{html.escape(title)}</h1>'
                f'<p>{html.escape(subtitle)}</p>{f"<div class=ps-chips>{chip_html}</div>" if chips else ""}</div>',
                unsafe_allow_html=True)


# ---- authentication ---------------------------------------------------------------------------------
# ---- first start on a fresh server (e.g. Streamlit Community Cloud): accounts from secrets, demo data ----
auth.ensure_store_from_env()
if not config.DB_PATH.exists():
    with st.spinner("First start: generating the demo dataset…"):
        seed_data.build_database(config.DB_PATH)

officer: str | None = None
if ss.token:
    try:
        officer = auth.verify_token(ss.token)
    except auth.AuthError as e:
        ss.token = None
        st.toast(f"🔒 {e}")


def login_page() -> None:
    st.markdown("""<style>
      [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"], [data-testid="collapsedControl"],
      header[data-testid="stHeader"] {display: none !important;}
      .stApp {background: radial-gradient(1200px 600px at 15% -10%, rgba(27,106,168,.35), transparent 60%),
                          radial-gradient(900px 500px at 110% 110%, rgba(15,76,129,.30), transparent 60%);}
      .block-container {padding-top: 7vh; max-width: 1100px;}
      [data-testid="stForm"] {box-shadow: 0 10px 40px rgba(11,34,57,.18); padding: 26px 28px 18px 28px;}
      .ps-login-logo {font-size: 2.6rem; line-height: 1; margin-bottom: 6px;}
      .ps-login-title {font-size: 2.3rem !important; font-weight: 700; letter-spacing: -.02em; line-height: 1.15;
                       margin: 4px 0 0 0;}
      .ps-login-sub {opacity: .75; margin: 4px 0 22px 0; font-size: 1rem;}
      .ps-feature {font-size: .86rem; opacity: .85; padding: 4px 0;}
    </style>""", unsafe_allow_html=True)
    left, mid, right = st.columns([0.9, 1.35, 0.9])
    with mid:
        st.markdown(f'<div style="text-align:center"><div class="ps-login-logo">🛡️</div>'
                    f'<div class="ps-login-title">{BRAND}</div><div class="ps-login-sub">{TAGLINE}</div></div>',
                    unsafe_allow_html=True)
        if not auth.store_exists():
            st.warning("No support-staff accounts have been set up yet. In a terminal, from the project folder, "
                       "run:\n\n`python -m src.auth init`\n\nthen reload this page.", icon="🔧")
            return
        with st.form("login", clear_on_submit=False):
            st.markdown("##### Support staff sign in")
            oid = st.text_input("Staff ID", placeholder="e.g. SUP-01", key="login_id")
            pw = st.text_input("Password", type="password", key="login_pw")
            submitted = st.form_submit_button("Sign in", type="primary", width="stretch")
            if submitted:
                try:
                    ss.token = auth.login(oid.strip().upper(), pw)
                    ss.pop("login_pw", None)
                    st.rerun()
                except auth.AuthError as e:
                    st.error(str(e), icon="🔒")
        st.caption(f"Campus support team only. {config.OFFICER_MAX_FAILED} failed attempts lock an account for "
                   f"{config.OFFICER_LOCKOUT_MINUTES} minutes; sessions expire after "
                   f"{config.OFFICER_SESSION_MINUTES} minutes.")
    st.write("")
    f = st.columns(3)
    for col, (icon, head, body) in zip(f, [
            ("🥪", "Snacks never wait", "Verified canteen and shop payments clear instantly, with no human needed."),
            ("🎫", "Scams get paused", "Shady ticket deals and phishing links pause for the student or staff."),
            ("🔓", "No wallet is ever frozen", "Code only allows clear, hold or escalate; a human always decides.")]):
        with col.container(border=True):
            st.markdown(f"**{icon} {head}**")
            st.markdown(f'<div class="ps-feature">{body}</div>', unsafe_allow_html=True)


if not officer:
    st.navigation([st.Page(login_page, title="Sign in", icon="🔐", url_path="login")], position="hidden").run()
    st.stop()


# =========================== everything below requires a valid staff session ===========================
@st.cache_data(show_spinner=False)
def load_cases(version: tuple) -> dict[str, CaseFile]:
    return {c.alert.record_id: c for c in workflow.list_saved_cases()}


@st.cache_data(show_spinner="Loading flagged payments…")
def load_queue(version: tuple) -> pd.DataFrame:
    conn = services.connect()
    try:
        rows = []
        for a in services.list_alerts(conn=conn):
            t = services.get_transaction(a.txn_id, conn=conn)
            rows.append(dict(alert=a.record_id, txn=t.record_id, time=t.ts, type=t.txn_type, amount_aed=t.amount_aed,
                             legacy_rule=a.legacy_rule, alert_status=a.status, student=t.student_id,
                             recipient=t.recipient_handle))
        return pd.DataFrame(rows)
    finally:
        conn.close()


@st.cache_data(show_spinner="Looking for scam campaigns across all payments…")
def load_campaigns(version: tuple, include_established: bool):
    return detect_scam_campaigns(include_established=include_established)


try:
    queue = load_queue(data_version())
except services.ServiceError as e:
    st.error(f"{e}")
    st.stop()
cases = load_cases(data_version())
DEMO_ROWS = services.list_demo_scenarios()
_txn_alert = dict(zip(queue["txn"], queue["alert"]))
DEMO = [_txn_alert[d["txn_id"]] for d in DEMO_ROWS if d["txn_id"] in _txn_alert]
DEMO_TITLE = {_txn_alert[d["txn_id"]]: f"#{d['ordinal']} {d['title']}" for d in DEMO_ROWS if d["txn_id"] in _txn_alert}
_alert_status = dict(zip(queue["alert"], queue["alert_status"]))
STAFF_Q = sorted((c for c in cases.values() if c.state == PaymentState.ESCALATED
                  and _alert_status.get(c.alert.record_id) == "ESCALATED"),
                 key=lambda c: (not (c.gate and c.gate.uncertain), c.alert.record_id))
STUDENT_Q = [c for c in cases.values() if c.state == PaymentState.PENDING_CONFIRMATION]
pending = len(STAFF_Q)
tel = compute_telemetry(list(cases.values()))
officer_title = next((o["title"] for o in auth.list_officers() if o["officer_id"] == officer), "")


def state_text(alert_id: str) -> str:
    c = cases.get(alert_id)
    if c is None:
        return STATE_TEXT[""]
    if c.state is not None:
        return STATE_TEXT[c.state.value]
    return {"CLEAR": STATE_TEXT["CLEARED"], "HOLD_FOR_CONFIRMATION": STATE_TEXT["PENDING_CONFIRMATION"],
            "ESCALATE": STATE_TEXT["ESCALATED"]}[c.final_route.value]


def run_one(alert_id: str) -> CaseFile:
    with st.status(f"Checking {alert_id}…", expanded=True) as status:
        case = workflow.run_case(alert_id, use_llm=ss.get("use_llm", True),
                                 simulate_outage=ss.get("outage", False),
                                 progress=lambda m: status.markdown(trace_html([m]), unsafe_allow_html=True))
        status.update(label=f"{alert_id} → {ROUTE_PILL[case.final_route.value][2]}", state="complete",
                      expanded=False)
    refresh()
    return case


def triage_alerts(alert_ids: list[str], *, rerun: bool = True) -> None:
    """Check several payments AT THE SAME TIME (ss.workers in parallel) with a live progress bar.
    Payments already finished or decided by a human are skipped by workflow.triage_many()."""
    todo = list(dict.fromkeys(alert_ids))
    if not todo:
        st.toast("Nothing to check: those payments are already done.", icon="ℹ️")
        return
    workers = ss.get("workers", workflow.DEFAULT_WORKERS)
    bar = st.progress(0.0, text=f"Checking {len(todo)} payment(s), {workers} at a time…")
    tally: dict[str, int] = {}

    def on_done(alert_id, case, err, done, total):   # runs in this (UI) thread
        label = "⚠️ error" if err else ROUTE_PILL[case.final_route.value][2]
        tally[label] = tally.get(label, 0) + 1
        bar.progress(done / total, text=f"{done}/{total} done · last: {alert_id} → {label}")

    t0 = time.perf_counter()
    done_cases, errors = workflow.triage_many(todo, workers=workers, use_llm=ss.get("use_llm", True),
                                              simulate_outage=ss.get("outage", False), on_done=on_done)
    ss.last_batch = dict(n=len(done_cases), skipped=len(todo) - len(done_cases) - len(errors), errors=errors,
                         tally=tally, seconds=round(time.perf_counter() - t0, 1), workers=workers)
    refresh()
    if rerun:
        st.rerun()


def run_demo() -> None:
    triage_alerts([a for a in DEMO if a not in cases])


def batch_summary() -> None:
    """Show the result of the last multi-payment check once, wherever the user lands next."""
    b = ss.pop("last_batch", None)
    if not b:
        return
    parts = " · ".join(f"{k}: {v}" for k, v in sorted(b["tally"].items()))
    msg = (f"Checked **{b['n']}** payment(s), {b['workers']} at a time, in {b['seconds']}s. {parts}"
           + (f" · {b['skipped']} already done, skipped" if b["skipped"] else ""))
    (st.warning if b["errors"] else st.success)(msg, icon="✅" if not b["errors"] else "⚠️")
    for a, e in list(b["errors"].items())[:5]:
        st.caption(f"{a}: {e}")


def open_case(alert_id: str) -> None:
    ss.selected_alert = alert_id
    st.switch_page(P_INVESTIGATE)


# ---- pages ----------------------------------------------------------------------------------------------
def page_home() -> None:
    hero("Start here", "What Pay Sentinel does for PocketPay, and how to try it in two minutes.")
    st.markdown("PocketPay's **two-person support team** reviews about **200 flagged payments and 20 suspicious "
                "messages a day**, and AED 15 canteen snacks sit in the same queue as real scams like *“Bro urgent! "
                "Selling my Front Row Fest pass…”*. Pay Sentinel sorts them automatically: **everyday purchases clear "
                "instantly**, **risky transfers pause for the student to confirm**, **likely scams go to staff with a "
                "full decision log**, and **no wallet is ever frozen**.")
    st.markdown("#### How a payment is checked")
    steps = [("1", "Read the chat", "Screens the message for hidden AI instructions, scam signs and slang it can't "
              "read. The AI says how sure it is.", ["tag-code", "tag-llm"]),
             ("2", "Check the facts", "get_transaction_context() and verify_campus_recipient(): who's paying, who's "
              "receiving, trust score, reports.", ["tag-code"]),
             ("3", "Decide", "Fixed rules: CLEAR, HOLD_FOR_CONFIRMATION or ESCALATE. Unsure means escalate, never "
              "guess.", ["tag-code"]),
             ("4", "Explain", "The AI writes a friendly note for the student; code deletes claims without real records.",
              ["tag-llm", "tag-code"]),
             ("5", "A human decides", "The student confirms or cancels, or staff release or cancel. The state is saved, "
              "so nothing loops.", ["tag-human"])]
    names = {"tag-code": "CODE", "tag-llm": "AI", "tag-human": "HUMAN"}
    for col, (n, t, d, tags) in zip(st.columns(5), steps):
        badges = "".join(f'<span class="tag {c}">{names[c]}</span>' for c in tags)
        col.markdown(f'<div class="ps-step"><div class="n">STEP {n}</div><div class="t">{t}</div>'
                     f'<div style="margin-bottom:6px">{badges}</div><div class="d">{d}</div></div>',
                     unsafe_allow_html=True)
    st.caption("CODE = fixed, testable Python rules · AI = language model (reads and writes only) · "
               "HUMAN = the student or support staff. The AI can make a decision more careful, never less.")

    st.markdown("#### Try it")
    tries = [("Run the 12 test scenarios", "Canteen snacks, the Front Row Fest scam, fake PocketPay messages, "
              "3 unclear-slang cases, a hidden attack and a service outage.", "▶ Run 12 scenarios",
              lambda: (run_demo(), st.rerun())),
             ("Watch the ticket scam get caught", "The exact message from the brief, step by step.",
              "🔍 Open the ticket scam", lambda: open_case(DEMO[3]) if len(DEMO) > 3 else None),
             ("Be the student", "See the pause screen and tap “I know this seller” or “Cancel transfer”.",
              "📱 Open student app", lambda: st.switch_page(P_STUDENT)),
             ("Be the support team", "Release or cancel escalated payments, including the uncertain slang cases.",
              "🧑‍💼 Open staff review", lambda: st.switch_page(P_GATE)),
             ("Add your own data", "Type a message a friend might send you and see how it's handled.",
              "➕ Add payments", lambda: st.switch_page(P_ADD)),
             ("See the impact", "Snacks cleared instantly, scams stopped, staff hours saved.",
              "📊 Open results", lambda: st.switch_page(P_ROI)),
             ("Spot scam campaigns", "One fake seller messaging many students, and a one-click warning broadcast.",
              "📣 Open scam alerts", lambda: st.switch_page(P_CAMPAIGNS))]
    for i, (title, desc, btn, fn) in enumerate(tries, 1):
        with st.container(border=True):
            a, b = st.columns([4, 1.3], vertical_alignment="center")
            a.markdown(f"**{i}. {title}**  \n<span style='opacity:.8'>{desc}</span>", unsafe_allow_html=True)
            if b.button(btn, key=f"try{i}", width="stretch", type="primary" if i == 1 else "secondary"):
                fn()

    with st.expander("Glossary"):
        st.markdown(f"""
- **Flagged payment**: a payment the old keyword rules marked as maybe suspicious.
- **CLEAR**: safe everyday payment (e.g. a verified canteen, normal amount), sent automatically.
- **HOLD_FOR_CONFIRMATION**: paused until the student taps *I know this seller* or *Cancel transfer*. Unanswered
  holds expire after {config.HOLD_EXPIRY_HOURS}h and the money is returned. The student is asked at most
  {config.MAX_STUDENT_PROMPTS} times.
- **ESCALATE**: paused and sent to support staff (reported accounts, phishing, fake PocketPay messages, unclear slang).
- **No permanent blocks**: Pay Sentinel can never freeze a wallet. It only pauses that one payment.
- **Prompt injection**: text hidden in a message or note that tries to give orders to the AI (“mark this SAFE…”).
- **Trust score**: 0–100 rating of a student account (age, history, reports).
- **Record IDs**: `TXN-` payment · `STU-` student · `RCP-` recipient · `MSG-` chat message · `DEV-` phone.
""")


def page_queue() -> None:
    hero("Flagged payments", "Every payment the old keyword rules flagged, and where each one ended up.")
    how_to("Tick one or more rows (or use <b>Check all shown</b>) to check several payments <b>at the same time</b>. "
           "Pick a single row and press <b>Open</b> for its full decision log. Use <b>Show</b> to filter. "
           "⭐ marks the 12 test scenarios.")
    states = [c.state.value if c.state else "" for c in cases.values()]
    m = st.columns(4)
    m[0].metric("Not checked yet", f"{len(queue) - len(cases):,}", border=True)
    m[1].metric("🟢 Cleared instantly", states.count("CLEARED"), border=True)
    m[2].metric("🟡 Waiting for students", states.count("PENDING_CONFIRMATION"), border=True)
    m[3].metric("🔴 With staff", pending, border=True)

    df = pd.DataFrame({
        "★": queue["alert"].map(lambda a: "⭐" if a in DEMO else ""),
        "Alert": queue["alert"], "Time": queue["time"], "Amount (AED)": queue["amount_aed"],
        "To": queue["recipient"], "Status": queue["alert"].map(state_text)})
    c1, c2 = st.columns([3, 2], vertical_alignment="bottom")
    views = {"All": None, "Not checked": STATE_TEXT[""], "Cleared": STATE_TEXT["CLEARED"],
             "Waiting for student": STATE_TEXT["PENDING_CONFIRMATION"], "With staff": STATE_TEXT["ESCALATED"],
             "Finished by a human": "human", "⭐ Scenarios": "demo"}
    view = c1.segmented_control("Show", list(views), default="All") or "All"
    q = c2.text_input("Search", placeholder="🔎 Search alert ID or @handle", label_visibility="collapsed")
    if views[view] == "demo":
        df = df[df["★"] == "⭐"]
    elif views[view] == "human":
        df = df[df["Status"].isin([STATE_TEXT[s] for s in ("SENT", "CANCELLED", "EXPIRED")])]
    elif views[view]:
        df = df[df["Status"] == views[view]]
    if q:
        df = df[df["Alert"].str.contains(q, case=False, regex=False) | df["To"].str.contains(q, case=False, regex=False)]
    event = st.dataframe(df, hide_index=True, width="stretch", height=430, on_select="rerun",
                         selection_mode="multi-row", key="queue_table", column_config={
        "★": st.column_config.TextColumn("", width=32),
        "Amount (AED)": st.column_config.NumberColumn(format="localized"),
        "Time": st.column_config.DatetimeColumn(format="MMM D, HH:mm")})
    rows = event.selection.rows if event and event.selection else []
    chosen = [df.iloc[i]["Alert"] for i in rows]
    unchecked_sel = [a for a in chosen if a not in cases]
    unchecked_shown = [a for a in df["Alert"] if a not in cases]
    workers = ss.get("workers", workflow.DEFAULT_WORKERS)
    c1, c2, c3, c4 = st.columns([2.2, 1.2, 1.4, 1.3], vertical_alignment="center")
    c1.caption(f"Showing {len(df):,} of {len(queue):,} · {len(chosen)} selected · checks {workers} at a time")
    if len(chosen) == 1 and c2.button(f"🔍 Open {chosen[0]}", width="stretch"):
        open_case(chosen[0])
    if c3.button(f"▶ Check {len(unchecked_sel)} selected", type="primary", width="stretch",
                 disabled=not unchecked_sel, help="Already-checked payments in your selection are skipped"):
        triage_alerts(unchecked_sel)
    cap = min(len(unchecked_shown), 100)
    if c4.button(f"▶ Check all shown ({cap})", width="stretch", disabled=not unchecked_shown,
                 help="Every not-yet-checked payment that matches the current filter (up to 100)"):
        triage_alerts(unchecked_shown[:100])


def render_case(c: CaseFile) -> None:
    t = c.transaction
    with st.container(border=True):
        h = st.columns([1, 1, 1.3, 1, 1.1])
        h[0].markdown(field("Amount", f"{t.amount_aed:,.2f} AED"), unsafe_allow_html=True)
        h[1].markdown(field("Student", t.student_id), unsafe_allow_html=True)
        h[2].markdown(field("To", t.recipient_handle), unsafe_allow_html=True)
        h[3].markdown(field("Type", f"{t.txn_type} · {t.channel}"), unsafe_allow_html=True)
        h[4].markdown(field("Time", f"{t.ts:%d %b, %H:%M}"), unsafe_allow_html=True)
        tag = " ❓ uncertain" if c.gate and c.gate.uncertain else ""
        st.markdown(f'<div style="margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
                    f'<span class="ps-label" style="margin:0">Decision</span>{pill(c.final_route.value)}'
                    f'<span class="ps-value">{tag}</span>'
                    f'<span class="ps-label" style="margin:0 0 0 10px">Payment state</span>'
                    f'<span class="ps-value">{STATE_TEXT[c.state.value] if c.state else "not saved"}</span></div>',
                    unsafe_allow_html=True)
    if c.investigation:
        st.info(f"**What the student sees:** {c.investigation.student_explanation}", icon="📱")

    left, right = st.columns([1.05, 1], gap="large")
    with left, st.container(border=True):
        st.markdown("##### Decision log")
        st.markdown(trace_html(c.trace), unsafe_allow_html=True)
        for e in c.errors:
            st.warning(e)
    with right, st.container(border=True):
        st.markdown("##### Chat message")
        if c.chat is None and c.memo is None:
            st.caption("No message or note came with this payment.")
        inj = any(s and s.injection_detected for s in (c.chat, c.memo))
        if inj:
            st.error("**Hidden instructions to the AI were found and removed.**", icon="🚨")
        if c.signals and c.signals.signals:
            st.warning("Scam signs: " + ", ".join(SIGNAL_TEXT.get(s, s) for s in c.signals.signals), icon="⚠️")
        if c.signals and c.signals.unclear_terms:
            st.warning("Slang the rules can't read: " + ", ".join(c.signals.unclear_terms), icon="❓")
        if c.assessment:
            a = c.assessment
            st.caption(f"AI reading: **{a.scam_risk}** risk, **{a.confidence:.0%}** sure. {a.summary}")
        if c.chat:
            st.markdown("**Original message** · untrusted")
            st.code(visible_text(c.chat.original), language=None, wrap_lines=True)
            st.markdown("**What the AI was allowed to read**")
            st.code(c.chat.clean_text or "(empty)", language=None, wrap_lines=True)
        if c.memo:
            st.markdown("**Payment note** · untrusted")
            st.code(visible_text(c.memo.original), language=None, wrap_lines=True)

    with st.container(border=True):
        st.markdown("##### Fixed payment rules · deterministic Python")
        box = {Route.CLEAR: st.success, Route.HOLD_FOR_CONFIRMATION: st.warning, Route.ESCALATE: st.error}[c.gate.route]
        box("\n".join(fmt_reason(r) for r in c.gate.reasons))

    with st.expander("Evidence from the tools"):
        if c.context:
            x = c.context
            e = st.columns(4)
            e[0].markdown(field("Student", f"{x.student.full_name} · {x.student.record_id}"), unsafe_allow_html=True)
            e[1].markdown(field("Grade", x.student.grade), unsafe_allow_html=True)
            e[2].markdown(field("PocketPay since", str(x.student.joined_on)), unsafe_allow_html=True)
            e[3].markdown(field("Payments in 24h", str(x.velocity.txn_count)), unsafe_allow_html=True)
            st.markdown("**Devices**")
            st.dataframe(pd.DataFrame([d.model_dump() | {"used_now": d.record_id == t.device_id}
                                       for d in x.devices]), hide_index=True, width="stretch")
        if c.recipient:
            rc = c.recipient
            if rc.recipient:
                r = rc.recipient
                st.markdown(f"**Recipient** `{r.record_id}` · {r.display_name} ({r.handle}) · "
                            f"{'✅ verified campus vendor' if rc.verified_vendor else 'student account'} · "
                            f"{rc.account_age_days} days old · trust {r.trust_score}/100 · {r.scam_reports} report(s)")
            else:
                st.markdown(f"**Recipient** {rc.handle}: not a known PocketPay account")
        else:
            st.markdown("**Recipient**: the check timed out. The payment paused instead of guessing.")
        if c.spread:
            st.markdown(f"**Reach** `{c.spread.record_id}` · {c.spread.distinct_students} student(s) within a day")

    if c.investigation and c.investigation.findings:
        with st.container(border=True):
            st.markdown("##### Findings · AI, citations verified by code")
            st.dataframe(pd.DataFrame([{"Finding": f.claim, "Citations": ", ".join(f.citations)}
                                       for f in c.investigation.findings]), hide_index=True, width="stretch")
            if c.dropped_findings:
                st.warning("Dropped by citation check (cited IDs not in evidence): " +
                           "; ".join(f"“{f.claim}” → {f.citations}" for f in c.dropped_findings), icon="✂️")
            st.markdown(f"**Staff summary.** {c.investigation.staff_summary}")
    if c.report_markdown:
        with st.expander("📄 Full decision log (shareable)"):
            st.download_button("Download decision log (.md)", c.report_markdown,
                               file_name=f"DECISION_LOG_{t.record_id}.md")
            st.markdown(c.report_markdown)


def page_investigation() -> None:
    hero("Investigation", "Watch one flagged payment go through the checks, step by step.")
    how_to("Choose a payment and press <b>Check payment</b>. The <b>decision log</b> (left) shows each step tagged "
           "CODE, AI or HUMAN, at least three automatic steps before any decision. The <b>chat panel</b> (right) "
           "shows the original message next to what the AI was allowed to read.")
    order = DEMO + [a for a in queue["alert"] if a not in DEMO]
    info = queue.set_index("alert")

    def label(a: str) -> str:
        r = info.loc[a]
        return (f"{DEMO_TITLE[a] + ' · ' if a in DEMO else ''}{a} · {r.amount_aed:,.2f} AED to {r.recipient} · "
                f"{state_text(a)}")

    start = order.index(ss.selected_alert) if ss.selected_alert in order else 0
    c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
    sel = c1.selectbox("Payment", order, index=start, format_func=label)
    ss.selected_alert = sel
    existing = cases.get(sel)
    done = existing is not None and existing.state in (PaymentState.CLEARED, PaymentState.SENT, PaymentState.CANCELLED,
                                                       PaymentState.EXPIRED)
    if c2.button("🔍 Check payment", type="primary", width="stretch", disabled=done,
                 help="Finished payments can't be re-run" if done else None):
        existing = run_one(sel)
    if existing:
        render_case(existing)
    else:
        st.info("Not checked yet. Press **Check payment** to watch the agents work.", icon="🔍")


def page_student() -> None:
    hero("📱 Student app", "What a student sees when Pay Sentinel pauses one of their payments.")
    how_to("Pick a student with a paused payment. This is the stretch-goal <b>“I know this seller / Cancel transfer”</b> "
           "flow. The reply goes through <code>update_payment_state()</code>, which checks it really is that student. "
           "Payments with staff can still be cancelled by the student, but only staff can release them.")
    mine = {c.transaction.student_id for c in STUDENT_Q} | {c.transaction.student_id for c in STAFF_Q}
    if not mine:
        st.info("No student has a paused payment right now. Run the 12 test scenarios first.", icon="📭")
        return
    students = {s["student_id"]: s for s in services.list_students()}
    sid = st.selectbox("Student", sorted(mine), format_func=lambda s: f"{s} · {students[s]['full_name']} "
                                                                       f"({students[s]['grade']})")
    warnings = {b["recipient_handle"]: b["text"] for b in services.list_broadcasts()}
    paused = [c for c in STUDENT_Q + STAFF_Q if c.transaction.student_id == sid]
    for c in paused:
        t = c.transaction
        st_rec = services.get_payment_state(t.record_id)
        with st.container():
            warn = (f'<div class="ps-warn">📣 Campus alert: {html.escape(warnings[t.recipient_handle])}</div>'
                    if t.recipient_handle in warnings else "")
            status = ("Waiting for you" if c.state == PaymentState.PENDING_CONFIRMATION
                      else "With the PocketPay support team")
            extra = (f"Asked {st_rec.prompts_sent} of {config.MAX_STUDENT_PROMPTS} times · expires "
                     f"{st_rec.expires_at:%d %b %H:%M}" if st_rec and st_rec.expires_at else "")
            st.markdown(f'<div class="ps-phone"><div class="app">PocketPay · 🛡️ protected by {BRAND}</div>'
                        f'<div class="ps-label">Payment paused · {html.escape(status)}</div>'
                        f'<div class="amt">{t.amount_aed:,.2f} AED</div>'
                        f'<div>to <b>{html.escape(t.recipient_handle)}</b></div>{warn}'
                        f'<div class="msg">{html.escape(c.investigation.student_explanation if c.investigation else "")}'
                        f'</div><div class="ps-label" style="margin-top:8px">{html.escape(extra)}</div></div>',
                        unsafe_allow_html=True)
            cols = st.columns([1, 1, 1]) if c.state == PaymentState.PENDING_CONFIRMATION else st.columns([1, 2])
            try:
                if c.state == PaymentState.PENDING_CONFIRMATION:
                    if cols[0].button("✅ I know this seller", key=f"ok_{t.record_id}", type="primary", width="stretch"):
                        workflow.student_respond(c.case_id, "STUDENT_CONFIRM", sid)
                        refresh(); st.toast("Payment sent.", icon="✅"); st.rerun()
                    if cols[1].button("❌ Cancel transfer", key=f"no_{t.record_id}", width="stretch"):
                        workflow.student_respond(c.case_id, "STUDENT_CANCEL", sid)
                        refresh(); st.toast("Transfer cancelled. Your money stays in your wallet.", icon="↩️"); st.rerun()
                    if cols[2].button("🚩 It's a scam", key=f"rep_{t.record_id}", width="stretch"):
                        workflow.student_respond(c.case_id, "STUDENT_REPORT", sid)
                        refresh(); st.toast("Thanks! The support team will check it.", icon="🚩"); st.rerun()
                else:
                    if cols[0].button("❌ Cancel transfer", key=f"no_{t.record_id}", width="stretch"):
                        workflow.student_respond(c.case_id, "STUDENT_CANCEL", sid)
                        refresh(); st.toast("Transfer cancelled.", icon="↩️"); st.rerun()
                    cols[1].caption("Only the support team can release this payment.")
            except (InvalidTransition, ApprovalRequired) as e:
                st.error(str(e))
            st.write("")


def page_gate() -> None:
    n_unc = sum(1 for c in STAFF_Q if c.gate and c.gate.uncertain)
    hero("Staff review", "Payments Pay Sentinel escalated: likely scams and messages it was too unsure to judge.",
         [f"{pending} waiting", f"❓ {n_unc} uncertain", f"🚨 {pending - n_unc} scam signs",
          f"Signed in · {officer}"])
    how_to("These payments are <b>paused, not frozen</b>: the student's wallet keeps working. <b>❓ Uncertain</b> means "
           "the message was too unclear to judge (listed first). <b>🚨 Scam signs</b> means reported accounts, phishing "
           "or fake PocketPay messages. Your staff ID comes from your signed session. Try the <b>Guard tests</b> "
           "at the bottom.")
    if not STAFF_Q:
        st.success("All clear: nothing is waiting for staff. Past decisions are in the **Audit log**.", icon="✅")
    else:
        def case_label(a: str) -> str:
            c0 = cases[a]
            tag = "❓ Uncertain" if c0.gate.uncertain else "🚨 Scam signs"
            return f"{tag} · {a} · {c0.transaction.amount_aed:,.2f} AED to {c0.transaction.recipient_handle}"

        pick = st.selectbox(f"Escalated payment ({pending} waiting)", [c.alert.record_id for c in STAFF_Q],
                            format_func=case_label)
        c = cases[pick]
        with st.container(border=True):
            h = st.columns(4)
            h[0].markdown(field("Amount", f"{c.transaction.amount_aed:,.2f} AED"), unsafe_allow_html=True)
            h[1].markdown(field("To", c.transaction.recipient_handle), unsafe_allow_html=True)
            h[2].markdown(field("Student", c.transaction.student_id), unsafe_allow_html=True)
            h[3].markdown(field("Why", "❓ Uncertain" if c.gate.uncertain else "🚨 Scam signs"), unsafe_allow_html=True)
        st.error("**Why it's here:**\n" + "\n".join(f"- {r}" for r in c.gate.reasons), icon="🛑")
        if c.chat:
            st.markdown("**The message the student received:**")
            st.code(visible_text(c.chat.original), language=None, wrap_lines=True)
        if c.investigation:
            st.markdown(f"**Summary.** {c.investigation.staff_summary}")
        with st.expander("📄 Full decision log"):
            st.markdown(c.report_markdown or "")
        with st.container(border=True):
            st.markdown("##### Your decision")
            st.caption(f"Signing as **{officer}**.")
            note = st.text_area("Note", placeholder="Reason (stored in the audit log)")
            b = st.columns(3)
            choices = [("✅ Release payment", "STAFF_RELEASE", "secondary"),
                       ("↩️ Cancel this transfer", "STAFF_CANCEL", "primary"),
                       ("📱 Ask the student to confirm", "STAFF_ASK_STUDENT", "secondary")]
            for col, (text, opt, kind) in zip(b, choices):
                if col.button(text, type=kind, width="stretch", key=opt):
                    try:
                        done = workflow.staff_decide(c.case_id, opt, ss.token, note)
                        refresh()
                        st.toast(f"{done.case_id}: {done.resolution.action.value} → {done.state.value}", icon="✅")
                        st.rerun()
                    except auth.AuthError as e:
                        ss.token = None
                        st.error(f"🔒 {e}")
                        st.rerun()
        with st.container(border=True):
            st.markdown("##### 🧪 Guard tests")
            st.caption("Call the payment-state tool directly, bypassing this screen.")
            g1, g2 = st.columns(2)
            if g1.button("Try to FREEZE the student's wallet", width="stretch"):
                try:
                    services.update_payment_state(c, "FREEZE_WALLET")
                    st.error("Guard FAILED")  # should never happen
                except InvalidTransition as e:
                    st.success(f"🛡️ Refused by code: {e}")
            if g2.button("Release it with no signed approval", width="stretch"):
                try:
                    services.update_payment_state(c, "STAFF_RELEASE")
                    st.error("Guard FAILED")  # should never happen
                except ApprovalRequired as e:
                    st.success(f"🛡️ Refused by code: {e}")

    if STUDENT_Q:
        with st.expander(f"🟡 {len(STUDENT_Q)} payment(s) waiting for students"):
            st.caption(f"Each student is asked at most {config.MAX_STUDENT_PROMPTS} times and holds expire after "
                       f"{config.HOLD_EXPIRY_HOURS}h (money returned). Waiting is a saved state, not a loop.")
            rows = []
            for c in STUDENT_Q:
                rec = services.get_payment_state(c.transaction.record_id)
                rows.append(dict(Payment=c.transaction.record_id, Student=c.transaction.student_id,
                                 To=c.transaction.recipient_handle, Asked=f"{rec.prompts_sent}/{config.MAX_STUDENT_PROMPTS}",
                                 Expires=rec.expires_at))
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            pick = st.selectbox("Send a reminder for", [c.case_id for c in STUDENT_Q])
            if st.button("🔔 Send one reminder"):
                try:
                    workflow.remind_student(pick)
                    refresh(); st.toast("Reminder sent.", icon="🔔"); st.rerun()
                except InvalidTransition as e:
                    st.warning(str(e))


def page_campaigns() -> None:
    hero("📣 Scam alerts", f"Accounts that messaged or charged {config.CAMPAIGN_MIN_STUDENTS}+ different students "
                          f"within a day, found across all payments.")
    how_to("A <b>scam campaign</b> is one account (like <code>@campus_tix_99</code>) hitting many students at once. "
           "Accounts that are still waiting for triage and accounts already triaged are listed separately. Each account's "
           "<b>overall outcome</b> sums up what happened to its payments. Use <b>Broadcast warning</b> to alert every "
           "student about it.")
    camps = load_campaigns(data_version(), False)
    if not camps:
        st.success("No scam campaigns in the current data.", icon="✅")
        return
    outcomes = {c.record_id: [case_outcome(cases.get(_txn_alert.get(t))) for t in c.txn_ids] for c in camps}
    verdicts = {rid: campaign_verdict(o) for rid, o in outcomes.items()}
    done = [c for c in camps if verdicts[c.record_id]["complete"]]
    waiting = [c for c in camps if not verdicts[c.record_id]["complete"]]
    good, total = campaign_precision(camps)
    k = st.columns(4)
    k[0].metric("Suspicious accounts", len(camps), border=True, help=f"eval: {good}/{total} purely scam payments")
    k[1].metric("Students reached", len({s for c in camps for s in c.student_ids}), border=True)
    k[2].metric("Money asked for", f"{sum(c.total_aed for c in camps):,.0f} AED", border=True)
    k[3].metric("Warnings broadcast", len(services.list_broadcasts()), border=True)

    def table(cs):
        return pd.DataFrame([dict(Account=c.handle, Students=len(c.student_ids), Payments=len(c.txn_ids),
                                  Total=c.total_aed, Outcome=verdicts[c.record_id]["label"],
                                  Checked=f"{verdicts[c.record_id]['triaged']}/{verdicts[c.record_id]['total']}")
                             for c in cs])
    cfg = {"Total": st.column_config.NumberColumn("Total (AED)", format="localized")}
    t1, t2 = st.tabs([f"⏳ Awaiting triage ({len(waiting)})", f"✅ Triaged ({len(done)})"])
    with t1:
        if waiting:
            st.dataframe(table(waiting), hide_index=True, width="stretch", column_config=cfg)
            todo = [_txn_alert[t] for c in waiting for t in c.txn_ids if _txn_alert.get(t) not in cases]
            if st.button(f"🔍 Check all {len(todo)} remaining payments", type="primary", key="camp_all"):
                triage_alerts(todo)
        else:
            st.success("Every campaign's payments have been checked.", icon="✅")
    with t2:
        if done:
            st.dataframe(table(done), hide_index=True, width="stretch", column_config=cfg)
        else:
            st.info("Nothing fully checked yet.", icon="⏳")

    st.markdown("#### Account details")
    pick = st.selectbox("Account", [c.record_id for c in waiting + done],
                        format_func=lambda rid: next(f"{c.handle} · {verdicts[rid]['label']}" for c in camps
                                                     if c.record_id == rid))
    c = next(x for x in camps if x.record_id == pick)
    v = verdicts[pick]
    box = {"AWAITING_STAFF": st.error, "AWAITING_STUDENT": st.warning, "MONEY_SENT": st.warning,
           "STOPPED": st.success}.get(v["code"], st.info)
    box(f"**Overall outcome: {v['label']}** · {v['triaged']} of {v['total']} payments checked  \n{v['explain']}")
    left, right = st.columns([1.1, 1], gap="large")
    with left, st.container(border=True):
        st.markdown(f"##### Who {html.escape(c.handle)} reached")
        r = c.recipient
        st.caption(f"{len(c.student_ids)} students between {c.first_ts:%d %b %H:%M} and {c.last_ts:%d %b %H:%M}. "
                   + (f"Account {(c.last_ts.date() - r.created_on).days} days old, trust {r.trust_score}/100, "
                      f"{r.scam_reports} report(s)." if r else "Not a known account.")
                   + (f" Links: {', '.join(c.links)}" if c.links else ""))
        st.graphviz_chart(campaign_dot([c], "dark" if DARK else "light"), width="stretch")
    with right, st.container(border=True):
        st.markdown("##### Each payment's outcome")
        pay = pd.DataFrame([dict(Payment=t, Student=s, Amount=a, Outcome=OUTCOME_LABEL[o])
                            for t, s, a, o in zip(c.txn_ids, c.txn_students, c.amounts_aed, outcomes[pick])])
        if pay.empty:
            st.caption("This account only sent messages; no payments yet.")
        else:
            st.dataframe(pay, hide_index=True, width="stretch",
                         column_config={"Amount": st.column_config.NumberColumn("Amount (AED)", format="localized")})
            rest = [_txn_alert[t] for t, o in zip(c.txn_ids, outcomes[pick]) if o == "UNTRIAGED"]
            if rest and st.button(f"🔍 Check this account's {len(rest)} payment(s)", type="primary", key="camp_one"):
                triage_alerts(rest)
        st.markdown("##### 📣 Broadcast warning")
        default = (f"Heads up! {c.handle} is messaging students with deals that look like a scam. Don't send money "
                   f"or click its links. Report it in the app.")
        text = st.text_area("Message to all students", value=default, key=f"bc_{c.record_id}")
        if st.button("📣 Broadcast warning", key="bc_send"):
            try:
                services.broadcast_scam_alert(c.handle, text, StaffApproval(staff_id=officer, session_token=ss.token))
                refresh(); st.toast("Warning sent to all students.", icon="📣"); st.rerun()
            except ApprovalRequired as e:
                st.error(str(e))
    sent = services.list_broadcasts()
    if sent:
        with st.expander(f"Broadcast history ({len(sent)})"):
            st.dataframe(pd.DataFrame(sent)[["created_at", "recipient_handle", "staff_id", "text"]],
                         hide_index=True, width="stretch")


def page_roi() -> None:
    hero("Results & ROI", "Staff time saved, scams stopped and AI cost across checked payments.")
    if not tel.triaged:
        st.info("Run some payments (sidebar) to fill in the dashboard.", icon="📊")
        return
    how_to("The headline numbers: everyday payments cleared with no human, scams that slipped through (target "
           "<b>0</b>), unclear messages sent to a human, and support-team hours saved. Quality is scored against the "
           "synthetic dataset's hidden answers, which the AI never sees.")
    st.caption(f"Over **{tel.triaged:,}** checked payments"
               + (f" ({tel.unlabeled} entered manually: counted for workload, not for quality)" if tel.unlabeled else "")
               + ".")
    k = st.columns(5)
    k[0].metric("Safe payments cleared instantly", f"{tel.auto_clear_rate:.0%}", border=True,
                help=f"{tel.safe_cleared}/{tel.safe_total} safe payments needed no human at all")
    k[1].metric("Scams that went through", tel.scams_cleared, border=True,
                help=f"{tel.scams_paused}/{tel.scams_total} scams were paused")
    k[2].metric("Unclear messages escalated", f"{tel.uncertain_escalated}/{tel.uncertain_total}", border=True)
    k[3].metric("Staff hours saved", f"{tel.hours_saved:,.1f}", border=True,
                help=f"{tel.baseline_hours} h by hand → {tel.agent_hours} h; {tel.student_queue} holds were settled "
                     f"by students, not staff")
    k[4].metric("Staff queue that really needed staff", f"{tel.queue_precision:.0%}", border=True,
                delta=f"{(tel.queue_precision - tel.legacy_precision) * 100:+.0f} pts vs old rules",
                help=f"Old keyword rules: {tel.legacy_precision:.0%} of flags needed a human")
    with st.container(border=True):
        st.markdown("##### Where each payment went, by true label")
        odf = pd.DataFrame(tel.outcome)
        order = [r.value for r in Route]
        odf["order"] = odf["route"].map(order.index)
        chart = (alt.Chart(odf[odf["count"] > 0])
                 .mark_bar(stroke=SURFACE, strokeWidth=2, size=26)
                 .encode(y=alt.Y("true_class:N", sort=["Safe", "Scam", "Uncertain", "Hidden instructions"], title=None),
                         x=alt.X("count:Q", stack="zero", title="Payments"),
                         color=alt.Color("route:N", scale=alt.Scale(domain=order, range=SERIES),
                                         legend=alt.Legend(orient="top", title=None)),
                         order=alt.Order("order:Q"),
                         tooltip=[alt.Tooltip("true_class:N", title="True label"),
                                  alt.Tooltip("route:N", title="Decision"), alt.Tooltip("count:Q", title="Payments")])
                 .properties(height=200)
                 .configure_axis(grid=False, domain=False)
                 .configure_view(stroke=None))
        st.altair_chart(chart, width="stretch")
        with st.expander("Table view"):
            st.dataframe(odf.pivot(index="true_class", columns="route", values="count")[order], width="stretch")
    st.markdown("##### AI cost & reliability")
    k = st.columns(5)
    k[0].metric("AI calls", tel.llm_calls, border=True)
    k[1].metric("Tokens / payment", f"{(tel.tokens_in + tel.tokens_out) / tel.triaged:,.0f}", border=True)
    k[2].metric("Avg latency / call", f"{tel.avg_latency_s:.2f} s", border=True)
    priced = config.LLM_COST_PER_1M_INPUT or config.LLM_COST_PER_1M_OUTPUT
    k[3].metric("Compute cost / payment", f"${tel.cost_per_case_usd:.5f}" if priced else "n/a", border=True,
                help=f"Total ${tel.cost_usd:.4f}" if priced else
                "Set LLM_COST_PER_1M_INPUT/OUTPUT in .env from your provider's pricing page")
    k[4].metric("Made-up findings deleted", tel.findings_dropped, border=True,
                help=f"{tel.findings_kept} citation-verified findings kept")
    if tel.by_purpose:
        st.dataframe(pd.DataFrame(tel.by_purpose), hide_index=True, width="stretch")
    st.caption(f"Assumptions: the 2-person team reviews ~200 flagged payments + 20 messages a day at "
               f"~{config.STAFF_MIN_PER_REVIEW:.0f} min each; an escalated case with summary ready takes "
               f"~{config.STAFF_MIN_ASSISTED:.0f} min; cleared payments and student-settled holds take no staff time.")


ACTION_TEXT = {"CLEAR": "🟢 Cleared", "HOLD_FOR_CONFIRMATION": "🟡 Held for student", "ESCALATE": "🔴 Escalated",
               "REMIND": "🔔 Reminder", "STUDENT_CONFIRM": "✅ Student confirmed", "STUDENT_CANCEL": "↩️ Student cancelled",
               "STUDENT_REPORT": "🚩 Student reported scam", "STAFF_RELEASE": "✅ Staff released",
               "STAFF_CANCEL": "↩️ Staff cancelled", "STAFF_ASK_STUDENT": "📱 Staff asked student", "EXPIRE": "⌛ Expired"}


def page_audit() -> None:
    hero("Audit log", "A permanent record of every state change: what happened, to which payment, and who did it.")
    how_to("<b>System</b> = automatic steps taken by code. <code>student:</code> and <code>staff:</code> = human "
           "decisions. Turn on <b>Show details</b> for the rule-based reasons behind each step.")
    recs = services.list_resolutions()
    if not recs:
        st.info("Nothing recorded yet. Check some payments first.", icon="📜")
        return
    full = pd.DataFrame([dict(When=r.committed_at, Payment=r.txn_id, Action=ACTION_TEXT[r.action.value],
                              Change=f"{r.from_state.value if r.from_state else 'new'} → {r.to_state.value}",
                              By=r.actor, Note=r.note, Reasons="; ".join(r.reasons), Record=r.record_id)
                         for r in recs])
    m = st.columns(3)
    m[0].metric("Steps recorded", len(full), border=True)
    m[1].metric("Human decisions", int((full["By"] != "system").sum()), border=True)
    m[2].metric("Wallets frozen", 0, border=True, help="Not possible: there is no freeze action")
    c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
    view = c1.segmented_control("Show", ["All", "Human decisions", "Automatic"], default="All") or "All"
    details = c2.toggle("Show details")
    df = full if view == "All" else full[(full["By"] != "system") == (view == "Human decisions")]
    cols = ["When", "Payment", "Action", "By", "Note"] + (["Change", "Reasons", "Record"] if details else [])
    st.dataframe(df[cols], hide_index=True, width="stretch", height=430, column_config={
        "When": st.column_config.DatetimeColumn(format="MMM D, HH:mm:ss"),
        "Reasons": st.column_config.TextColumn(width="large")})
    st.download_button("⬇️ Download full log (.csv)", full.to_csv(index=False), file_name="pay_sentinel_audit_log.csv")


# ---- data entry -------------------------------------------------------------------------------------------
INTAKE_COLS = ["student_id", "amount_aed", "txn_type", "channel", "recipient_handle", "recipient_name", "message",
               "memo", "device"]
EXAMPLES = {
    "Canteen snack": dict(student_id="STU-0020", amount_aed=12.0, txn_type="PURCHASE", channel="QR",
                          recipient_handle="@campus_canteen", recipient_name="", message="", memo="lunch",
                          device="known"),
    "Ticket scam (from the brief)": dict(
        student_id="STU-0021", amount_aed=50.0, txn_type="P2P", channel="LINK", recipient_handle="@campus_tix_99",
        recipient_name="", memo="", device="known",
        message="Bro urgent! Selling my Front Row Fest pass for 50 AED, send to @campus_tix_99 via PocketPay fast "
                "before I give it to someone else, click here to claim! frontrow-fest.co/claim"),
    "Unclear slang": dict(student_id="STU-0022", amount_aed=45.0, txn_type="P2P", channel="APP",
                          recipient_handle="@drip_plug_zay", recipient_name="", memo="", device="known",
                          message="ngl my plug has the drip, slide the bread and it's yours"),
}
TRIAGE_NOW_MAX = 25


@st.cache_data(show_spinner=False)
def load_students(version: tuple) -> list[dict]:
    return services.list_students()


def _rows_from(df: pd.DataFrame) -> list[dict]:
    recs = df.to_dict("records")
    return [r for r in recs if any(not services._blank(v) for v in r.values())]


def _import(items, triage: bool) -> None:
    out = services.add_transactions(items, officer)
    if triage and len(out) <= TRIAGE_NOW_MAX:
        bar = st.progress(0.0, text="Checking new payments…")
        for i, o in enumerate(out, 1):
            workflow.run_case(o["alert_id"], use_llm=ss.get("use_llm", True), simulate_outage=ss.get("outage", False))
            bar.progress(i / len(out), text=f"{o['alert_id']} ({i}/{len(out)})")
    ss.last_import = dict(rows=out, triaged=triage and len(out) <= TRIAGE_NOW_MAX)
    refresh()
    st.rerun()


def _validation_summary(rows: list[dict]):
    valid, errors = services.validate_new_transactions(rows)
    a, b = st.columns(2)
    a.metric("✅ Ready to import", len(valid), border=True)
    b.metric("⚠️ Rows with problems", len(errors), border=True)
    if errors:
        st.dataframe(pd.DataFrame(errors).rename(columns={"row": "Row", "error": "Problem"}),
                     hide_index=True, width="stretch")
    return valid


def page_add() -> None:
    hero("Add payments", "Enter flagged payments and the chat messages behind them, one at a time or in bulk.")
    how_to("New entries become flagged payments, just like PocketPay's own. Messages and notes are treated as "
           "<b>untrusted</b>, so try typing a scam or some slang a friend might send. Entries are tagged with your "
           "staff ID and are <b>not</b> counted in the accuracy scores (they have no known answer).")
    last = ss.pop("last_import", None)
    if last:
        rows = last["rows"]
        with st.container(border=True):
            st.success(f"Imported {len(rows)} payment(s)" + (" and checked them." if last["triaged"] else "."), icon="✅")
            cur = load_cases(data_version())
            st.dataframe(pd.DataFrame([dict(Alert=r["alert_id"], Payment=r["txn_id"],
                                            Status=STATE_TEXT[cur[r["alert_id"]].state.value]
                                            if r["alert_id"] in cur and cur[r["alert_id"]].state else STATE_TEXT[""])
                                       for r in rows]), hide_index=True, width="stretch")
            if len(rows) == 1 and st.button(f"🔍 Open {rows[0]['alert_id']} in Investigation", type="primary"):
                open_case(rows[0]["alert_id"])
            if not last["triaged"]:
                st.caption("Use ▶ Check the next batch (sidebar → More tools) to check them.")

    students = load_students(data_version())
    names = {s["student_id"]: f"{s['full_name']} ({s['grade']})" for s in students}
    t_one, t_grid, t_csv = st.tabs(["✏️ Single payment", "📋 Bulk: type in a table", "📤 Bulk: upload CSV"])

    with t_one:
        st.caption("Start from an example, or fill the form yourself.")
        ex = st.columns(len(EXAMPLES))
        for col, (label, data) in zip(ex, EXAMPLES.items()):
            if col.button(f"Use example: {label}", width="stretch"):
                for k, v in data.items():
                    ss[f"f_{k}"] = v
                st.rerun()
        ss.setdefault("f_amount_aed", 20.0)
        with st.form("one_txn"):
            a, b = st.columns(2)
            sid = a.selectbox("Student", list(names), format_func=lambda s: f"{s} · {names[s]}", key="f_student_id")
            amount = b.number_input("Amount (AED)", min_value=0.01, max_value=5000.0, step=5.0, key="f_amount_aed")
            a, b, c = st.columns(3)
            ttype = a.selectbox("Payment type", ["P2P", "PURCHASE"], key="f_txn_type",
                                format_func=lambda x: "Send to a student" if x == "P2P" else "Pay a campus shop")
            channel = b.selectbox("How", ["APP", "QR", "LINK"], key="f_channel",
                                  format_func=lambda x: {"APP": "In the app", "QR": "QR code", "LINK": "Payment link"}[x])
            device = c.selectbox("Phone", ["known", "new"], key="f_device",
                                 format_func=lambda d: "Student's usual phone" if d == "known" else "A new phone")
            a, b = st.columns(2)
            handle = a.text_input("Pay to (@handle)", placeholder="@campus_canteen", key="f_recipient_handle")
            rname = b.text_input("Display name (optional)", key="f_recipient_name")
            msg = st.text_area("Chat message the student received (untrusted)", max_chars=500, key="f_message")
            memo = st.text_input("Payment note (untrusted, optional)", max_chars=200, key="f_memo")
            triage = st.checkbox("Check it straight away", value=True)
            if st.form_submit_button("➕ Add payment", type="primary"):
                raw = dict(student_id=sid, amount_aed=amount, txn_type=ttype, channel=channel, recipient_handle=handle,
                           recipient_name=rname, message=msg, memo=memo, device=device)
                valid, errors = services.validate_new_transactions([raw])
                if errors:
                    st.error(f"Please fix: {errors[0]['error']}", icon="⚠️")
                else:
                    _import(valid, triage)

    with t_grid:
        st.caption("Edit the cells directly. Use the **+** at the bottom of the table to add rows, and select rows "
                   "then press the bin icon to delete them.")
        ss.setdefault("grid_ver", 0)
        starter = pd.DataFrame(list(EXAMPLES.values()), columns=INTAKE_COLS)
        edited = st.data_editor(starter, num_rows="dynamic", hide_index=True, width="stretch",
                                key=f"grid_{ss.grid_ver}", column_config={
            "student_id": st.column_config.SelectboxColumn("Student", options=list(names), required=True),
            "amount_aed": st.column_config.NumberColumn("Amount (AED)", min_value=0.01, format="localized"),
            "txn_type": st.column_config.SelectboxColumn("Type", options=["P2P", "PURCHASE"]),
            "channel": st.column_config.SelectboxColumn("How", options=["APP", "QR", "LINK"]),
            "recipient_handle": "Pay to (@handle)", "recipient_name": "Display name",
            "message": st.column_config.TextColumn("Chat message", width="large"),
            "memo": "Note", "device": st.column_config.SelectboxColumn("Phone", options=["known", "new"])})
        valid = _validation_summary(_rows_from(edited))
        a, b = st.columns([1, 2], vertical_alignment="center")
        triage = b.checkbox(f"Check them straight away (up to {TRIAGE_NOW_MAX} rows)", value=True, key="grid_triage")
        if a.button(f"➕ Import {len(valid)} row(s)", type="primary", disabled=not valid, width="stretch"):
            ss.grid_ver += 1
            _import(valid, triage)

    with t_csv:
        st.caption(f"Upload a CSV with these columns (header row required, up to {services.INTAKE_MAX_ROWS} rows). "
                   "Only the first five are required.")
        st.code(",".join(INTAKE_COLS), language=None)
        st.download_button("⬇️ Download template with examples",
                           pd.DataFrame(list(EXAMPLES.values()), columns=INTAKE_COLS).to_csv(index=False),
                           file_name="pay_sentinel_template.csv")
        up = st.file_uploader("CSV file", type=["csv"])
        if up is not None:
            import hashlib
            digest = hashlib.sha256(up.getvalue()).hexdigest()
            try:
                cdf = pd.read_csv(up, dtype=str, keep_default_na=False)
            except Exception as e:  # noqa: BLE001
                st.error(f"Could not read this CSV: {e}")
                return
            cdf.columns = [str(c).strip().lower() for c in cdf.columns]
            missing = [c for c in INTAKE_COLS[:5] if c not in cdf.columns]
            if missing:
                st.error(f"Missing required column(s): {', '.join(missing)}", icon="⚠️")
                return
            st.markdown("**Preview** (first 10 rows)")
            st.dataframe(cdf.head(10), hide_index=True, width="stretch")
            valid = _validation_summary(_rows_from(cdf))
            done = ss.get("csv_imported") == digest
            if done:
                st.info("This file has already been imported.", icon="ℹ️")
            a, b = st.columns([1, 2], vertical_alignment="center")
            triage = b.checkbox(f"Check them straight away (up to {TRIAGE_NOW_MAX} rows)", value=True, key="csv_triage")
            if a.button(f"➕ Import {len(valid)} row(s)", type="primary", disabled=not valid or done, width="stretch"):
                ss.csv_imported = digest
                _import(valid, triage)


# ---- navigation + sidebar ---------------------------------------------------------------------------------
P_HOME = st.Page(page_home, title="Start here", icon="🏠", url_path="start", default=True)
P_QUEUE = st.Page(page_queue, title="Flagged payments", icon="📥", url_path="queue")
P_INVESTIGATE = st.Page(page_investigation, title="Investigation", icon="🔍", url_path="investigation")
P_STUDENT = st.Page(page_student, title=f"Student app ({len(STUDENT_Q)})", icon="📱", url_path="student")
P_GATE = st.Page(page_gate, title=f"Staff review ({pending})", icon="🧑‍💼", url_path="staff-review")
P_ADD = st.Page(page_add, title="Add payments", icon="➕", url_path="add")
P_ROI = st.Page(page_roi, title="Results & ROI", icon="📊", url_path="roi")
P_CAMPAIGNS = st.Page(page_campaigns, title="Scam alerts", icon="📣", url_path="scam-alerts")
nav = st.navigation({
    "": [P_HOME],
    "Work": [P_QUEUE, P_INVESTIGATE, P_STUDENT, P_GATE, P_ADD],
    "Insights": [P_ROI, P_CAMPAIGNS],
    "Records": [st.Page(page_audit, title="Audit log", icon="📜", url_path="audit-log")],
})

with st.sidebar:
    st.markdown(f'<p class="ps-side-brand">🛡️ {BRAND}</p><p class="ps-side-sub">{TAGLINE}</p>',
                unsafe_allow_html=True)
    mins = max(0, int((auth.token_expiry(ss.token) - time.time()) // 60))
    st.markdown(f'<div class="ps-user"><b>{html.escape(officer)}</b><br>{html.escape(officer_title)}<br>'
                f'<span style="opacity:.7">Session expires in {mins} min</span></div>', unsafe_allow_html=True)
    if st.button("Sign out", width="stretch"):
        ss.token = None
        ss.selected_alert = None
        st.rerun()
    st.write("")
    if st.button("▶ Run 12 test scenarios", type="primary", width="stretch",
                 help="Snacks, the Front Row Fest scam, fake PocketPay messages, 3 unclear-slang cases, an attack "
                      "hidden in a note and a service outage"):
        run_demo()
        st.rerun()
    with st.expander("⚙️ More tools"):
        ss.use_llm = st.toggle("Use AI agents", value=ss.get("use_llm", True),
                               help="Off = rules only (message scanners + fixed payment rules), no AI reading.")
        ss.outage = st.toggle("Simulate verification outage", value=ss.get("outage", False),
                              help="verify_campus_recipient() times out: payments pause safely instead of crashing.")
        ss.workers = st.slider("Payments checked at the same time", 1, workflow.MAX_WORKERS,
                               value=ss.get("workers", workflow.DEFAULT_WORKERS),
                               help="Checks run in parallel; AI calls dominate the time, so more at once is faster. "
                                    "Keep it modest on free-tier AI keys (rate limits).")
        st.caption(f"Model: `{config.LLM_MODEL}`")
        st.markdown("**Check the next payments**")
        c1, c2 = st.columns([1, 1.3], vertical_alignment="bottom")
        n = c1.number_input("How many", min_value=1, max_value=1000, value=20, label_visibility="collapsed")
        if c2.button("▶ Check batch", width="stretch"):
            todo = [a.record_id for a in services.list_alerts(limit=int(n), status="NEW")]
            triage_alerts(todo)
        st.markdown("**Unanswered holds**")
        if st.button("⌛ Expire overdue holds", width="stretch",
                     help=f"Holds older than {config.HOLD_EXPIRY_HOURS}h are cancelled and the money returned"):
            n_exp = len(workflow.expire_holds())
            refresh(); st.toast(f"{n_exp} hold(s) expired, money returned.", icon="⌛"); st.rerun()
        st.markdown("**Start over**")
        confirm = st.checkbox("Delete all results and regenerate the data")
        if st.button("Reset dataset", disabled=not confirm, width="stretch"):
            shutil.rmtree(config.REPORTS_DIR, ignore_errors=True)
            seed_data.build_database(config.DB_PATH)
            refresh()
            st.rerun()

batch_summary()   # result of the last multi-payment check, shown once on whichever page you land on
nav.run()
