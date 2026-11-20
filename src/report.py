"""Render templates/decision_log_template.md from a CaseFile (pure code; no LLM)."""
from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from string import Template

from src import config
from src.schemas import CaseFile


def _cell(s: object) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ").strip()


def visible_text(s: str) -> str:
    """Public alias used by the UI to display quarantined text."""
    return _visible(s)


def _visible(s: str) -> str:
    """Show invisible/format chars explicitly and defuse code fences (quarantined display)."""
    out = "".join(f"<U+{ord(ch):04X}>" if unicodedata.category(ch) == "Cf" else ch for ch in (s or ""))
    return out.replace("```", "'''") or "(none)"


def _transaction(c: CaseFile) -> str:
    t = c.transaction
    return "\n".join([
        f"- When: {t.ts:%Y-%m-%d %H:%M} [{t.record_id}]",
        f"- What: {'purchase' if t.txn_type == 'PURCHASE' else 'transfer to another student'} via {t.channel} "
        f"[{t.record_id}]",
        f"- Amount: **{t.amount_aed:,.2f} AED** [{t.record_id}]",
        f"- To: {t.recipient_name} ({t.recipient_handle}) [{t.record_id}]",
        f"- From: {t.student_id}, device {t.device_id or 'n/a'} [{t.record_id}]",
    ])


def _context(c: CaseFile) -> str:
    if not c.context:
        return "_Unavailable: the payment paused for a human instead of guessing._"
    x, t = c.context, c.transaction
    rows = [f"- Student: {x.student.full_name}, {x.student.grade}, PocketPay since {x.student.joined_on} "
            f"[{x.student.record_id}]",
            f"- Payments in the last 24h: {x.velocity.txn_count}, total {x.velocity.total_aed:,.2f} AED "
            f"[{x.velocity.record_id}]"]
    for d in x.devices:
        mark = " ← used for this payment" if d.record_id == t.device_id else ""
        rows.append(f"- Device {d.record_id}: {_cell(d.os)}, first seen {d.first_seen}, trusted {d.trusted}{mark}")
    if x.message:
        m = x.message
        rows.append(f"- Chat message from {m.sender_handle} on {m.platform} at {m.ts:%H:%M} [{m.record_id}]")
    return "\n".join(rows)


def _recipient(c: CaseFile) -> str:
    rc = c.recipient
    if rc is None:
        return "_The recipient check timed out. Fallback: the payment was paused (never blocked)._"
    if not rc.found:
        return f"{rc.handle} is **not a known PocketPay account**."
    r = rc.recipient
    kind = "Verified campus vendor" if rc.verified_vendor else "Student account"
    return (f"- {kind}: **{r.display_name}** ({r.handle}) [{r.record_id}]\n"
            f"- Account age: {rc.account_age_days} days · trust score {r.trust_score}/100 · scam reports "
            f"{r.scam_reports} · status {r.status} [{r.record_id}]")


def _spread(c: CaseFile) -> str:
    s = c.spread
    if s is None:
        return "_Not applicable (purchase from a vendor)._"
    return (f"- {s.distinct_students} different student(s) paid or were messaged by {s.recipient_handle} within "
            f"±24h [{s.record_id}]")


def render_report(c: CaseFile) -> str:
    inv, gate, chat, memo = c.investigation, c.gate, c.chat, c.memo
    findings = ("\n".join(f"| {i} | {_cell(f.claim)} | {', '.join(f.citations)} |"
                          for i, f in enumerate(inv.findings, 1))
                if inv and inv.findings else "| - | _No AI findings (AI off, not needed, or unavailable)_ | - |")
    dropped = (f"{len(c.dropped_findings)}: " + "; ".join(_cell(f.claim) for f in c.dropped_findings)
               if c.dropped_findings else "none")
    rules = sorted({r for s in (chat, memo) if s for r in s.matched_rules})
    a = c.assessment
    ai_reading = (f"{a.scam_risk} risk, {a.confidence:.0%} confident. {_cell(a.summary)}" if a else "not used")
    audit = [f"- {_cell(s)}" for s in c.trace]
    if c.llm_usage:
        audit.append(f"- AI calls: {len(c.llm_usage)}, tokens in/out "
                     f"{sum(u['prompt_tokens'] for u in c.llm_usage)}/{sum(u['completion_tokens'] for u in c.llm_usage)}")
    audit += [f"- Check error: {_cell(e)}" for e in c.errors]
    res = c.resolution
    human = res if res and (res.actor.startswith("staff:") or res.actor.startswith("student:")) else None
    values = dict(
        state=c.state.value if c.state else "not recorded", case_id=c.case_id, alert_id=c.alert.record_id,
        legacy_rule=c.alert.legacy_rule, generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        final_route=c.final_route.value if c.final_route else "n/a",
        awaiting={"STUDENT": "the student", "STAFF": "support staff"}.get(gate.awaiting if gate else "", "nobody"),
        student_explanation=inv.student_explanation if inv else "-",
        txn_record_id=c.transaction.record_id, transaction_block=_transaction(c),
        injection_detected="YES" if any(s and s.injection_detected for s in (chat, memo)) else "NO",
        matched_rules=", ".join(rules) or "none",
        scam_signals=", ".join(c.signals.signals) if c.signals and c.signals.signals else "none",
        unclear_terms=", ".join(c.signals.unclear_terms) if c.signals and c.signals.unclear_terms else "none",
        ai_reading=ai_reading,
        message_original=_visible(chat.original) if chat else "(no chat message)",
        message_clean=_visible(chat.clean_text) if chat else "(no chat message)",
        note_original=_cell(_visible(memo.original)) if memo else "(none)",
        context_block=_context(c), recipient_block=_recipient(c), spread_block=_spread(c),
        gate_reasons="\n".join(f"- {_cell(r)}" for r in gate.reasons) if gate else "_Not evaluated._",
        findings_rows=findings, dropped_claims=dropped,
        staff_summary=_cell(inv.staff_summary) if inv else "-",
        human_actor=human.actor if human else "-", human_action=human.action.value if human else "-",
        human_ts=f"{human.committed_at:%Y-%m-%d %H:%M:%S}" if human else "-",
        human_note=_cell(human.note) if human and human.note else "-",
        audit_trail="\n".join(audit) or "-",
    )
    return Template(config.TEMPLATE_PATH.read_text(encoding="utf-8")).safe_substitute(values)


def write_report(c: CaseFile, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or config.REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"DECISION_LOG_{c.transaction.record_id}.md"
    path.write_text(c.report_markdown or render_report(c), encoding="utf-8")
    return path
