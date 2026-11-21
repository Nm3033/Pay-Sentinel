"""Round 3, Area 4: deployment reality. Who runs Pay Sentinel, where, what it costs, how fast it is, and what it
still cannot do.

Costs and speeds come from MEASURED checks (CaseFile.llm_usage, check_seconds, ai_on) priced with the published
per-model prices in config.LLM_PRICES_PER_1M; nothing is invented. Reporting only: never used for routing."""
from __future__ import annotations

from statistics import mean, median

from pydantic import BaseModel

from src import config
from src.schemas import CaseFile, Route


def price_per_1m(model: str) -> tuple[float, float] | None:
    """(input, output) USD per 1M tokens for a model, or None when its price is unknown."""
    if model in config.LLM_PRICES_PER_1M:
        return config.LLM_PRICES_PER_1M[model]
    if config.LLM_COST_PER_1M_INPUT or config.LLM_COST_PER_1M_OUTPUT:      # .env override for other models
        return config.LLM_COST_PER_1M_INPUT, config.LLM_COST_PER_1M_OUTPUT
    return None


def usage_cost(usage: list[dict]) -> tuple[float, set[str]]:
    """Cost in USD of these AI calls, each priced by the model that answered; plus any models with no price."""
    total, unpriced = 0.0, set()
    for u in usage:
        p = price_per_1m(u.get("model", ""))
        if p is None:
            unpriced.add(u.get("model", "?"))
            continue
        total += (u.get("prompt_tokens", 0) * p[0] + u.get("completion_tokens", 0) * p[1]) / 1e6
    return total, unpriced


class PilotEstimate(BaseModel):
    checked: int                      # payments measured
    ai_checked: int                   # of which checked with the AI switched on
    tokens_per_payment: float         # average over AI-on checks (many need no AI call at all)
    cost_per_payment_usd: float       # measured, priced per answering model
    worst_cost_per_payment_usd: float  # same tokens if the dearest priced model answered every call
    cost_per_day_usd: float
    cost_per_month_usd: float
    worst_cost_per_month_usd: float
    seconds_ai: float | None          # median time for one whole check with AI
    seconds_ai_slowest: float | None
    seconds_rules: float | None       # median time with rules only
    staff_minutes_saved_per_payment: float
    staff_hours_saved_per_day: float
    staff_hours_saved_per_month: float
    unpriced_models: list[str]
    per_day: int = config.PILOT_FLAGGED_PER_DAY
    days_per_month: int = config.PILOT_SCHOOL_DAYS_PER_MONTH


def estimate(cases: list[CaseFile]) -> PilotEstimate:
    """Scale what was measured on these checks up to the pilot's daily volume."""
    ai = [c for c in cases if c.ai_on]
    usage = [u for c in ai for u in c.llm_usage]
    tokens = sum(u.get("prompt_tokens", 0) + u.get("completion_tokens", 0) for u in usage)
    cost, unpriced = usage_cost(usage)
    dearest = max(config.LLM_PRICES_PER_1M.values(), key=lambda p: p[0] + p[1], default=(0.0, 0.0))
    worst = sum(u.get("prompt_tokens", 0) * dearest[0] + u.get("completion_tokens", 0) * dearest[1]
                for u in usage) / 1e6
    n_ai = len(ai) or 1
    per_pay, worst_pay = cost / n_ai, worst / n_ai
    day, month = config.PILOT_FLAGGED_PER_DAY, config.PILOT_FLAGGED_PER_DAY * config.PILOT_SCHOOL_DAYS_PER_MONTH
    t_ai = [c.check_seconds for c in ai if c.check_seconds is not None]
    t_rules = [c.check_seconds for c in cases if not c.ai_on and c.check_seconds is not None]
    staff = [c for c in cases if c.final_route is not None]
    esc = sum(c.final_route == Route.ESCALATE for c in staff) / len(staff) if staff else 0.0
    saved_min = config.STAFF_MIN_PER_REVIEW - esc * config.STAFF_MIN_ASSISTED
    return PilotEstimate(
        checked=len(cases), ai_checked=len(ai), tokens_per_payment=round(tokens / n_ai, 1),
        cost_per_payment_usd=round(per_pay, 6), worst_cost_per_payment_usd=round(worst_pay, 6),
        cost_per_day_usd=round(per_pay * day, 4), cost_per_month_usd=round(per_pay * month, 2),
        worst_cost_per_month_usd=round(worst_pay * month, 2),
        seconds_ai=round(median(t_ai), 2) if t_ai else None, seconds_ai_slowest=round(max(t_ai), 2) if t_ai else None,
        seconds_rules=round(median(t_rules), 3) if t_rules else None,
        staff_minutes_saved_per_payment=round(saved_min, 2),
        staff_hours_saved_per_day=round(saved_min * day / 60, 1),
        staff_hours_saved_per_month=round(saved_min * month / 60, 0),
        unpriced_models=sorted(unpriced))


