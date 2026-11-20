"""Batch-wide scam campaign detection (pure Python over SQLite; no LLM).

A campaign = one unverified, non-established account that messaged or was paid by >= CAMPAIGN_MIN_STUDENTS
different students inside any 24h window (e.g. @campus_tix_99 spamming fake Fest passes).
Campaigns that target the same students are joined into one network (union-find).
"""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from pydantic import Field

from src import config, services
from src.gate import is_established_peer
from src.schemas import Recipient, Strict


class ScamCampaign(Strict):
    record_id: str = Field(pattern=r"^CMP-")
    network_id: str = ""
    handle: str
    display_name: str
    recipient: Recipient | None
    established: bool
    student_ids: list[str]
    txn_ids: list[str]
    txn_students: list[str]        # student of each txn, aligned with txn_ids
    amounts_aed: list[float]
    message_ids: list[str]
    links: list[str]
    total_aed: float
    first_ts: datetime
    last_ts: datetime


def _best_window(events: list[tuple], hours: int) -> list[tuple]:
    """Two-pointer sweep: the window (<= hours wide) with the most distinct students. events: (ts, student, ...)."""
    span, best, best_n, left = timedelta(hours=hours), [], 0, 0
    students: Counter = Counter()
    for right, e in enumerate(events):
        students[e[1]] += 1
        while e[0] - events[left][0] > span:
            students[events[left][1]] -= 1
            if students[events[left][1]] == 0:
                del students[events[left][1]]
            left += 1
        if len(students) > best_n:
            best_n, best = len(students), events[left:right + 1]
    return best


def detect_scam_campaigns(*, window_hours: int = 24, min_students: int = config.CAMPAIGN_MIN_STUDENTS,
                          include_established: bool = False,
                          conn: sqlite3.Connection | None = None) -> list[ScamCampaign]:
    own = conn is None
    conn = conn or services.connect()
    try:
        events: dict[str, list[tuple]] = defaultdict(list)
        for r in conn.execute("SELECT txn_id, student_id, recipient_handle, ts, amount_aed FROM transactions "
                              "WHERE txn_type = 'P2P'"):
            events[r[2]].append((datetime.fromisoformat(r[3]), r[1], "txn", r[0], r[4], None))
        for r in conn.execute("SELECT message_id, student_id, sender_handle, ts, link_url FROM messages"):
            events[r[2]].append((datetime.fromisoformat(r[3]), r[1], "msg", r[0], 0.0, r[4]))
        out: list[ScamCampaign] = []
        for handle, evs in events.items():
            evs.sort(key=lambda e: (e[0], e[3]))
            win = _best_window(evs, window_hours)
            studs = sorted({e[1] for e in win})
            if len(studs) < min_students:
                continue
            try:
                rc = services.verify_campus_recipient(handle, as_of=win[-1][0].date(), conn=conn)
            except services.VerificationTimeout:
                rc = None
            if rc is not None and rc.verified_vendor:
                continue
            est = is_established_peer(rc)
            if est and not include_established:
                continue
            txns = [e for e in win if e[2] == "txn"]
            out.append(ScamCampaign(
                record_id=f"CMP-{handle.strip('@')[:16]}", handle=handle,
                display_name=rc.recipient.display_name if rc and rc.recipient else handle,
                recipient=rc.recipient if rc else None, established=est, student_ids=studs,
                txn_ids=[e[3] for e in txns], txn_students=[e[1] for e in txns], amounts_aed=[e[4] for e in txns],
                message_ids=[e[3] for e in win if e[2] == "msg"], links=sorted({e[5] for e in win if e[5]}),
                total_aed=round(sum(e[4] for e in txns), 2), first_ts=win[0][0], last_ts=win[-1][0]))
    finally:
        if own:
            conn.close()
    _link_networks(out)
    return sorted(out, key=lambda c: (c.established, -len(c.student_ids), c.record_id))


