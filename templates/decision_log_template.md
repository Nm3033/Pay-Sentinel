# Pay Sentinel · Decision Log

> **PocketPay campus wallet · Scam defense** · Payment state: **$state**
> Every fact below cites a record ID (`TXN-`, `STU-`, `RCP-`, `MSG-`, `DEV-`, `VEL-`, `SPR-`).
> Code removes any AI claim that cites a record which doesn't exist.

| Field | Value |
|---|---|
| Case ID | $case_id |
| Flagged by | $alert_id / old rule $legacy_rule |
| Generated (UTC) | $generated_at |
| Decision | **$final_route** |
| Waiting for | **$awaiting** |

## 1. What the student sees
$student_explanation

## 2. The payment [$txn_record_id]
$transaction_block

## 3. The chat message and note (untrusted input)
- Hidden instructions to the AI found: **$injection_detected** ($matched_rules)
- Scam signs found by the fixed scanner: $scam_signals
- Slang the rules couldn't read: $unclear_terms
- AI reading: $ai_reading

Original message (verbatim, quarantined):
```text
$message_original
```
What the AI was allowed to read:
```text
$message_clean
```
Payment note (verbatim): `$note_original`

## 4. Evidence from the tools
### 4.1 get_transaction_context()
$context_block

### 4.2 verify_campus_recipient()
$recipient_block

### 4.3 Reach of this recipient (campaign check)
$spread_block

## 5. Fixed payment rules (deterministic Python)
$gate_reasons

## 6. Findings (AI, citations verified by code)
| # | Finding | Citations |
|---|---|---|
$findings_rows

Claims dropped for invalid citations: $dropped_claims

**Summary for support staff:** $staff_summary

## 7. Human decision
| Who | Action | Timestamp (UTC) | Note |
|---|---|---|---|
| $human_actor | $human_action | $human_ts | $human_note |

> Pay Sentinel can only **clear**, **hold for confirmation** or **escalate**. It can never freeze a wallet.
> Paused payments are released or cancelled only by the student or signed-in support staff
> (enforced in `update_payment_state()`).

## 8. Step-by-step log
$audit_trail