# ---- the plan itself (shown on the Pilot plan page and in DEPLOYMENT.md) ------------------------------------------
WHO = [
    ("Students", "Use PocketPay as normal. Only when a payment is paused do they see Pay Sentinel: “I know this "
                 "seller / Cancel / It's a scam”, and a 2-minute “Wait, cancel it!” window."),
    ("Parents/guardians", f"Approve or decline transfers above {config.GUARDIAN_LIMIT_AED:,.0f} AED by students under "
                          f"{config.GUARDIAN_AGE}."),
    ("Campus support team (2 people)", "Work the Staff review queue (only payments the rules escalate), release or "
                                       "cancel, and broadcast scam warnings. Each has their own staff login."),
    ("School IT owner", "Runs the app: staff accounts and passwords, the AI keys (kept in secrets, never in code), "
                        "backups, updates, and a weekly look at the Activity log and safety evidence."),
    ("PocketPay (the wallet provider)", "Sends each flagged payment to Pay Sentinel and pauses / releases the real "
                                        "money when Pay Sentinel's state changes (integration still to build)."),
]

WHERE = [
    ("Today (demo & first pilot week)", "Streamlit Community Cloud", "Free",
     "Public HTTPS link, deploys from GitHub. Limits: about 1 GB memory, the app sleeps after 12 hours without "
     "visitors (wakes on the next visit), and its disk is wiped on every reboot or redeploy."),
    ("Real pilot", "A small cloud VM or a school server running the same app", "About USD 5–15 / month (estimate)",
     "Persistent disk for the database, nightly backups, HTTPS, staff-only access. Same code, no rewrite."),
    ("AI", f"Groq: {config.LLM_MODEL} first, then the backup chain, then rules only", "Pay per use (below)",
     "Free plan has strict per-minute and daily limits; a pilot should use the paid Developer plan "
     "(250K tokens/min, 1K requests/min per model) and add a backup on a second provider."),
]

LIMITATIONS = [
    "**Synthetic data only.** Not yet connected to PocketPay's real payment system; pausing and releasing real "
    "money needs an API integration with the wallet provider.",
    "**Simulated student and guardian identity.** The Student and Guardian apps have no login yet; a real pilot "
    "needs the school's single sign-on.",
    "**Storage.** SQLite on Streamlit Community Cloud is wiped on every reboot; a pilot needs a persistent database "
    "with backups.",
    "**One AI provider.** The main AI and its backup are both on Groq, so one outage or rate limit can hit both "
    "(we saw this in testing). The rules still decide safely, but explanations become the standard wording.",
    "**Mostly English.** The slang and scam patterns are English (the hidden-instruction filter also catches Arabic "
    "commands); messages in other languages are untested, and unclear ones are escalated to a person.",
    "**Tuned on synthetic data.** 82% of safe payments clear instantly; the other 18% need one tap from the student. "
    "Thresholds need tuning on real traffic before a full rollout.",
    "**Not verified at scale or by a security audit.** Tested with 200 payments a day and short bursts; no load test "
    "beyond that, no penetration test. Demo passwords are public and must be changed.",
    "**Can't verify ticket ownership.** It flags risky sellers but cannot prove a ticket or item is real.",
]

MONITORING = [
    "Activity log: every state change, refused attempt and refused entry, with who and why (CSV export).",
    "Safety evidence: the three Round 3 cases with their log entries (Markdown/CSV export).",
    "Results: scams that got through (target 0), staff queue precision, AI calls, tokens, cost and latency.",
    "Failure notices on each payment (AI down, rate limit, time limit, directory timeout) and the warning at the top "
    "when a failure test is switched on.",
]
