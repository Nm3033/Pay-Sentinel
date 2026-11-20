"""Pydantic v2 contracts shared by services, guards, agents and UI (PocketPay campus wallet).

Every model forbids unknown fields, so evaluation-only columns (ground_truth, scenario, ...)
and unexpected LLM keys fail validation instead of leaking through silently.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- Enums -------------------------------------------------------------------
class Route(str, Enum):
    """The brief's three action boundaries, in order of seriousness. There is NO freeze/block route."""
    CLEAR = "CLEAR"                                  # safe: payment goes through
    HOLD_FOR_CONFIRMATION = "HOLD_FOR_CONFIRMATION"  # paused until the student confirms or cancels
    ESCALATE = "ESCALATE"                            # paused and sent to campus support staff

    @property
    def severity(self) -> int:
        return _ROUTE_ORDER.index(self.value)


_ROUTE_ORDER = ["CLEAR", "HOLD_FOR_CONFIRMATION", "ESCALATE"]


class PaymentState(str, Enum):
    CLEARED = "CLEARED"                          # sent automatically (terminal)
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"  # waiting for the student's reply
    PENDING_GUARDIAN = "PENDING_GUARDIAN"        # under-16 peer transfer: waiting for a parent/guardian
    CONFIRMED_CANCELLABLE = "CONFIRMED_CANCELLABLE"  # confirmed; the student may still cancel inside the window
    ESCALATED = "ESCALATED"                      # waiting for support staff
    SENT = "SENT"                                # sent after support staff released it (terminal)
    RELEASED = "RELEASED"                        # sent after the cancel window closed (terminal)
    CANCELLED = "CANCELLED"                      # transfer cancelled, money stays with the student (terminal)
    EXPIRED = "EXPIRED"                          # nobody answered in time: cancelled, money returned (terminal)


TERMINAL_STATES = {PaymentState.CLEARED, PaymentState.SENT, PaymentState.RELEASED, PaymentState.CANCELLED,
                   PaymentState.EXPIRED}


class Action(str, Enum):
    """Every allowed state change. Freezing a wallet is deliberately NOT an action."""
    CLEAR = "CLEAR"
    HOLD_FOR_CONFIRMATION = "HOLD_FOR_CONFIRMATION"
    ESCALATE = "ESCALATE"
    REMIND = "REMIND"                        # one reminder to the student (capped)
    STUDENT_CONFIRM = "STUDENT_CONFIRM"      # "I know this seller" -> cancel window (or guardian, if needed)
    STUDENT_CANCEL = "STUDENT_CANCEL"        # "Cancel transfer"
    STUDENT_REPORT = "STUDENT_REPORT"        # "This is a scam" -> staff
    STAFF_RELEASE = "STAFF_RELEASE"          # staff checked it: send
    STAFF_CANCEL = "STAFF_CANCEL"            # staff: cancel this transfer (wallet stays usable)
    STAFF_ASK_STUDENT = "STAFF_ASK_STUDENT"  # staff hands it back to the student to confirm
    EXPIRE = "EXPIRE"                        # no reply before the deadline
    RELEASE = "RELEASE"                      # cancel window closed -> money sent
    GUARDIAN_APPROVE = "GUARDIAN_APPROVE"    # parent/guardian approves an under-16 transfer
    GUARDIAN_DECLINE = "GUARDIAN_DECLINE"    # parent/guardian declines it
    INVALID_REQUEST = "INVALID_REQUEST"      # LOG ONLY: an attempted action that is not allowed (never changes state)


# ---- Source records (each carries a citable record_id) ------------------------
class Alert(Strict):
    record_id: str = Field(pattern=r"^ALR-\d{5}$")
    txn_id: str
    legacy_rule: str          # why the old keyword/rule system flagged it
    created_at: datetime
    status: str


class Transaction(Strict):
    record_id: str = Field(pattern=r"^TXN-\d{5}$")
    student_id: str
    ts: datetime
    txn_type: Literal["PURCHASE", "P2P"]
    channel: Literal["APP", "QR", "LINK"]
    amount_aed: float = Field(ge=0)
    device_id: str | None
    recipient_handle: str
    recipient_name: str
    message_id: str | None = None   # chat message that led to this payment, if any
    memo: str = ""                  # RAW, UNTRUSTED payment note. Only the sanitizer may read it.


