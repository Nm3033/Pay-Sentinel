"""Deterministic payment rules. PURE PYTHON: imports only config + schemas, never src.llm.

Three action boundaries (the brief's): CLEAR, HOLD_FOR_CONFIRMATION (student decides), ESCALATE (staff decide).
There is no freeze/block outcome, and missing data or low confidence can only ever cause a pause.
Every reason string cites the record ID it is based on, so it can go straight into the decision log.
"""
from __future__ import annotations

from src import config
from src.schemas import (GateDecision, MessageAssessment, MessageSignals, PairWindow, RecipientCheck, Route,
                         SanitizedMemo, SpreadStats, Transaction, TransactionContext)


def needs_guardian(txn_type: str, amount_aed: float, age: int | None) -> bool:
    """HS-1.5: students under GUARDIAN_AGE need a parent/guardian to approve peer transfers above the limit."""
    return (age is not None and age >= 0 and age < config.GUARDIAN_AGE and txn_type == "P2P"
            and amount_aed > config.GUARDIAN_LIMIT_AED)


def is_established_peer(rc: RecipientCheck | None) -> bool:
    r = rc.recipient if rc else None
    return (r is not None and r.kind == "PEER" and (rc.account_age_days or 0) >= config.NEW_ACCOUNT_DAYS
            and r.trust_score >= config.TRUST_MIN and r.scam_reports == 0)


