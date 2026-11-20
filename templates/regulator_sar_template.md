# Suspicious Activity Report (SAR) — DRAFT

> **Pay Sentinel · Financial Crime Compliance** · Status: **$status**
> Every factual statement cites a source record ID (`CUS-`, `DEV-`, `LGN-`, `WL-`, `TXN-`, `VEL-`).
> Code removes any claim without a valid citation before this report is rendered.

| Field | Value |
|---|---|
| Case ID | $case_id |
| Alert ID / legacy rule | $alert_id / $legacy_rule |
| Generated (UTC) | $generated_at |
| Final route | **$final_route** |
| Human approval required | **$requires_human** |

## 1. Subject — KYC [$kyc_record_id]
$subject_block

## 2. Reported Transaction [$txn_record_id]
$transaction_block

## 3. Payment Memo (untrusted input)
- Injection detected: **$injection_detected**
- Guard rules matched: $matched_rules
- Sanitizer agent verdict: $agent_verdict ($agent_reason)

Original memo (verbatim, quarantined; never shown to the investigator agent):
```text
$memo_original
```
Sanitized memo (the only memo text the investigator saw):
```text
$memo_clean
```

## 4. Evidence Gathered by Code
### 4.1 Devices & Logins
$device_table

### 4.2 Sanctions Screening
$watchlist_block

### 4.3 Velocity (24h)
$velocity_block

## 5. Risk Indicators & Gate Decision (deterministic Python)
$gate_reasons

## 6. Investigator Findings (LLM, citations verified by code)
| # | Finding | Citations |
|---|---|---|
$findings_rows

Claims dropped for invalid citations: $dropped_claims

## 7. Narrative (LLM draft, for officer review)
$narrative

## 8. Compliance Officer Decision
| Officer ID | Decision | Timestamp (UTC) | Note |
|---|---|---|---|
| $officer_id | $officer_decision | $officer_ts | $officer_note |

> This SAR must not be filed, and no account block may be executed, until a Compliance Officer
> records **APPROVE** in the Pay Sentinel console. Code enforces this in `commit_case_resolution()`.

## 9. Audit Trail
$audit_trail
