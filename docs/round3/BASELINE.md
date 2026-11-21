# Round 3 baseline: Pay Sentinel before the Round 3 changes

Recorded 21 Nov 2026 (Phase 0), on a fresh copy of the 200-payment synthetic dataset. This is the "before" for the
before/after slide; judges compare Round 3 with what was built earlier.

## State of the project
- Code: all work committed and pushed (`main` = `origin/main`, latest commit "Add dark mode").
- Tests: `python -m pytest -q` → **171 passed**.
- Live link: https://pay-sentinel-techx.streamlit.app is up (HTTP 200) and serves the current light-blue design.

## Rules only (no AI), all 200 payments
| Measure | Result |
|---|---|
| Decisions | 121 sent automatically · 28 ask the student · 51 send to staff |
| Scams that went through | **0 / 40** |
| Unclear messages sent to a person | **12 / 12** |
| Hidden-instruction attacks that went through | **0 / 8** |
| Safe payments cleared instantly | 121 / 148 (82%) |
| Staff queue that really needed staff | 100% |
| Staff hours saved per day | 15.0 |
| Speed | 200 payments in 2.2 s (~11 ms each), 0 failures |

## With the AI chain (Groq `openai/gpt-oss-120b`, backup `qwen/qwen3.8-27b`), 12 demo scenarios
| Measure | Run 1 | Run 2 |
|---|---|---|
| Decisions matching the expected route | **12 / 12** | 12 / 12 |
| Time for all 12 (4 checked at once) | 38.4 s (3.2 s per payment) | — |
| AI calls / tokens per payment | 21 calls / 874 tokens | 27 calls / 1,510 tokens |
| Average AI call latency | 0.58 s | 0.67 s |
| Cost per payment | not shown: `LLM_COST_PER_1M_*` is 0 (Phase 4) | |

**Found while recording the baseline (input for Phase 2):** back-to-back runs hit Groq's free-tier **per-minute**
rate limit (HTTP 429). The backup model on the same provider was limited too, so for 5 payments the student
explanation fell back to the rules-only wording. The decisions stayed correct (12/12) and nothing crashed, but the
on-screen message for this should be clearer. A stage demo straight after rehearsals could hit the same limit. The
twelfth logged error is the planned verification-outage scenario.

## Screenshots (`docs/round3/baseline/`)
`home` · `queue` · `details` · `student` · `staff` · `results` · `alerts` · `log` · `add`, taken from a local copy after
checking the 12 demo scenarios with the AI.
