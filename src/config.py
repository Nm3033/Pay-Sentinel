"""Single source of truth for thresholds and environment settings.

Pay Sentinel protects PocketPay, a campus wallet: safe everyday purchases clear on their own,
risky peer transfers wait for the student to confirm, and confirmed scams go to support staff.

SAFETY INVARIANT: payment thresholds are plain Python constants defined here.
They are never read from env, prompts, or LLM output. Changing one needs team approval.
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # python-dotenv is optional at import time (tests run without .env)
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent

# ---- Paths -----------------------------------------------------------------
DB_PATH = Path(os.getenv("GH_DB_PATH", ROOT / "data" / "campus_wallet_logs.sqlite"))
TEMPLATE_PATH = ROOT / "templates" / "decision_log_template.md"
REPORTS_DIR = ROOT / "reports"
OFFICERS_PATH = Path(os.getenv("OFFICERS_PATH", ROOT / "data" / "officers.json"))  # staff credentials (hashed)

# ---- Support-staff authentication ---------------------------------------------
OFFICER_SESSION_MINUTES: int = 30
OFFICER_MAX_FAILED: int = 5
OFFICER_LOCKOUT_MINUTES: int = 15

# ---- Hard payment rules (pure Python; NOT configurable via env) ---------------
VENDOR_NORMAL_MAX_AED: float = 150.0  # verified campus vendor + amount <= this (+ no red flags) => CLEAR
P2P_AUTO_MAX_AED: float = 100.0       # peer transfer above this => student must confirm
NEW_ACCOUNT_DAYS: int = 30            # recipient account younger than this => "new account"
TRUST_MIN: int = 60                   # peer trust score below this => hold for confirmation
REPORTS_ESCALATE: int = 2             # recipient with >= this many scam reports => escalate to staff
CAMPAIGN_MIN_STUDENTS: int = 3        # one unverified recipient reaching >= this many students in 24h => scam campaign
CONFIDENCE_MIN: float = 0.7           # AI confidence below this => uncertain => escalate (never guess)
UNCLEAR_TERMS_MIN: int = 2            # >= this many unreadable slang terms (code OR AI) => uncertain
NEW_DEVICE_DAYS: int = 30             # student's device younger than this => new device
VELOCITY_MAX_24H: int = 6             # student payments in 24h above this => unusual
HOLD_EXPIRY_HOURS: int = 24           # unanswered confirmation expires -> transfer cancelled, money returned
MAX_STUDENT_PROMPTS: int = 2          # one prompt + one reminder; the agent never asks again (no loops)
MEMO_MAX_CHARS: int = 280             # sanitized text truncation length

# ---- Dataset ---------------------------------------------------------------
SEED: int = 42

# ---- LLM (env-configurable transport only; never thresholds) ---------------
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3")
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "60"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))  # generous: reasoning models spend hidden tokens
# "" = don't send. Defaults to "low" for gpt-oss models (fast, cheap); ignored by most other providers.
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "low" if "gpt-oss" in LLM_MODEL else "")
SANITIZER_USE_LLM = os.getenv("SANITIZER_USE_LLM", "true").lower() == "true"

# ---- ROI telemetry (reporting assumptions only; never used for routing) ------------
STAFF_MIN_PER_REVIEW: float = 5.0     # ASSUMPTION: minutes for the 2-person team to review one flagged item by hand
STAFF_MIN_ASSISTED: float = 2.0       # ASSUMPTION: minutes for an escalated case with summary + evidence ready
LLM_COST_PER_1M_INPUT = float(os.getenv("LLM_COST_PER_1M_INPUT", "0"))   # USD; set from provider pricing
LLM_COST_PER_1M_OUTPUT = float(os.getenv("LLM_COST_PER_1M_OUTPUT", "0"))
