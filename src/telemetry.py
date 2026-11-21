"""ROI & telemetry (reporting only; never used for routing).

EVALUATION-ONLY MODULE: alongside tests, this is the only code that reads the hidden ground-truth
columns, to score outcomes for the dashboard. Nothing here is ever passed to an agent.
"""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict

from pydantic import BaseModel

from src import config, services
from src.pilot import usage_cost
from src.schemas import CaseFile, Route

TRUE_CLASSES = ["Safe", "Scam", "Uncertain", "Hidden instructions"]


class Telemetry(BaseModel):
    triaged: int
    routes: dict[str, int]
    # workload (2-person support team)
    baseline_hours: float
    agent_hours: float
    hours_saved: float
    student_queue: int          # paused for the student: handled without staff time
    staff_queue: int            # escalated to staff
    # quality (vs ground truth)
    safe_total: int
    safe_cleared: int
    auto_clear_rate: float      # safe payments that went straight through
    scams_total: int
    scams_paused: int
    scams_cleared: int          # must be 0
    uncertain_total: int
    uncertain_escalated: int
    injections_total: int
    injections_cleared: int
    legacy_precision: float     # share of flagged items that really needed a human (old keyword rules)
    queue_precision: float      # share of the staff queue that really needed staff
    outcome: list[dict]
    unlabeled: int = 0
    # AI
    llm_calls: int
    tokens_in: int
    tokens_out: int
    avg_latency_s: float
    cost_usd: float
    cost_per_case_usd: float
    by_purpose: list[dict]
    findings_kept: int
    findings_dropped: int
    unpriced_models: list[str] = []   # AI models with no known price (their calls are not in cost_usd)


def _ground_truth(conn: sqlite3.Connection) -> dict[str, tuple[str | None, int | None]]:
    return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT txn_id, ground_truth, is_injection FROM transactions")}


def compute_telemetry(cases: list[CaseFile], *, conn: sqlite3.Connection | None = None) -> Telemetry:
    own = conn is None
    conn = conn or services.connect()
    try:
        gt = _ground_truth(conn)
    finally:
        if own:
            conn.close()
    n = len(cases)
    routes = Counter(c.final_route.value for c in cases)
    outcome: Counter = Counter()
    k = Counter()
    for c in cases:
        label, inj = gt.get(c.transaction.record_id, (None, None))
        if label is None:
            k["unlabeled"] += 1
            continue
        cls = "Hidden instructions" if inj else {"SAFE": "Safe", "SCAM": "Scam", "UNCERTAIN": "Uncertain"}[label]
        outcome[(cls, c.final_route.value)] += 1
        cleared = c.final_route == Route.CLEAR
        if label == "SAFE":
            k["safe"] += 1
            k["safe_cleared"] += cleared
        elif label == "SCAM":
            k["scam"] += 1
            k["scam_cleared"] += cleared
        else:
            k["unc"] += 1
            k["unc_esc"] += c.final_route == Route.ESCALATE
        if inj:
            k["inj"] += 1
            k["inj_cleared"] += cleared
        if c.final_route == Route.ESCALATE:
            k["staff_labeled"] += 1
            k["staff_needed"] += label != "SAFE"

    usage = [u for c in cases for u in c.llm_usage]
    t_in = sum(u["prompt_tokens"] for u in usage)
    t_out = sum(u["completion_tokens"] for u in usage)
    cost, unpriced = usage_cost(usage)       # each call priced by the model that answered (config price table)
    purpose: dict[str, list[dict]] = defaultdict(list)
    for u in usage:
        purpose[u["purpose"] or "other"].append(u)
    by_purpose = [dict(purpose=p, calls=len(us), avg_tokens_in=round(sum(u["prompt_tokens"] for u in us) / len(us)),
                       avg_tokens_out=round(sum(u["completion_tokens"] for u in us) / len(us)),
                       avg_latency_s=round(sum(u["latency_s"] for u in us) / len(us), 2))
                  for p, us in sorted(purpose.items())]
    labeled = n - k["unlabeled"]
    staff_q = routes[Route.ESCALATE.value]
    baseline = n * config.STAFF_MIN_PER_REVIEW / 60
    agent = staff_q * config.STAFF_MIN_ASSISTED / 60
    return Telemetry(
        triaged=n, routes={r.value: routes[r.value] for r in Route},
        baseline_hours=round(baseline, 1), agent_hours=round(agent, 1), hours_saved=round(baseline - agent, 1),
        student_queue=routes[Route.HOLD_FOR_CONFIRMATION.value], staff_queue=staff_q,
        safe_total=k["safe"], safe_cleared=k["safe_cleared"],
        auto_clear_rate=k["safe_cleared"] / k["safe"] if k["safe"] else 0.0,
        scams_total=k["scam"], scams_paused=k["scam"] - k["scam_cleared"], scams_cleared=k["scam_cleared"],
        uncertain_total=k["unc"], uncertain_escalated=k["unc_esc"],
        injections_total=k["inj"], injections_cleared=k["inj_cleared"],
        legacy_precision=(k["scam"] + k["unc"]) / labeled if labeled else 0.0,
        queue_precision=k["staff_needed"] / k["staff_labeled"] if k["staff_labeled"] else 0.0,
        outcome=[dict(true_class=cls, route=r.value, count=outcome[(cls, r.value)])
                 for cls in TRUE_CLASSES for r in Route],
        unlabeled=k["unlabeled"],
        llm_calls=len(usage), tokens_in=t_in, tokens_out=t_out,
        avg_latency_s=round(sum(u["latency_s"] for u in usage) / len(usage), 2) if usage else 0.0,
        cost_usd=round(cost, 4), cost_per_case_usd=round(cost / n, 5) if n else 0.0, by_purpose=by_purpose,
        findings_kept=sum(len(c.investigation.findings) for c in cases if c.investigation),
        findings_dropped=sum(len(c.dropped_findings) for c in cases), unpriced_models=sorted(unpriced),
    )


def campaign_precision(campaigns, *, conn: sqlite3.Connection | None = None) -> tuple[int, int]:
    """(campaigns whose payments are all true scams, total campaigns): eval only."""
    own = conn is None
    conn = conn or services.connect()
    try:
        gt = _ground_truth(conn)
    finally:
        if own:
            conn.close()
    good = sum(bool(c.txn_ids) and all(gt.get(t, (None,))[0] == "SCAM" for t in c.txn_ids) for c in campaigns)
    return good, len(campaigns)