def evaluate_escalation_gate(txn: Transaction, context: TransactionContext | None,
                             recipient: RecipientCheck | None, signals: MessageSignals | None,
                             memo: SanitizedMemo | None, chat: SanitizedMemo | None = None,
                             spread: SpreadStats | None = None, assessment: MessageAssessment | None = None,
                             evidence_errors: list[str] | None = None,
                             pair: PairWindow | None = None) -> GateDecision:
    """ESCALATE: reported recipient, phishing link + pressure, impersonation, exam leak, scam campaign,
                 hidden instructions, or an uncertain read (low AI confidence / unreadable slang).
    HOLD_FOR_CONFIRMATION: unknown/new/low-trust recipient, amount above the automatic limit, scam signals,
                 new device, many payments, or any check that couldn't run (fallback: pause, never block).
    CLEAR: none of the above (e.g. verified campus vendor, normal amount, clean message).
    Curveballs: lookalike of a verified shop => ESCALATE; split payments (rolling total to one recipient above the
    automatic limit) => HOLD; under-16 peer transfer above the guardian limit => HOLD + guardian approval.
    """
    tid = txn.record_id
    esc: list[str] = []
    hold: list[str] = [f"CHECK_UNAVAILABLE: {e} [{tid}]" for e in (evidence_errors or [])]
    sig = set(signals.signals) if signals else set()
    msg_ref = context.message.record_id if context and context.message else tid

    # ---- recipient (verify_campus_recipient) ---------------------------------------------------
    vendor = False
    if recipient is None:
        hold.append(f"VERIFICATION_UNAVAILABLE: recipient check did not answer [{tid}]")
    else:
        rec = recipient.recipient
        vendor = recipient.verified_vendor
        if recipient.lookalike_of:
            esc.append(f"LOOKALIKE_VENDOR: {recipient.handle} imitates the verified shop {recipient.lookalike_of} "
                       f"({recipient.lookalike_score:.0%} similar) [{rec.record_id if rec else tid}]")
        if not recipient.found:
            hold.append(f"UNKNOWN_RECIPIENT: {recipient.handle} is not a PocketPay account on record [{tid}]")
        else:
            rid = rec.record_id
            if rec.scam_reports >= config.REPORTS_ESCALATE:
                esc.append(f"REPORTED_RECIPIENT: {rec.scam_reports} scam reports [{rid}]")
            if not vendor:
                if recipient.account_age_days < config.NEW_ACCOUNT_DAYS:
                    hold.append(f"NEW_ACCOUNT: {recipient.account_age_days} days old [{rid}]")
                if rec.trust_score < config.TRUST_MIN:
                    hold.append(f"LOW_TRUST: trust score {rec.trust_score} [{rid}]")
        limit = config.VENDOR_NORMAL_MAX_AED if vendor else config.P2P_AUTO_MAX_AED
        if txn.amount_aed > limit:
            hold.append(f"LARGE_AMOUNT: {txn.amount_aed:,.2f} AED > {limit:,.0f} AED automatic limit [{tid}]")
        if (spread is not None and spread.distinct_students >= config.CAMPAIGN_MIN_STUDENTS and not vendor
                and not is_established_peer(recipient)):
            esc.append(f"SCAM_CAMPAIGN: reached {spread.distinct_students} students within a day [{spread.record_id}]")
        # HS-1.1 split payments: small pieces that add up past the limit within the window
        if pair is not None and pair.txn_count >= 2 and pair.total_aed > limit:
            hold.append(f"SPLIT_PAYMENTS: {pair.txn_count} payments to {pair.recipient_handle} total "
                        f"{pair.total_aed:,.2f} AED in {config.SPLIT_WINDOW_MINUTES} min (limit {limit:,.0f}) "
                        f"[{pair.record_id}]")

    # ---- message & note (slang scanner + sanitizer) ------------------------------------------
    if "PHISHING_LINK" in sig and sig & {"URGENCY", "CLICK_BAIT", "IMPERSONATION"}:
        esc.append(f"PHISHING_PATTERN: unofficial link + pressure [{msg_ref}]")
    if "IMPERSONATION" in sig:
        esc.append(f"IMPERSONATION: pretends to be PocketPay or the school [{msg_ref}]")
    if "EXAM_LEAK" in sig:
        esc.append(f"EXAM_LEAK: offers leaked exam material [{msg_ref}]")
    soft = sorted(sig - {"PHISHING_LINK", "IMPERSONATION", "EXAM_LEAK"})
    if soft or ("PHISHING_LINK" in sig and not esc):
        hold.append(f"SCAM_SIGNALS: {', '.join(soft or ['PHISHING_LINK'])} [{msg_ref}]")
    for label, s in (("payment note", memo), ("chat message", chat)):
        if s is None:
            continue
        if s.injection_detected:
            esc.append(f"HIDDEN_INSTRUCTIONS: in the {label} ({', '.join(s.matched_rules)}) [{tid}]")
        elif s.agent_verdict == "UNAVAILABLE":
            hold.append(f"SAFETY_CHECK_UNAVAILABLE: AI check of the {label} didn't run [{tid}]")

    # ---- uncertainty: flag, never guess ---------------------------------------------------------
    # Uncertain if EITHER side is unsure. A confident AI can never cancel the code's doubt (AI only adds caution).
    unclear = signals.unclear_terms if signals else []
    uncertain = False
    if len(unclear) >= config.UNCLEAR_TERMS_MIN:
        uncertain = True
        esc.append(f"LOW_CONFIDENCE: the rules can't interpret this slang ({', '.join(unclear[:4])}) [{msg_ref}]")
    if assessment is not None and (assessment.confidence < config.CONFIDENCE_MIN
                                   or len(assessment.unclear_terms) >= config.UNCLEAR_TERMS_MIN):
        uncertain = True
        esc.append(f"LOW_CONFIDENCE: AI is only {assessment.confidence:.0%} sure"
                   + (f" and can't read: {', '.join(assessment.unclear_terms[:4])}" if assessment.unclear_terms else "")
                   + f" [{msg_ref}]")

    # ---- student's device & behaviour (get_transaction_context) ----------------------------------
    if context is not None:
        dev = next((d for d in context.devices if d.record_id == txn.device_id), None)
        if dev is None:
            hold.append(f"UNKNOWN_DEVICE {txn.device_id} [{tid}]")
        else:
            age = (txn.ts.date() - dev.first_seen).days
            if not dev.trusted or age < config.NEW_DEVICE_DAYS:
                hold.append(f"NEW_DEVICE: {age} days old{'' if dev.trusted else ', untrusted'} [{dev.record_id}]")
        if context.velocity.txn_count > config.VELOCITY_MAX_24H and not vendor:   # canteen runs aren't a risk
            hold.append(f"MANY_PAYMENTS: {context.velocity.txn_count} in 24h [{context.velocity.record_id}]")

        # HS-1.5 guardian approval: age read from the dataset, rule decided here
        age = context.student.age_on(txn.ts.date())
        if needs_guardian(txn.txn_type, txn.amount_aed, age):
            hold.append(f"GUARDIAN_REQUIRED: student is {age}; peer transfers above {config.GUARDIAN_LIMIT_AED:,.0f} "
                        f"AED need a guardian's approval [{context.student.record_id}]")
    guardian = any(r.startswith("GUARDIAN_REQUIRED") for r in hold)

    if esc:
        return GateDecision(route=Route.ESCALATE, reasons=esc + hold, requires_human=True, awaiting="STAFF",
                            uncertain=uncertain, approvals=["STAFF"] + (["GUARDIAN"] if guardian else []))
    if hold:
        return GateDecision(route=Route.HOLD_FOR_CONFIRMATION, reasons=hold, requires_human=True,
                            awaiting="STUDENT", approvals=["STUDENT"] + (["GUARDIAN"] if guardian else []))
    who = (f"verified campus vendor [{recipient.recipient.record_id}]" if vendor
           else f"established classmate [{recipient.recipient.record_id}]")
    return GateDecision(route=Route.CLEAR, requires_human=False, awaiting="NONE", reasons=[
        f"ALL_CLEAR: {who}; normal amount {txn.amount_aed:,.2f} AED; no scam signals [{tid}]"])


