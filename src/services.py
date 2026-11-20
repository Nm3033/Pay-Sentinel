"""Mock PocketPay services over SQLite (plain Python; no LLM).

The three tools from the brief:
  get_transaction_context()  - the payment, the student, their devices/logins, the chat message, 24h spend
  verify_campus_recipient()  - verified campus vendor? new/unverified account? trust score, scam reports
                               (raises VerificationTimeout when the upstream service is down)
  update_payment_state()     - the ONLY way a payment changes state. A fixed transition table: there is
                               no "freeze wallet" action at all, student actions must come from that student,
                               staff actions need a signed session, and reminders are capped (no loops).

Every returned record carries a citable record_id (STU-, DEV-, LGN-, MSG-, RCP-, TXN-, VEL-, SPR-, RES-).
Evaluation-only columns (ground_truth, scenario, is_injection, injection_variant, label, expected_route)
are NEVER selected here, so they can't reach an agent.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from src import config
from src.schemas import (
    Action, Alert, CaseFile, ChatMessage, Device, DeviceLogin, NewTransaction, PaymentState,
    PaymentStateRecord, Recipient, RecipientCheck, ResolutionRecord, SpreadStats, StaffApproval,
    StudentProfile, TERMINAL_STATES, Transaction, TransactionContext, VelocityStats,
)


class ServiceError(Exception):
    """Upstream service failure."""


class NotFound(ServiceError):
    pass


class VerificationTimeout(ServiceError):
    """verify_campus_recipient() did not answer in time (simulated outage)."""


class ApprovalRequired(ServiceError):
    """A protected action was attempted without the right human (student / signed-in staff)."""


class InvalidTransition(ServiceError):
    """The requested state change is not in the allowed transition table."""


# ---- Connection ----------------------------------------------------------------
def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or config.DB_PATH)
    if not path.exists():
        raise ServiceError(f"Database not found at {path}. Run: python -m src.seed_data")
    conn = sqlite3.connect(path, timeout=30)          # parallel triage: wait for the write lock, don't fail
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")         # readers don't block the writer (and vice versa)
    return conn


@contextmanager
def _use(conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    if conn is not None:
        yield conn
        return
    own = connect()
    try:
        yield own
    finally:
        own.close()


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


# ---- Readers -------------------------------------------------------------------
_TXN_COLS = ("txn_id, student_id, ts, txn_type, channel, amount_aed, device_id, recipient_handle, recipient_name, "
             "message_id, memo")


def _txn(row: sqlite3.Row) -> Transaction:
    d = dict(row)
    d["record_id"] = d.pop("txn_id")
    d["memo"] = d["memo"] or ""
    return Transaction(**d)


def get_transaction(txn_id: str, *, conn: sqlite3.Connection | None = None) -> Transaction:
    with _use(conn) as c:
        row = c.execute(f"SELECT {_TXN_COLS} FROM transactions WHERE txn_id = ?", (txn_id,)).fetchone()
    if row is None:
        raise NotFound(f"Transaction {txn_id} not found")
    return _txn(row)


def _alert(row: sqlite3.Row) -> Alert:
    d = dict(row)
    d["record_id"] = d.pop("alert_id")
    return Alert(**d)


_ALERT_COLS = "alert_id, txn_id, legacy_rule, created_at, status"


def get_alert(alert_id: str, *, conn: sqlite3.Connection | None = None) -> Alert:
    with _use(conn) as c:
        row = c.execute(f"SELECT {_ALERT_COLS} FROM alerts WHERE alert_id = ?", (alert_id,)).fetchone()
    if row is None:
        raise NotFound(f"Alert {alert_id} not found")
    return _alert(row)


def get_alert_for_txn(txn_id: str, *, conn: sqlite3.Connection | None = None) -> Alert:
    with _use(conn) as c:
        row = c.execute(f"SELECT {_ALERT_COLS} FROM alerts WHERE txn_id = ?", (txn_id,)).fetchone()
    if row is None:
        raise NotFound(f"No alert for {txn_id}")
    return _alert(row)


def list_alerts(limit: int | None = None, status: str | None = None, *,
                conn: sqlite3.Connection | None = None) -> list[Alert]:
    sql, args = f"SELECT {_ALERT_COLS} FROM alerts", []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY alert_id"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    with _use(conn) as c:
        return [_alert(r) for r in c.execute(sql, args).fetchall()]


def list_demo_scenarios(*, conn: sqlite3.Connection | None = None) -> list[dict]:
    """The named test scenarios (titles only; the expected outcome is evaluation-only)."""
    with _use(conn) as c:
        return [dict(r) for r in c.execute("SELECT ordinal, txn_id, title FROM demo_scenarios ORDER BY ordinal")]


def get_message(message_id: str, *, conn: sqlite3.Connection | None = None) -> ChatMessage:
    with _use(conn) as c:
        row = c.execute("SELECT message_id, student_id, ts, sender_handle, platform, text, link_url FROM messages "
                        "WHERE message_id = ?", (message_id,)).fetchone()
    if row is None:
        raise NotFound(f"Message {message_id} not found")
    d = dict(row)
    d["record_id"] = d.pop("message_id")
    return ChatMessage(**d)


# ---- Tool 1: transaction context ------------------------------------------------------
def compute_velocity(student_id: str, as_of: datetime, *, window_hours: int = 24,
                     conn: sqlite3.Connection | None = None) -> VelocityStats:
    start = as_of - timedelta(hours=window_hours)
    with _use(conn) as c:
        rows = c.execute(
            "SELECT txn_id, amount_aed, recipient_handle FROM transactions WHERE student_id = ? AND ts >= ? "
            "AND ts <= ? ORDER BY ts, txn_id",
            (student_id, start.isoformat(timespec="seconds"), as_of.isoformat(timespec="seconds"))).fetchall()
    return VelocityStats(record_id=f"VEL-{student_id}-{as_of:%Y%m%dT%H%M%S}", student_id=student_id,
                         window_start=start, window_end=as_of, txn_count=len(rows),
                         total_aed=round(sum(r["amount_aed"] for r in rows), 2),
                         distinct_recipients=len({r["recipient_handle"] for r in rows}),
                         txn_ids=[r["txn_id"] for r in rows])


def get_transaction_context(txn_id: str, *, login_limit: int = 10,
                            conn: sqlite3.Connection | None = None) -> TransactionContext:
    """Tool: the payment plus everything known about the student at that moment."""
    with _use(conn) as c:
        t = get_transaction(txn_id, conn=c)
        as_of = t.ts.isoformat(timespec="seconds")
        s = c.execute("SELECT * FROM students WHERE student_id = ?", (t.student_id,)).fetchone()
        if s is None:
            raise NotFound(f"Student {t.student_id} not found")
        devs = c.execute("SELECT * FROM devices WHERE student_id = ? AND first_seen <= ? ORDER BY device_id",
                         (t.student_id, as_of[:10])).fetchall()
        logins = c.execute("SELECT * FROM device_logins WHERE student_id = ? AND ts <= ? ORDER BY ts DESC LIMIT ?",
                           (t.student_id, as_of, login_limit)).fetchall()
        msg = get_message(t.message_id, conn=c) if t.message_id else None
        vel = compute_velocity(t.student_id, t.ts, conn=c)
    sd = dict(s)
    sd["record_id"] = sd.pop("student_id")
    devices = []
    for r in devs:
        d = dict(r)
        d["record_id"] = d.pop("device_id")
        devices.append(Device(**d))
    recent = []
    for r in logins:
        d = dict(r)
        d["record_id"] = d.pop("login_id")
        d.pop("student_id")
        recent.append(DeviceLogin(**d))
    return TransactionContext(transaction=t, student=StudentProfile(**sd), devices=devices, recent_logins=recent,
                              message=msg, velocity=vel)


# ---- Tool 2: recipient verification ---------------------------------------------------
def verify_campus_recipient(handle: str, *, as_of: date | None = None, simulate_outage: bool = False,
                            conn: sqlite3.Connection | None = None) -> RecipientCheck:
    """Tool: is the receiver a verified campus vendor, an established student, or a new/unverified account?
    Raises VerificationTimeout if the (mock) upstream service is down."""
    with _use(conn) as c:
        row = c.execute("SELECT * FROM recipients WHERE handle = ?", (handle.lower(),)).fetchone()
    if simulate_outage or (row is not None and row["api_status"] == "TIMEOUT"):
        raise VerificationTimeout("campus recipient directory did not respond within 3s")
    if row is None:
        return RecipientCheck(handle=handle, found=False, recipient=None, account_age_days=None, verified_vendor=False)
    d = dict(row)
    d["record_id"] = d.pop("recipient_id")
    d.pop("api_status")
    rec = Recipient(**d)
    age = ((as_of or date.today()) - rec.created_on).days
    return RecipientCheck(handle=rec.handle, found=True, recipient=rec, account_age_days=age,
                          verified_vendor=rec.kind == "VENDOR" and rec.verified)


def compute_recipient_spread(handle: str, around: datetime, *, window_hours: int = 24,
                             conn: sqlite3.Connection | None = None) -> SpreadStats:
    """How many different students this recipient paid-or-messaged within +/- window (campaign detection)."""
    start, end = around - timedelta(hours=window_hours), around + timedelta(hours=window_hours)
    s, e = start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")
    with _use(conn) as c:
        txns = c.execute("SELECT txn_id, student_id FROM transactions WHERE recipient_handle = ? AND ts >= ? "
                         "AND ts <= ? ORDER BY ts, txn_id", (handle, s, e)).fetchall()
        msgs = c.execute("SELECT message_id, student_id FROM messages WHERE sender_handle = ? AND ts >= ? "
                         "AND ts <= ? ORDER BY ts, message_id", (handle, s, e)).fetchall()
    students = sorted({r["student_id"] for r in txns} | {r["student_id"] for r in msgs})
    return SpreadStats(record_id=f"SPR-{re.sub(r'[^a-z0-9]', '', handle.lower())[:12]}-{around:%Y%m%dT%H%M%S}",
                       recipient_handle=handle, window_start=start, window_end=end,
                       distinct_students=len(students), student_ids=students,
                       txn_ids=[r["txn_id"] for r in txns], message_ids=[r["message_id"] for r in msgs])


# ---- Tool 3: payment state (stateful memory + action boundaries) ------------------------------
_P = PaymentState
TRANSITIONS: dict[tuple[PaymentState | None, Action], PaymentState] = {
    (None, Action.CLEAR): _P.CLEARED,
    (None, Action.HOLD_FOR_CONFIRMATION): _P.PENDING_CONFIRMATION,
    (None, Action.ESCALATE): _P.ESCALATED,
    (_P.PENDING_CONFIRMATION, Action.REMIND): _P.PENDING_CONFIRMATION,
    (_P.PENDING_CONFIRMATION, Action.STUDENT_CONFIRM): _P.SENT,
    (_P.PENDING_CONFIRMATION, Action.STUDENT_CANCEL): _P.CANCELLED,
    (_P.PENDING_CONFIRMATION, Action.STUDENT_REPORT): _P.ESCALATED,
    (_P.PENDING_CONFIRMATION, Action.ESCALATE): _P.ESCALATED,
    (_P.PENDING_CONFIRMATION, Action.EXPIRE): _P.EXPIRED,
    (_P.ESCALATED, Action.STAFF_RELEASE): _P.SENT,
    (_P.ESCALATED, Action.STAFF_CANCEL): _P.CANCELLED,
    (_P.ESCALATED, Action.STAFF_ASK_STUDENT): _P.PENDING_CONFIRMATION,
    (_P.ESCALATED, Action.STUDENT_CANCEL): _P.CANCELLED,   # a student can always cancel their own transfer
}
_SYSTEM = {Action.CLEAR, Action.HOLD_FOR_CONFIRMATION, Action.ESCALATE, Action.REMIND, Action.EXPIRE}
_STUDENT = {Action.STUDENT_CONFIRM, Action.STUDENT_CANCEL, Action.STUDENT_REPORT}
_STAFF = {Action.STAFF_RELEASE, Action.STAFF_CANCEL, Action.STAFF_ASK_STUDENT}
_AWAITING = {_P.PENDING_CONFIRMATION: "STUDENT", _P.ESCALATED: "STAFF"}


def _state_row(r: sqlite3.Row) -> PaymentStateRecord:
    d = dict(r)
    d["history"] = json.loads(d["history"] or "[]")
    return PaymentStateRecord(**d)


def get_payment_state(txn_id: str, *, conn: sqlite3.Connection | None = None) -> PaymentStateRecord | None:
    with _use(conn) as c:
        row = c.execute("SELECT * FROM payment_states WHERE txn_id = ?", (txn_id,)).fetchone()
    return _state_row(row) if row else None


def list_payment_states(state: PaymentState | None = None, *,
                        conn: sqlite3.Connection | None = None) -> list[PaymentStateRecord]:
    sql, args = "SELECT * FROM payment_states", []
    if state:
        sql += " WHERE state = ?"
        args.append(PaymentState(state).value)
    with _use(conn) as c:
        return [_state_row(r) for r in c.execute(sql + " ORDER BY txn_id", args)]


def _independent_guard(t: Transaction, c: sqlite3.Connection) -> list[str]:
    """Re-derive from the DB why a payment must NOT be cleared automatically (defense in depth)."""
    reasons = []
    rec = c.execute("SELECT kind, verified, scam_reports FROM recipients WHERE handle = ?",
                    (t.recipient_handle,)).fetchone()
    if rec is None:
        reasons.append("recipient unknown to PocketPay")
    else:
        if rec["scam_reports"] >= config.REPORTS_ESCALATE:
            reasons.append(f"recipient has {rec['scam_reports']} scam reports")
        vendor = rec["kind"] == "VENDOR" and rec["verified"]
        limit = config.VENDOR_NORMAL_MAX_AED if vendor else config.P2P_AUTO_MAX_AED
        if t.amount_aed > limit:
            reasons.append(f"{t.amount_aed:,.2f} AED is above the {limit:,.0f} AED automatic limit")
    return reasons


def update_payment_state(case: CaseFile | str, action: Action | str, *, actor: str = "system",
                         approval: StaffApproval | None = None, note: str = "", reasons: list[str] | None = None,
                         now: datetime | None = None, conn: sqlite3.Connection | None = None) -> PaymentStateRecord:
    """Tool: move a payment to its next state. Raises InvalidTransition for anything not in TRANSITIONS
    (including any attempt to freeze a wallet), ApprovalRequired for the wrong human."""
    try:
        action = Action(action)
    except ValueError:
        raise InvalidTransition(f"'{action}' is not an allowed action. Pay Sentinel can only clear, hold for "
                                f"confirmation or escalate; it can never freeze a wallet.") from None
    txn_id = case.transaction.record_id if isinstance(case, CaseFile) else case
    now = now or _now()
    with _use(conn) as c:
        t = get_transaction(txn_id, conn=c)
        cur = get_payment_state(txn_id, conn=c)
        frm = cur.state if cur else None
        to = TRANSITIONS.get((frm, action))
        if to is None:
            raise InvalidTransition(f"{action.value} is not allowed when the payment is "
                                    f"{frm.value if frm else 'new'}")
        # who may do this
        if action in _STUDENT and actor != f"student:{t.student_id}":
            raise ApprovalRequired(f"Only the student who made {txn_id} ({t.student_id}) can do {action.value}")
        if action in _STAFF:
            from src.auth import AuthError, verify_token
            if approval is None:
                raise ApprovalRequired(f"{action.value} needs a signed-in support staff member")
            try:
                who = verify_token(approval.session_token)
            except AuthError as e:
                raise ApprovalRequired(f"{action.value} needs a valid staff session ({e})") from None
            if who != approval.staff_id:
                raise ApprovalRequired(f"Approval names {approval.staff_id} but the session belongs to {who}")
            actor = f"staff:{who}"
        if action in _SYSTEM:
            actor = "system"
        if action == Action.CLEAR:
            guard = _independent_guard(t, c)
            if guard:
                raise ApprovalRequired("Automatic CLEAR refused: " + "; ".join(guard))
        # stateful memory: prompts + expiry
        prompts = cur.prompts_sent if cur else 0
        expires = cur.expires_at if cur else None
        if action == Action.REMIND:
            if prompts >= config.MAX_STUDENT_PROMPTS:
                raise InvalidTransition(f"Already asked the student {prompts} times; Pay Sentinel never asks "
                                        f"again (no loops). The hold will expire on its own.")
            prompts += 1
        if to == _P.PENDING_CONFIRMATION and action != Action.REMIND:
            prompts += 1
            expires = now + timedelta(hours=config.HOLD_EXPIRY_HOURS)
        if to in TERMINAL_STATES:
            expires = None
        history = (cur.history if cur else []) + [dict(at=now.isoformat(timespec="seconds"), action=action.value,
                                                       actor=actor, to=to.value, note=note)]
        created = cur.created_at if cur else now
        c.execute("INSERT OR REPLACE INTO payment_states VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (txn_id, to.value, _AWAITING.get(to, "NONE"), prompts, created.isoformat(timespec="seconds"),
                   now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds") if expires else None,
                   action.value, actor, json.dumps(history)))
        alert = get_alert_for_txn(txn_id, conn=c)
        c.execute("INSERT INTO case_resolutions (case_id, txn_id, action, from_state, to_state, actor, note, reasons, "
                  "committed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (alert.record_id.replace("ALR-", "CASE-"), txn_id, action.value, frm.value if frm else None,
                   to.value, actor, note, json.dumps(reasons or []),
                   datetime.now(timezone.utc).isoformat(timespec="seconds")))
        c.execute("UPDATE alerts SET status = ? WHERE txn_id = ?", (to.value, txn_id))
        c.commit()
    return get_payment_state(txn_id, conn=conn) if conn else get_payment_state(txn_id)


def expire_stale_holds(now: datetime | None = None, *, conn: sqlite3.Connection | None = None) -> list[str]:
    """Deterministic sweep: confirmations nobody answered in time are cancelled and the money stays with the
    student. Runs on demand (no background polling loop). Returns the expired txn_ids."""
    now = now or _now()
    expired = []
    with _use(conn) as c:
        due = [r["txn_id"] for r in c.execute(
            "SELECT txn_id FROM payment_states WHERE state = ? AND expires_at IS NOT NULL AND expires_at < ?",
            (_P.PENDING_CONFIRMATION.value, now.isoformat(timespec="seconds")))]
        for txn_id in due:
            update_payment_state(txn_id, Action.EXPIRE, note="no reply before the deadline; money returned",
                                 now=now, conn=c)
            expired.append(txn_id)
    return expired


def _res(r: sqlite3.Row) -> ResolutionRecord:
    return ResolutionRecord(record_id=f"RES-{r['id']:06d}", case_id=r["case_id"], txn_id=r["txn_id"],
                            action=r["action"], from_state=r["from_state"], to_state=r["to_state"], actor=r["actor"],
                            note=r["note"] or "", reasons=json.loads(r["reasons"] or "[]"),
                            committed_at=r["committed_at"])


def list_resolutions(limit: int = 500, *, conn: sqlite3.Connection | None = None) -> list[ResolutionRecord]:
    """Audit log, newest first."""
    with _use(conn) as c:
        return [_res(r) for r in c.execute("SELECT * FROM case_resolutions ORDER BY id DESC LIMIT ?", (limit,))]


def latest_resolution(txn_id: str, *, conn: sqlite3.Connection | None = None) -> ResolutionRecord | None:
    with _use(conn) as c:
        row = c.execute("SELECT * FROM case_resolutions WHERE txn_id = ? ORDER BY id DESC LIMIT 1",
                        (txn_id,)).fetchone()
    return _res(row) if row else None


# ---- Scam alert broadcasts (stretch) ---------------------------------------------------------
def broadcast_scam_alert(handle: str, text: str, approval: StaffApproval, *,
                         conn: sqlite3.Connection | None = None) -> int:
    """Staff-only: warn all students about a scam account. Returns the broadcast id."""
    from src.auth import AuthError, verify_token
    try:
        who = verify_token(approval.session_token)
    except AuthError as e:
        raise ApprovalRequired(f"Broadcasting needs a valid staff session ({e})") from None
    with _use(conn) as c:
        cur = c.execute("INSERT INTO scam_broadcasts (recipient_handle, text, staff_id, created_at) VALUES (?,?,?,?)",
                        (handle, text.strip()[:400], who, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        c.commit()
        return cur.lastrowid


def list_broadcasts(*, conn: sqlite3.Connection | None = None) -> list[dict]:
    with _use(conn) as c:
        return [dict(r) for r in c.execute("SELECT * FROM scam_broadcasts ORDER BY id DESC")]


# ---- Data intake (console form / grid / CSV) --------------------------------------
INTAKE_MAX_ROWS = 500
_INTAKE_FIELDS = set(NewTransaction.model_fields)


def list_students(*, conn: sqlite3.Connection | None = None) -> list[dict]:
    with _use(conn) as c:
        return [dict(r) for r in c.execute("SELECT student_id, full_name, grade FROM students ORDER BY student_id")]


def list_recipients(*, conn: sqlite3.Connection | None = None) -> list[dict]:
    with _use(conn) as c:
        return [dict(r) for r in c.execute("SELECT handle, display_name, kind, verified FROM recipients ORDER BY handle")]


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and v != v) or (isinstance(v, str) and not v.strip())


def validate_new_transactions(rows: list[dict], *, conn: sqlite3.Connection | None = None
                              ) -> tuple[list[NewTransaction], list[dict]]:
    """Validate raw rows (any source). Returns (valid items, errors [{row, error}]); never writes."""
    if len(rows) > INTAKE_MAX_ROWS:
        return [], [dict(row=0, error=f"Too many rows ({len(rows)}); the limit is {INTAKE_MAX_ROWS} per import")]
    with _use(conn) as c:
        students = {r[0] for r in c.execute("SELECT student_id FROM students")}
    valid, errors = [], []
    for i, raw in enumerate(rows, 1):
        data = {str(k).strip().lower(): v for k, v in raw.items() if k is not None}
        data = {k: v for k, v in data.items() if k in _INTAKE_FIELDS and not _blank(v)}
        try:
            item = NewTransaction(**data)
        except ValidationError as e:
            err = e.errors()[0]
            errors.append(dict(row=i, error=f"{'.'.join(map(str, err['loc'])) or 'row'}: {err['msg']}"))
            continue
        if item.student_id not in students:
            errors.append(dict(row=i, error=f"student_id: {item.student_id} does not exist"))
            continue
        valid.append(item)
    return valid, errors


def _next_id(c: sqlite3.Connection, table: str, col: str, prefix: str) -> int:
    row = c.execute(f"SELECT MAX(CAST(SUBSTR({col}, {len(prefix) + 1}) AS INTEGER)) FROM {table}").fetchone()
    return (row[0] or 0) + 1


def add_transactions(items: list[NewTransaction], entered_by: str, *,
                     conn: sqlite3.Connection | None = None) -> list[dict]:
    """Insert validated payments (+ their chat message) and a NEW alert for each, atomically.
    Ground truth stays NULL (unlabeled). Returns [{txn_id, alert_id}]."""
    import secrets

    out: list[dict] = []
    with _use(conn) as c:
        try:
            n_txn = _next_id(c, "transactions", "txn_id", "TXN-")
            n_alr = _next_id(c, "alerts", "alert_id", "ALR-")
            n_dev = _next_id(c, "devices", "device_id", "DEV-")
            n_lgn = _next_id(c, "device_logins", "login_id", "LGN-")
            n_msg = _next_id(c, "messages", "message_id", "MSG-")
            for it in items:
                ts = it.ts or _now()
                dev = None
                if it.device == "known":
                    dev = c.execute("SELECT device_id FROM devices WHERE student_id = ? AND trusted = 1 "
                                    "ORDER BY first_seen LIMIT 1", (it.student_id,)).fetchone()
                    dev = dev[0] if dev else None
                if dev is None:
                    dev = f"DEV-{n_dev:05d}"
                    n_dev += 1
                    c.execute("INSERT INTO devices VALUES (?,?,?,?,?,?,?)",
                              (dev, it.student_id, secrets.token_hex(8), "Unknown", ts.date().isoformat(),
                               ts.date().isoformat(), 0))
                c.execute("INSERT INTO device_logins VALUES (?,?,?,?,?,?)",
                          (f"LGN-{n_lgn:06d}", dev, it.student_id,
                           (ts - timedelta(minutes=2)).isoformat(timespec="seconds"), "AE", 1))
                n_lgn += 1
                msg_id = None
                if it.message:
                    msg_id = f"MSG-{n_msg:05d}"
                    n_msg += 1
                    link = re.search(r"(https?://\S+|\b[a-z0-9-]+\.(?:co|com|xyz|site|link|io|me|app|school)\S*)",
                                     it.message, re.I)
                    c.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
                              (msg_id, it.student_id, (ts - timedelta(minutes=5)).isoformat(timespec="seconds"),
                               it.recipient_handle, "Entered by staff", it.message,
                               link.group(0) if link else None, None))
                rec = c.execute("SELECT display_name FROM recipients WHERE handle = ?", (it.recipient_handle,)).fetchone()
                name = it.recipient_name or (rec[0] if rec else it.recipient_handle)
                txn_id, alert_id = f"TXN-{n_txn:05d}", f"ALR-{n_alr:05d}"
                n_txn += 1
                n_alr += 1
                c.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (txn_id, it.student_id, ts.isoformat(timespec="seconds"), it.txn_type, it.channel,
                           round(it.amount_aed, 2), dev, it.recipient_handle, name, msg_id, it.memo,
                           None, f"manual:{entered_by}", None, None))
                c.execute("INSERT INTO alerts VALUES (?,?,?,?,?)",
                          (alert_id, txn_id, "MANUAL_ENTRY", ts.isoformat(timespec="seconds"), "NEW"))
                out.append(dict(txn_id=txn_id, alert_id=alert_id))
            c.commit()
        except Exception:
            c.rollback()
            raise
    return out