class StudentProfile(Strict):
    record_id: str = Field(pattern=r"^STU-\d{4}$")
    full_name: str
    grade: str
    joined_on: date
    usual_daily_spend_aed: float
    date_of_birth: date
    guardian_id: str
    guardian_name: str

    def age_on(self, day: date) -> int:
        return age_on(self.date_of_birth, day)


def age_on(dob: date, day: date) -> int:
    return day.year - dob.year - ((day.month, day.day) < (dob.month, dob.day))


class Device(Strict):
    record_id: str = Field(pattern=r"^DEV-\d{5}$")
    student_id: str
    fingerprint: str
    os: str
    first_seen: date
    last_seen: date
    trusted: bool


class DeviceLogin(Strict):
    record_id: str = Field(pattern=r"^LGN-\d{6}$")
    device_id: str
    ts: datetime
    ip_country: str
    success: bool


class ChatMessage(Strict):
    record_id: str = Field(pattern=r"^MSG-\d{5}$")
    student_id: str
    ts: datetime
    sender_handle: str
    platform: str
    text: str                       # RAW, UNTRUSTED. Only the sanitizer and the assessor may read it.
    link_url: str | None = None


class VelocityStats(Strict):
    record_id: str = Field(pattern=r"^VEL-")
    student_id: str
    window_start: datetime
    window_end: datetime
    txn_count: int
    total_aed: float
    distinct_recipients: int
    txn_ids: list[str]


class TransactionContext(Strict):
    """Output of get_transaction_context(): everything the bank knows about this payment."""
    transaction: Transaction
    student: StudentProfile
    devices: list[Device]
    recent_logins: list[DeviceLogin]
    message: ChatMessage | None
    velocity: VelocityStats

    def record_ids(self) -> set[str]:
        ids = {self.transaction.record_id, self.student.record_id, self.velocity.record_id,
               *self.velocity.txn_ids, *(d.record_id for d in self.devices),
               *(l.record_id for l in self.recent_logins)}
        if self.message:
            ids.add(self.message.record_id)
        return ids


class Recipient(Strict):
    record_id: str = Field(pattern=r"^RCP-\d{4}$")
    handle: str
    display_name: str
    kind: Literal["VENDOR", "PEER"]
    verified: bool               # verified campus vendor (canteen, bookstore...)
    created_on: date
    trust_score: int = Field(ge=0, le=100)
    scam_reports: int = Field(ge=0)
    status: Literal["ACTIVE", "FLAGGED"]


class RecipientCheck(Strict):
    """Output of verify_campus_recipient()."""
    handle: str
    found: bool
    recipient: Recipient | None
    account_age_days: int | None
    verified_vendor: bool
    lookalike_of: str | None = None       # verified shop handle this one imitates (close but not exact)
    lookalike_score: float | None = None

    def record_ids(self) -> set[str]:
        return {self.recipient.record_id} if self.recipient else set()


class SpreadStats(Strict):
    """How many different students one recipient reached (paid or messaged) within +/- 24h."""
    record_id: str = Field(pattern=r"^SPR-")
    recipient_handle: str
    window_start: datetime
    window_end: datetime
    distinct_students: int
    student_ids: list[str]
    txn_ids: list[str]
    message_ids: list[str]


class PairWindow(Strict):
    """Rolling total of one student's transfers to one recipient (split-payment check)."""
    record_id: str = Field(pattern=r"^PAIR-")
    student_id: str
    recipient_handle: str
    window_start: datetime
    window_end: datetime
    txn_count: int
    total_aed: float
    txn_ids: list[str]


# ---- Guard / agent / gate outputs ---------------------------------------------
class SanitizerVerdict(Strict):
    """Secondary Sanitizer Agent output: a classification only, never an action."""
    is_injection: bool
    reason: str = ""


class SanitizedMemo(Strict):
    original: str
    clean_text: str
    injection_detected: bool
    matched_rules: list[str] = []
    agent_verdict: Literal["CLEAN", "INJECTION", "UNAVAILABLE", "SKIPPED"] = "SKIPPED"
    agent_reason: str = ""


class MessageSignals(Strict):
    """Deterministic scam/slang scan of the chat message + payment note (src/slang.py)."""
    signals: list[str] = []          # e.g. URGENCY, PHISHING_LINK, IMPERSONATION, TOO_GOOD, PRESSURE
    unclear_terms: list[str] = []    # slang the code can't interpret
    links: list[str] = []


class MessageAssessment(Strict):
    """AI read of the chat. Advisory only: code decides the route; low confidence => escalate."""
    scam_risk: Literal["LOW", "MEDIUM", "HIGH"]
    confidence: float = Field(ge=0, le=1)
    signals: list[str] = []
    unclear_terms: list[str] = []
    summary: str = ""


