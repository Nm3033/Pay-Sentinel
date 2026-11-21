# Pay Sentinel: deployment plan for a pilot

What it takes to run Pay Sentinel for real: who runs it, where, what it costs, how fast it is, and what it still
cannot do. Numbers were **measured on 21 Nov 2026** (12 test scenarios checked one at a time with the AI on, plus all
200 synthetic payments with rules only) and priced with Groq's published prices. The app's **🚀 Pilot plan** page
recomputes them live from whatever has been checked.

## At a glance
| | |
|---|---|
| **AI cost per payment** | **USD 0.0008** (about 0.003 AED), measured. Worst case USD 0.0032 if the backup AI answered every call |
| **AI cost per month** | **USD 3.62** (about 13 AED) for 200 flagged payments a day × 22 school days. Worst case USD 14.04 |
| **Hosting** | Free on Streamlit Community Cloud today; about USD 5–15 a month on a small server for a real pilot (estimate) |
| **Speed per payment** | **4.8 s** median with AI (1–3 s when the AI isn't rate-limited; never more than the 20 s cap); **3 ms** rules only |
| **Staff time saved** | About **15 hours a day** for the 2-person team (4.5 min per flagged payment; assumptions below) |

## Who runs it
| Who | What they do |
|---|---|
| Students | Use PocketPay as normal. Only a paused payment shows Pay Sentinel: “I know this seller / Cancel / It's a scam”, then a 2-minute “Wait, cancel it!” window. |
| Parents/guardians | Approve or decline transfers above 100 AED by students under 16. |
| Campus support team (2 people) | Work the Staff review queue (only escalated payments), release or cancel, and broadcast scam warnings. Each has their own login. |
| School IT owner | Runs the app: staff accounts, the AI keys (in secrets, never in code), backups, updates, and a weekly look at the Activity log and safety evidence. |
| PocketPay (wallet provider) | Sends each flagged payment to Pay Sentinel, and pauses or releases the real money when Pay Sentinel's state changes. This integration is still to build. |

## Where it runs
| Stage | Where | Cost | Notes |
|---|---|---|---|
| Today (demo and first pilot week) | Streamlit Community Cloud | Free | Public HTTPS link that deploys from GitHub. About 1 GB memory, sleeps after 12 h without visitors (wakes on the next visit), and the disk is **wiped on every reboot or redeploy**. |
| Real pilot | Small cloud VM (1 vCPU, 1–2 GB) or a school server, same app | About USD 5–15 / month (estimate) | Persistent disk for the database, nightly backups, HTTPS, staff-only access. Same code, no rewrite. |
| AI | Groq: `openai/gpt-oss-120b` first, then the backup `qwen/qwen3.8-27b`, then rules only | Pay per use | The free plan has strict per-minute and daily limits. A pilot should use the paid Developer plan (250K tokens/min, 1K requests/min per model) and add a backup on a **second provider**. |

### Costs in detail
Each AI call is priced by the model that answered (`src/config.py → LLM_PRICES_PER_1M`, checked 21 Nov 2026 on
console.groq.com/docs/models):

| Model | Role | USD per 1M input tokens | USD per 1M output tokens |
|---|---|---|---|
| `openai/gpt-oss-120b` | main AI | 0.15 | 0.60 |
| `qwen/qwen3.8-27b` | backup AI (a Groq *preview* model) | 0.80 | 4.00 |

Measured on the 12 test scenarios: 33 AI calls (29 by the main AI, 4 by the backup when the main AI was
rate-limited), 2,264 tokens per payment on average. These scenarios are mostly risky ones, which need the most AI
work, so everyday traffic (mostly canteen purchases, with no message to read) should cost **less** per payment.
Rules-only checks cost nothing. A realistic pilot month is about **USD 4–30 in total** (AI plus hosting).

### Speed in detail
| | Measured |
|---|---|
| One check with AI (median) | 4.8 s |
| Fastest / slowest with AI | 0.9 s / 17.9 s |
| One check, rules only | 3 ms (all 200 payments in under a second) |

The slow checks came from the **free plan's rate limit**: the client waited, retried and then switched to the backup
AI. No check can take longer than `CASE_TIME_LIMIT_S` (20 s); after that, the fixed rules finish it.

### Staff time
Today the 2-person team reviews every flagged payment by hand (assumed **5 min** each). With Pay Sentinel, only
escalated payments reach staff, with the summary and evidence ready (assumed **2 min** each); everything else is
cleared automatically or answered by the student. On the synthetic data that saves about 4.5 min per flagged payment:
about **15 h a day** at 200 payments. Both minute figures are assumptions in `src/config.py`, to re-measure in the
pilot.

## What it still can't do
- **Synthetic data only.** It isn't yet connected to PocketPay's real payment system; pausing and releasing real
  money needs an API integration with the wallet provider.
- **Simulated student and guardian identity.** The Student and Guardian apps have no login yet; a real pilot needs
  the school's single sign-on.
- **Storage.** SQLite on Streamlit Community Cloud is wiped on every reboot; a pilot needs a persistent database with
  backups.
- **One AI provider.** The main AI and its backup are both on Groq, so one outage or rate limit can hit both (seen in
  testing). The rules still decide safely, but explanations fall back to the standard wording.
- **Mostly English.** The slang and scam patterns are English; the hidden-instruction filter also catches Arabic
  commands. Messages in other languages are untested, and unclear ones are escalated to a person.
- **Tuned on synthetic data.** 82% of safe payments clear instantly; the other 18% need one tap from the student.
  Thresholds need tuning on real traffic.
- **Not verified at scale or by a security audit.** Tested at 200 payments a day and in short bursts; no larger load
  test and no penetration test. The demo passwords are public and must be changed.
- **Can't verify ticket ownership.** It flags risky sellers but cannot prove a ticket or item is real.

## How we keep an eye on it
- **Activity log:** every state change, refused attempt and refused entry, with who and why (CSV export).
- **Safety evidence:** the three safety cases (safe → automatic, risky → paused, attack → blocked) with their log
  entries (Markdown/CSV export).
- **Results:** scams that got through (target 0), staff queue precision, AI calls, tokens, cost and latency.
- **Failure notices:** each payment shows a notice when something went wrong (AI down, rate limit, time limit,
  directory timeout), and the sidebar warns while a failure test is switched on.

## Running it
- **Today:** push to GitHub; Streamlit Community Cloud redeploys. The AI keys and staff accounts live in the app's
  *Secrets*. Reboot the app after changing `.streamlit/config.toml`.
- **Real pilot:** on the server, `pip install -r requirements.txt`, set the same values as environment variables (see
  `.env.example`), run `python -m src.auth init` for staff accounts, then `streamlit run app.py` behind HTTPS.
  Back up `data/` and `reports/` nightly.
