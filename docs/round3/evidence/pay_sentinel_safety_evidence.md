# Pay Sentinel: safety evidence from the activity log
Generated 2026-11-21 10:41. Every row below is a real entry in the `case_resolutions` table (the activity log); checks are done by fixed Python rules, not by the AI.

## ✅ Safe action done automatically (1 log entry)
An everyday payment passed every fixed rule, so it was sent straight away with no human needed.

**Example RES-000001:** Sent automatically: every fixed rule passed.

Record | When | Payment | Action | Change | By | Reasons
---|---|---|---|---|---|---
RES-000001 | 2026-11-21 06:41:03 UTC | TXN-00201 | CLEAR | new → CLEARED | system | ALL_CLEAR: verified campus vendor [RCP-0001]; normal amount 12.00 AED; no scam signals [TXN-00201]

## ⏸️ Risky action paused for a person (1 log entry)
The fixed rules found a risk, so the payment was paused, not sent, and a person must decide. Safe default: if nobody answers within 24 h it expires and the money is returned.

**Example RES-000002:** Paused, not sent: waiting for the student to decide.

Record | When | Payment | Action | Change | By | Reasons
---|---|---|---|---|---|---
RES-000002 | 2026-11-21 06:41:05 UTC | TXN-00202 | HOLD_FOR_CONFIRMATION | new → PENDING_CONFIRMATION | system | NEW_ACCOUNT: 14 days old [RCP-0264]; LOW_TRUST: trust score 5 [RCP-0264]; LARGE_AMOUNT: 300.00 AED &gt; 100 AED automatic limit [TXN-00202]

## 🛡️ Attack blocked (2 log entries)
The attack did nothing: hidden instructions aimed at the AI were removed before any AI read them and the payment could not be sent automatically; changes the rules don't allow, and bad input at entry, were refused. Nothing unsafe happened, and each attempt is recorded.

**Example RES-000004:** A new payment was refused at entry; nothing was saved.

Record | When | Payment | Action | Change | By | Reasons
---|---|---|---|---|---|---
RES-000004 | 2026-11-21 06:41:08 UTC | (not saved) | INTAKE_REJECTED | new → (none) | staff:SUP-01 | INTAKE_REJECTED: Pay to (@handle) must start with @ and use 3–30 lowercase letters, digits, _ . or -; input: student_id='STU-0001', amount_aed='50.0', recipient_handle='@&lt;script&gt;alert(1)&lt;/script&gt;'
RES-000003 | 2026-11-21 06:41:08 UTC | TXN-00203 | ESCALATE | new → ESCALATED | system | REPORTED_RECIPIENT: 4 scam reports [RCP-0261]; PHISHING_PATTERN: unofficial link + pressure [MSG-00101]; HIDDEN_INSTRUCTIONS: in the chat message (clear_command, escalation_suppression, role_prefix, verification_claim) [TXN-00203]; NEW_ACCOUNT: 15 days old [RCP-0261]; LOW_TRUST: trust score 12 [RCP-0261]; SCAM_SIGNALS: CLICK_BAIT, TOO_GOOD, URGENCY [MSG-00101]
