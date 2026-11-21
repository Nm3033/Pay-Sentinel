"""One payment's journey from trigger to outcome (Round 3, Area 1: end-to-end workflow).

Pure: it only READS the alert, the transaction, the saved case file and the stored state (with its history) and
turns them into five plain-language steps plus the notice the student gets. It never changes a payment; all state
changes still go through services.update_payment_state(). It reads no evaluation-only column."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from src import config
from src.gate import plain_reasons
from src.schemas import TERMINAL_STATES, Alert, CaseFile, PaymentState, PaymentStateRecord, Route, Transaction

StepStatus = Literal["done", "current", "todo", "skipped"]
Waiting = Literal["NONE", "CHECK", "STUDENT", "GUARDIAN", "STAFF"]


class JourneyStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Literal["trigger", "checked", "person", "decided", "output"]
    title: str
    status: StepStatus
    detail: str
    at: datetime | None = None


class Journey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alert_id: str
    txn_id: str
    student_id: str
    steps: list[JourneyStep]
    waiting_for: Waiting          # who must act next ("NONE" once finished, "CHECK" before the first check)
    finished: bool                # a terminal state was reached
    notice: str                   # what the student is told right now: the output they can act on
    next_step: str                # one sentence: what happens next / what to do


_ROUTE_TEXT = {Route.CLEAR: "safe everyday payment, sent automatically",
               Route.HOLD_FOR_CONFIRMATION: "paused: the student must confirm they know the seller",
               Route.ESCALATE: "paused and sent to the campus support team"}
_ACTION_TEXT = {"STUDENT_CONFIRM": "confirmed they know the seller", "STUDENT_CANCEL": "cancelled the transfer",
                "STUDENT_REPORT": "reported it as a scam", "STAFF_RELEASE": "released it",
                "STAFF_CANCEL": "cancelled it", "STAFF_ASK_STUDENT": "asked the student to confirm",
                "GUARDIAN_APPROVE": "approved it", "GUARDIAN_DECLINE": "declined it",
                "EXPIRE": "nobody answered before the deadline", "RELEASE": "the cancel window closed",
                "REMIND": "sent the student a reminder"}
_WAITING_TEXT = {"STUDENT": "the student", "GUARDIAN": "the student's parent/guardian", "STAFF": "campus support staff"}


def who(actor: str) -> str:
    """'student:STU-0004' -> 'Student STU-0004'; 'system' -> 'Pay Sentinel (automatic)'."""
    kind, _, ident = (actor or "").partition(":")
    return {"student": f"Student {ident}", "guardian": f"Parent/guardian {ident}", "staff": f"Support staff {ident}",
            "system": "Pay Sentinel (automatic)"}.get(kind, actor or "unknown")


def _t(dt: datetime | None) -> str:
    return f"{dt:%d %b %H:%M}" if dt else ""


def student_notice(txn: Transaction, state: PaymentState | None, rec: PaymentStateRecord | None = None,
                   case: CaseFile | None = None) -> str:
    """The message the student sees for the payment's current state (plain words, actionable)."""
    pay = f"your {txn.amount_aed:,.2f} AED payment to {txn.recipient_handle}"
    waiting_text = case.investigation.student_explanation if case and case.investigation else ""
    if state is None:
        return ""
    if state == PaymentState.CLEARED:
        return f"Done: {pay} went through."
    if state == PaymentState.PENDING_CONFIRMATION:
        return waiting_text or (f"We paused {pay}. Do you know this seller? Open PocketPay and confirm or cancel. "
                                "Your wallet keeps working.")
    if state == PaymentState.ESCALATED:
        return waiting_text or (f"We paused {pay} and asked the campus support team to check it. Your wallet keeps "
                                "working, and you can still cancel the transfer.")
    if state == PaymentState.PENDING_GUARDIAN:
        return (f"Thanks for confirming. Because you are under {config.GUARDIAN_AGE}, your parent/guardian must approve "
                f"{pay} before it is sent.")
    if state == PaymentState.CONFIRMED_CANCELLABLE:
        when = f" at {rec.expires_at:%H:%M:%S}" if rec and rec.expires_at else " in a moment"
        return f"Confirmed: {pay} will be sent{when}. Changed your mind? Tap “Wait, cancel it!” before then."
    if state == PaymentState.SENT:
        return f"Done: the support team checked {pay} and sent it."
    if state == PaymentState.RELEASED:
        return f"Done: {pay} has been sent."
    if state == PaymentState.CANCELLED:
        by = who(rec.last_actor) if rec else ""
        extra = " The support team stopped it to protect you." if rec and rec.last_actor.startswith("staff:") else ""
        return f"Stopped: {pay} was cancelled{' by ' + by if by and 'Student' not in by else ''}. " \
               f"The money is still in your wallet.{extra}"
    if state == PaymentState.EXPIRED:
        return f"Stopped: nobody confirmed {pay} in time, so it was cancelled and the money is back in your wallet."
    return ""