def route(gate_route: Route, llm_route: Route | None) -> Route:
    """The AI may raise the level of caution, never lower it."""
    if llm_route is None:
        return gate_route
    return max(gate_route, llm_route, key=lambda r: r.severity)


# ---- plain-language reasons (for the UI trace and the student explanation) ------------------------------
REASON_TEXT = {
    "REPORTED_RECIPIENT": "other students reported this account as a scam",
    "UNKNOWN_RECIPIENT": "the receiver isn't a known PocketPay account",
    "NEW_ACCOUNT": "the receiver's account is brand new", "LOW_TRUST": "the receiver has a low trust score",
    "LARGE_AMOUNT": "the amount is above the automatic limit",
    "SCAM_CAMPAIGN": "this account messaged or charged many students at once",
    "PHISHING_PATTERN": "an unofficial link plus pressure to act fast", "IMPERSONATION": "it pretends to be PocketPay or the school",
    "EXAM_LEAK": "it offers leaked exam material", "SCAM_SIGNALS": "the message has common scam signs",
    "HIDDEN_INSTRUCTIONS": "hidden instructions aimed at the AI", "SAFETY_CHECK_UNAVAILABLE": "an AI safety check couldn't run",
    "LOW_CONFIDENCE": "the message is too unclear to judge safely",
    "UNKNOWN_DEVICE": "an unknown phone was used", "NEW_DEVICE": "a new phone was used",
    "MANY_PAYMENTS": "unusually many payments today", "VERIFICATION_UNAVAILABLE": "the recipient check timed out",
    "CHECK_UNAVAILABLE": "a check couldn't run",
    "LOOKALIKE_VENDOR": "the name imitates a verified campus shop",
    "SPLIT_PAYMENTS": "several small payments to the same person add up past the limit",
    "GUARDIAN_REQUIRED": "a parent or guardian must approve this transfer",
}


def plain_reasons(reasons: list[str]) -> list[str]:
    """Coded gate reasons -> short phrases, de-duplicated, in order."""
    out: list[str] = []
    for r in reasons:
        code = r.split(":")[0].split(" [")[0].split(" ")[0]
        text = REASON_TEXT.get(code, code.replace("_", " ").lower())
        if text not in out:
            out.append(text)
    return out
