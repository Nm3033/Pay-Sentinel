# Pay Sentinel — Scam Defense for the PocketPay Campus Wallet · PLAN

Track: IEEE CS SYP TechX Dubai '26 · Domain 1 Fintech Fraud & Compliance · **High School**
(*PocketPay — Autonomous Campus Wallet & Scam Defense Agent*). Adapted in place from the university-track build:
same architecture, modules, UI shell, auth and tests; domain, data, rules and states changed.

## 1. Architecture & Stack

**Problem:** a two-person support team hand-reviews ~200 flagged payments + 20 suspicious messages a day; AED 15 canteen
snacks wait in the same queue as scams like *"Bro urgent! Selling my Front Row Fest pass for 50 AED…"*.

**Core principle:** the AI reads and writes; Python decides and acts. The AI never sets thresholds, never picks the final
route on its own, and nothing can freeze a wallet.

```
flagged payment ──► [CODE] sanitizer regex  ─┐ chat message + payment note (untrusted)
                ──► [AI  ] sanitizer agent   ─┘ classification only (can only add suspicion)
                ──► [CODE] scam & slang scanner (src/slang.py): urgency, phishing link, impersonation, too-good,
                │          exam leak; lists slang it can't read
                ──► [AI  ] message assessor: scam risk + CONFIDENCE + unclear terms (advisory)
                ──► [CODE] tools: get_transaction_context(), verify_campus_recipient() (timeout → fallback),
                │          compute_recipient_spread() (campaigns)
                ──► [CODE] gate.py → CLEAR | HOLD_FOR_CONFIRMATION | ESCALATE
                ──► [AI  ] case writer: student-friendly explanation + staff summary + cited findings
                ──► [CODE] citation check, final = max(gate, AI)  (AI only adds caution)
                ──► [CODE] update_payment_state(): CLEARED | PENDING_CONFIRMATION (student) | ESCALATED (staff)
                ──► [HUMAN] student: confirm / cancel / report · staff: release / cancel / ask student
                           (holds expire → money returned; reminders capped → no loops)
```

| Layer | Choice |
|---|---|
| Data | SQLite `data/campus_wallet_logs.sqlite` (stdlib `sqlite3`) |
| Schemas | Pydantic v2, `extra="forbid"` |
| UI | Streamlit multipage, staff login |
| LLM | one `openai`-SDK client; Groq / Ollama `qwen3` / Gemini Flash via `.env` |
| Tests | pytest (128) incl. Streamlit AppTest |

## 2. Project Structure

| File | Responsibility |
|---|---|
| `src/config.py` | Payment rules as constants: `VENDOR_NORMAL_MAX_AED=150`, `P2P_AUTO_MAX_AED=100`, `NEW_ACCOUNT_DAYS=30`, `TRUST_MIN=60`, `REPORTS_ESCALATE=2`, `CAMPAIGN_MIN_STUDENTS=3`, `CONFIDENCE_MIN=0.7`, `UNCLEAR_TERMS_MIN=2`, `HOLD_EXPIRY_HOURS=24`, `MAX_STUDENT_PROMPTS=2` |
| `src/schemas.py` | `Transaction`, `StudentProfile`, `ChatMessage`, `Recipient`, `RecipientCheck`, `TransactionContext`, `MessageSignals`, `MessageAssessment`, `GateDecision`, `Route`, `PaymentState`, `Action` (no freeze), `PaymentStateRecord`, `CaseFile`, `NewTransaction` |
| `src/seed_data.py` | 250 students, verified vendors, peers (established / new / scam), 250 flagged payments (238 background + 12 demo; counts in `COUNTS`), 121 chat messages, **12 named demo scenarios** (3 uncertain, 1 outage); hidden labels for eval |
| `src/services.py` | `get_transaction_context()`, `verify_campus_recipient()`, `update_payment_state()` (transition table, identity checks, reminder cap, independent CLEAR guard), `expire_stale_holds()`, campaign spread, broadcasts, intake |
| `src/slang.py` | Deterministic scam-pattern + unreadable-slang scanner |
| `src/sanitizer.py` | Prompt-injection guard (regex + AI checker), unchanged |
| `src/gate.py` | Pure-Python routing rules + plain-language reasons |
| `src/workflow.py` | Orchestration, AI assessor & writer, citation check, `staff_decide()`, `student_respond()`, `remind_student()`, `expire_holds()`, CLI scorecard |
| `src/report.py` + `templates/decision_log_template.md` | Shareable decision log per payment |
| `src/graph.py` | Scam campaign detection (one account → many students in 24h) + outcome summary + graph |
| `src/telemetry.py` | Staff hours saved, scams stopped, safe payments cleared, AI cost (eval-only module) |
| `src/auth.py` | Support-staff accounts (`SUP-01`, `SUP-02`), PBKDF2, lockout, signed sessions |
| `app.py` | Login → Start here, Flagged payments, Investigation, 📱 Student app, Staff review, Add payments, Results & ROI, 📣 Scam alerts, Audit log |