class GateDecision(Strict):
    route: Route
    reasons: list[str]
    requires_human: bool             # True for HOLD and ESCALATE
    awaiting: Literal["NONE", "STUDENT", "STAFF"] = "NONE"
    uncertain: bool = False          # low-confidence read: escalated instead of guessing
    approvals: list[str] = []        # who must approve before money moves, decided by code (e.g. STUDENT, GUARDIAN)


class Finding(Strict):
    claim: str = Field(min_length=1)
    citations: list[str] = Field(min_length=1)


class InvestigatorOutput(Strict):
    """AI case writer output: cited findings, a student-friendly explanation and a staff summary."""
    findings: list[Finding]
    student_explanation: str
    staff_summary: str
    suggested_route: Route


class StaffApproval(Strict):
    staff_id: str = Field(min_length=1)
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    note: str = ""
    # Signed session token from src.auth.login(); verified before any staff action.
    # Excluded from serialization so it never lands in case JSON, logs or reports.
    session_token: str = Field(default="", exclude=True, repr=False)


class PaymentStateRecord(Strict):
    """Stateful memory for one payment (table payment_states). Waiting is a stored state, never a loop."""
    txn_id: str
    state: PaymentState
    awaiting: Literal["NONE", "STUDENT", "STAFF", "GUARDIAN"]
    prompts_sent: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    last_action: Action
    last_actor: str
    history: list[dict] = []


class ResolutionRecord(Strict):
    """One audit-log row: a state change, who made it and why."""
    record_id: str = Field(pattern=r"^RES-\d{6}$")
    case_id: str
    txn_id: str
    action: Action
    from_state: PaymentState | None
    to_state: PaymentState | None    # == from_state when the attempt was rejected
    actor: str
    note: str
    reasons: list[str]
    committed_at: datetime


class CaseFile(Strict):
    case_id: str
    alert: Alert
    transaction: Transaction
    memo: SanitizedMemo | None = None        # payment note, sanitized
    chat: SanitizedMemo | None = None        # chat message, sanitized
    signals: MessageSignals | None = None
    assessment: MessageAssessment | None = None
    context: TransactionContext | None = None
    recipient: RecipientCheck | None = None
    spread: SpreadStats | None = None
    pair: PairWindow | None = None
    gate: GateDecision | None = None
    investigation: InvestigatorOutput | None = None
    dropped_findings: list[Finding] = []
    final_route: Route | None = None
    state: PaymentState | None = None
    approval: StaffApproval | None = None
    resolution: ResolutionRecord | None = None
    report_markdown: str | None = None
    errors: list[str] = []
    trace: list[str] = []          # plain-language decision log for the UI / audit trail
    llm_usage: list[dict] = []     # per-call tokens & latency (telemetry)


class NewTransaction(Strict):
    """A flagged payment entered from the console (form, grid or CSV). Chat text and note are stored RAW
    and treated as untrusted like any other: they are screened when the case is triaged."""
    student_id: str = Field(pattern=r"^STU-\d{4}$")
    amount_aed: float = Field(gt=0, le=5_000)
    txn_type: Literal["PURCHASE", "P2P"] = "P2P"
    channel: Literal["APP", "QR", "LINK"] = "APP"
    recipient_handle: str = Field(pattern=r"^@[a-z0-9_.\-]{3,30}$")
    recipient_name: str = Field(default="", max_length=80)
    message: str = Field(default="", max_length=500)   # the chat message the student received
    memo: str = Field(default="", max_length=200)      # payment note
    device: Literal["known", "new"] = "known"
    ts: datetime | None = None                          # default: now

    @field_validator("student_id", "txn_type", "channel", mode="before")
    @classmethod
    def _upper(cls, v):
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("recipient_handle", mode="before")
    @classmethod
    def _handle(cls, v):
        if not isinstance(v, str):
            return v
        v = "".join(v.split()).lower()
        return v if v.startswith("@") else "@" + v

    @field_validator("device", mode="before")
    @classmethod
    def _device(cls, v):
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("amount_aed", mode="before")
    @classmethod
    def _amount(cls, v):
        return v.replace(",", "").replace("AED", "").strip() if isinstance(v, str) else v

    @field_validator("recipient_name", "message", "memo", mode="before")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("ts", mode="after")
    @classmethod
    def _naive(cls, v):
        return v.replace(tzinfo=None, microsecond=0) if v else v