def _link_networks(camps: list[ScamCampaign]) -> None:
    parent = list(range(len(camps)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[str, int] = {}
    for i, c in enumerate(camps):
        for s in c.student_ids:
            if s in owner:
                parent[find(i)] = find(owner[s])
            else:
                owner[s] = i
    roots = sorted({find(i) for i in range(len(camps))}, key=lambda x: camps[x].record_id)
    label = {root: f"NET-{n:02d}" for n, root in enumerate(roots, 1)}
    for i, c in enumerate(camps):
        c.network_id = label[find(i)]


# ---- Campaign outcome (summary of the per-payment states; pure Python) ----------------------------
OUTCOME_LABEL = {
    "UNTRIAGED": "⚪ Not triaged", "CLEARED": "💸 Sent automatically", "AWAITING_STUDENT": "🟡 Waiting for student",
    "AWAITING_STAFF": "🔴 Waiting for staff", "SENT": "💸 Sent after confirmation", "CANCELLED": "🛡️ Cancelled",
    "EXPIRED": "🛡️ Expired, money returned",
}
VERDICT_LABEL = {
    "NOT_TRIAGED": "⚪ Not triaged yet", "PARTIAL": "◔ Partly triaged", "AWAITING_STAFF": "🔴 Waiting for staff",
    "AWAITING_STUDENT": "🟡 Waiting for students", "MONEY_SENT": "💸 Some money was sent", "STOPPED": "🛡️ Scam stopped",
}
VERDICT_EXPLAIN = {
    "NOT_TRIAGED": "None of this account's payments have been checked yet.",
    "PARTIAL": "Some payments are checked and settled, but others still need to be triaged.",
    "AWAITING_STAFF": "At least one payment is paused and waiting for the support team.",
    "AWAITING_STUDENT": "Payments are paused until the students confirm or cancel.",
    "MONEY_SENT": "Every payment is settled, but at least one went through. Consider warning students.",
    "STOPPED": "Every payment to this account was cancelled or expired; no money reached it.",
}


def case_outcome(case) -> str:
    if case is None:
        return "UNTRIAGED"
    s = case.state.value if case.state else None
    return {"CLEARED": "CLEARED", "PENDING_CONFIRMATION": "AWAITING_STUDENT", "ESCALATED": "AWAITING_STAFF",
            "SENT": "SENT", "CANCELLED": "CANCELLED", "EXPIRED": "EXPIRED"}.get(
        s, {"CLEAR": "CLEARED", "HOLD_FOR_CONFIRMATION": "AWAITING_STUDENT", "ESCALATE": "AWAITING_STAFF"}.get(
            case.final_route.value if case.final_route else "", "UNTRIAGED"))


def campaign_verdict(outcomes: list[str]) -> dict:
    total, done = len(outcomes), [o for o in outcomes if o != "UNTRIAGED"]
    if not done:
        code = "NOT_TRIAGED"
    elif "AWAITING_STAFF" in done:
        code = "AWAITING_STAFF"
    elif "AWAITING_STUDENT" in done:
        code = "AWAITING_STUDENT"
    elif len(done) < total:
        code = "PARTIAL"
    elif any(o in ("SENT", "CLEARED") for o in done):
        code = "MONEY_SENT"
    else:
        code = "STOPPED"
    return dict(code=code, label=VERDICT_LABEL[code], explain=VERDICT_EXPLAIN[code], triaged=len(done),
                total=total, complete=len(done) == total)


# ---- Graphviz rendering (palette: first three validated categorical slots) ----------------------
PALETTE = {
    "light": {"student": "#2a78d6", "scam": "#eb6834", "established": "#1baf7a", "ink": "#0b0b0b", "muted": "#52514e"},
    "dark": {"student": "#3987e5", "scam": "#d95926", "established": "#199e70", "ink": "#f0efec", "muted": "#c3c2b7"},
}


def campaign_dot(camps: list[ScamCampaign], mode: str = "light") -> str:
    """Students -> account graph. Identity is carried by shape + label, not color alone."""
    p = PALETTE["dark" if mode == "dark" else "light"]
    out = ['digraph G {', 'rankdir=LR; bgcolor="transparent"; pad=0.2; nodesep=0.15; ranksep=0.9;',
           f'node [fontname="Helvetica" fontsize=10 fontcolor="{p["ink"]}" penwidth=2 style="rounded"];',
           f'edge [color="{p["muted"]}" fontcolor="{p["muted"]}" fontname="Helvetica" fontsize=9 penwidth=1.2 arrowsize=0.6];']
    seen: set[str] = set()
    for c in camps:
        kind = "established" if c.established else "scam"
        tag = "known classmate" if c.established else "suspected scam account"
        out.append(f'"{c.handle}" [shape=box color="{p[kind]}" penwidth=3 '
                   f'label="{c.handle}\\n{tag}\\n{len(c.student_ids)} students · {c.total_aed:,.0f} AED"];')
        paid = dict(zip(c.txn_students, c.amounts_aed))
        for s in c.student_ids:
            if s not in seen:
                out.append(f'"{s}" [shape=ellipse color="{p["student"]}" label="{s}"];')
                seen.add(s)
            label = f"paid {paid[s]:,.0f}" if s in paid else "messaged"
            out.append(f'"{s}" -> "{c.handle}" [label="{label}"];')
    out.append("}")
    return "\n".join(out)
