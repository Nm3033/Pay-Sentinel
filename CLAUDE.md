# CLAUDE.md — Pay Sentinel (scam defense for the PocketPay campus wallet)

Read `PLAN.md` before any work. Build only what you were asked for, then stop at the checkpoint.
Track: IEEE CS SYP TechX Dubai '26, Domain 1 High School: *PocketPay Autonomous Campus Wallet & Scam Defense Agent*.

## Stack
- Python 3.10+, stdlib `sqlite3` (`data/campus_wallet_logs.sqlite`), Pydantic v2, Streamlit, pytest.
- LLM via the `openai` SDK with `base_url`/`model`/`key` from `.env` (Ollama `qwen3` | Groq | Gemini Flash). No other LLM frameworks.
- Run as modules from repo root: `python -m src.seed_data`, `python -m src.workflow --demo`, `streamlit run app.py`.

## Safety invariants (never violate)
1. **Thresholds live only in `src/config.py`** as Python constants. Never in prompts or LLM output.
2. **`src/gate.py` is pure Python** (imports only `config` + `schemas`; a test enforces this). Its only outcomes are
   `CLEAR`, `HOLD_FOR_CONFIRMATION`, `ESCALATE`.
3. **No permanent blocks.** There is no freeze/block action anywhere. `services.update_payment_state()` is the only way a
   payment changes state; it follows the fixed `TRANSITIONS` table and raises `InvalidTransition` for anything else.
   Missing data, timeouts or low confidence can only cause a pause.
4. **The AI may raise caution, never lower it:** `final_route = max(gate_route, ai_route)`. Uncertainty is OR-ed: the
   code's unreadable slang OR low AI confidence OR AI-listed unclear terms ⇒ ESCALATE (never guess).
5. **Humans own the money decisions.** Student actions must come from that student (`actor="student:STU-xxxx"`); staff
   actions need a valid signed session (`src/auth.py`) whose ID matches the approval. Tokens are never serialized.
6. **Stateful memory, no loops.** Waiting is a row in `payment_states`, never a polling loop. Re-running a case is
   idempotent; reminders are capped at `MAX_STUDENT_PROMPTS`; unanswered holds `EXPIRE` after `HOLD_EXPIRY_HOURS`
   (money returned).
7. **Outage fallback.** If a tool fails (e.g. `VerificationTimeout`), record it and pause; never crash.
8. **Untrusted text.** Chat messages and payment notes go through `regex_guard` → Sanitizer Agent before any other AI
   reads them; the AI only sees `clean_text`. Hidden instructions ⇒ ESCALATE.
9. **No uncited claims.** Each AI `Finding` cites ≥1 record ID a tool actually returned; `verify_citations()` drops the
   rest and masks unknown IDs in free text.
10. All LLM I/O models use Pydantic with `extra="forbid"`. LLM failure ⇒ code-only path, never a less careful route.
11. **Round 2 rules stay in code:** split totals, lookalike checks and the guardian rule are computed in Python from
   the DB; the cancel window is checked against the clock in `update_payment_state()`. Every refused state change is
   written to `case_resolutions` with a `REJECTED:` note and the state left unchanged. Guardian actions must come from
   `guardian:<the student's guardian_id>`.
12. Evaluation-only columns (`ground_truth`, `scenario`, `is_injection`, `injection_variant`, `messages.label`,
    `demo_scenarios.expected_route`) are never put in prompts. Only `src/telemetry.py`, the workflow CLI scorecard and
    `tests/` may read them.
13. Console data entry (`validate_new_transactions` → `add_transactions`) validates every row, inserts atomically, stores
    text raw (screened at triage), tags rows `manual:<staff>`, and leaves ground truth NULL.

## Conventions
- Business logic lives in `src/`. `app.py` only renders and calls into `src/`.
- Never write dataset rows by hand or paste them in chat. Change `src/seed_data.py` instead.
- Keep `.env` out of git and commit only `.env.example`. Don't log API keys.
- Add tests with the code they cover. `pytest` must pass before each checkpoint.