## 3. Build status

- **Core 1 (end-to-end triage):** ✅ 12 scenarios through both tools with student-friendly explanations; 3 uncertain cases escalate.
- **Core 2 (stateful memory + hold guard):** ✅ `payment_states` table, idempotent re-runs, capped reminders, expiry, no freeze action.
- **Batch / parallel checking:** ✅ multi-select in Flagged payments ("Check N selected", "Check all shown"); `workflow.triage_many()` runs payments 4 at a time (1–8, sidebar slider; CLI `--workers`), dedupes, skips finished or human-decided payments, isolates failures and per-payment AI usage; the AI client stops calling the API when the provider says to wait > 20s (e.g. daily quota) so batches fall back to rules in seconds.
- **Stretch:** ✅ "I know this seller / Cancel transfer" UI · ✅ scam alert broadcasting · ✅ staff-hours dashboard ·
  ⬜ ticket barcode/ownership verification (not built).

- **Round 2 curveballs:** ✅ HS-1.1 split payments (`compute_pair_window`, `SPLIT_PAYMENTS` hold) · ✅ HS-1.2 lookalike
  vendors (`src/lookalike.py`, `LOOKALIKE_VENDOR` escalate + CLEAR guard) · ✅ HS-1.3 AI-addressed text (`role_prefix`,
  `clear_command`, `verification_claim` sanitizer rules) · ✅ HS-1.4 `CONFIRMED_CANCELLABLE` → `CANCELLED` (inside the window)
  / `RELEASED` (after it), refused attempts logged as `REJECTED:` · ✅ HS-1.5 `PENDING_GUARDIAN` + `guardian:` actor,
  age from `students.date_of_birth`. Tests: `tests/test_curveballs.py`.

Results: code-only on all 250 payments, 0/50 scams auto-cleared, 15/15 uncertain escalated, 152/185 (82%) safe payments
cleared instantly. Live AI (Groq gpt-oss-120b): 12/12 demo scenarios match.

## 4. Checkpoint (team)
1. `pip install -r requirements.txt` · `python -m pytest -q` → 128 passed.
2. `python -m src.auth init --force` (creates `SUP-01`, `SUP-02`; passwords printed once).
3. `python -m src.seed_data` · `python -m src.workflow --demo` → 12/12.
4. `streamlit run app.py` and rehearse `DEMO.md`.

## 5. AI Coding Agent Handoff Rules
1. Read `PLAN.md` + `CLAUDE.md` first; touch only the files the task needs.
2. Never paste dataset rows in chat or hand-write data; change `src/seed_data.py`.
3. Never move a threshold, route or state change into a prompt or AI output; never add a freeze/block action.
4. Every LLM call: Pydantic output model, `extra="forbid"`, code-side fallback.
5. Keep `pytest` green; stop and ask if the brief and the code disagree.

### Assumptions
- All data is synthetic and fictional; amounts in AED. Student identity in the student app is simulated (no student login).
- Staff-time figures assume ~5 min per manual review and ~2 min per escalated case with a summary ready.
- Campaign detection looks ±24h around a payment (post-hoc review).