def build_journey(alert: Alert, txn: Transaction, case: CaseFile | None, rec: PaymentStateRecord | None) -> Journey:
    """Five steps: payment flagged → checked → a person answers (if needed) → decided → student told + report."""
    state = rec.state if rec else (case.state if case else None)
    history = rec.history if rec else []
    human = [h for h in history if not str(h.get("actor", "")).startswith("system")]
    awaiting = rec.awaiting if rec else "NONE"
    finished = state in TERMINAL_STATES

    source = ("Entered on the Add payments page (console)" if alert.legacy_rule == "MANUAL_ENTRY"
              else f"Flagged by PocketPay's old rule {alert.legacy_rule}")
    steps = [JourneyStep(key="trigger", title="Payment flagged", status="done", at=alert.created_at,
                         detail=f"{source}: {txn.amount_aed:,.2f} AED from {txn.student_id} to {txn.recipient_handle}.")]

    if (case is None or case.gate is None) and rec is not None:   # state stored, detailed case file unavailable
        steps.append(JourneyStep(key="checked", title="Checked", status="done", at=rec.created_at,
                                 detail="Checked by the fixed rules (the detailed case file isn't available here)."))
    elif case is None or case.gate is None:
        steps.append(JourneyStep(key="checked", title="Checked", status="current",
                                 detail="Not checked yet. Press Check payment to run every step."))
    else:
        how = "fixed rules and AI" if case.llm_usage else "fixed rules"
        why = "" if case.final_route == Route.CLEAR else f" Why: {', '.join(plain_reasons(case.gate.reasons)[:2])}."
        steps.append(JourneyStep(key="checked", title="Checked", status="done", at=case.checked_at,
                                 detail=f"By {how}: {_ROUTE_TEXT[case.final_route]}.{why}"))

    if state is None:
        steps.append(JourneyStep(key="person", title="A person answers", status="todo", detail="Only if needed."))
    elif state == PaymentState.CLEARED:
        steps.append(JourneyStep(key="person", title="A person answers", status="skipped",
                                 detail="Not needed: nobody had to check this payment."))
    else:
        said = [f"{who(h['actor'])} {_ACTION_TEXT.get(h['action'], h['action'].lower())} "
                f"({datetime.fromisoformat(h['at']):%H:%M})" for h in human]
        timed_out = [h for h in history if h.get("action") == "EXPIRE"]
        if awaiting in _WAITING_TEXT:
            due = f", answer needed by {_t(rec.expires_at)}" if rec and rec.expires_at else ""
            steps.append(JourneyStep(key="person", title="A person answers", status="current",
                                     at=rec.updated_at if rec else None,
                                     detail="; ".join(said + [f"Waiting for {_WAITING_TEXT[awaiting]}{due}"]) + "."))
        else:
            last = human[-1] if human else (timed_out[-1] if timed_out else None)
            steps.append(JourneyStep(key="person", title="A person answers", status="done",
                                     at=datetime.fromisoformat(last["at"]) if last else None,
                                     detail=("; ".join(said) or "Nobody answered before the deadline") + "."))

    final_text = {PaymentState.CLEARED: "Sent automatically.",
                  PaymentState.SENT: "Sent after support staff checked it.",
                  PaymentState.RELEASED: f"Sent after the {config.CANCEL_WINDOW_SECONDS // 60}-minute cancel window closed.",
                  PaymentState.CANCELLED: "Cancelled: the money stays in the student's wallet.",
                  PaymentState.EXPIRED: "Nobody answered in time: cancelled, money returned."}
    if finished:
        steps.append(JourneyStep(key="decided", title="Decided", status="done", detail=final_text[state],
                                 at=datetime.fromisoformat(history[-1]["at"]) if history else None))
    elif state == PaymentState.CONFIRMED_CANCELLABLE:
        steps.append(JourneyStep(key="decided", title="Decided", status="current", at=rec.updated_at if rec else None,
                                 detail=f"Confirmed; sends at {rec.expires_at:%H:%M:%S} unless the student cancels."
                                 if rec and rec.expires_at else "Confirmed; inside the cancel window."))
    else:
        steps.append(JourneyStep(key="decided", title="Decided", status="todo", detail="After the person answers."))

    notice = student_notice(txn, state, rec, case)
    report = bool(case and case.report_markdown)
    if finished:
        steps.append(JourneyStep(key="output", title="Student told + report", status="done",
                                 detail="The student got the outcome below; the full decision report is ready"
                                        + (" to download." if report else ".")))
    else:
        steps.append(JourneyStep(key="output", title="Student told + report", status="todo",   # one "current" step
                                 detail="Comes at the end." if state is None
                                 else "At the end. Meanwhile the student already sees the message below."))

    if (case is None or case.gate is None) and rec is None:
        waiting: Waiting = "CHECK"
        nxt = "Check the payment to start the journey."
    elif finished:
        waiting, nxt = "NONE", "Finished. Nothing else can change this payment; every step is in the activity log."
    elif state == PaymentState.CONFIRMED_CANCELLABLE:
        waiting, nxt = "STUDENT", "The student can still tap “Wait, cancel it!” until the timer runs out."
    else:
        waiting = awaiting if awaiting in _WAITING_TEXT else "NONE"
        nxt = f"Next: {_WAITING_TEXT.get(awaiting, 'nobody')} must answer." + (
            f" If nobody does by {_t(rec.expires_at)}, it is cancelled and the money returned."
            if rec and rec.expires_at and awaiting in ("STUDENT", "GUARDIAN") else "")
    return Journey(alert_id=alert.record_id, txn_id=txn.record_id, student_id=txn.student_id, steps=steps,
                   waiting_for=waiting, finished=finished, notice=notice, next_step=nxt)
