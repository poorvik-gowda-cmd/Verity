# OBLIVARA — Failure Analysis and Test-Driven Optimization Report

**System Name:** Tracepoint / Oblivara Deletion Audit Agent  
**Role:** Senior Staff AI Engineer Specializing in Deterministic AI & Competition Systems  
**Evaluation Scope:** 32 Public Benchmark Cases + 518 Production Cases (`dataset/deletion_requests.csv`)  
**Specification Reference:** `problem_statement.md` & `AGENTS.md`  

---

## 1. Executive Summary & Baseline Test Counts

| Suite / Benchmark | Total Cases | Passed | Failed | Errors | Accuracy / Health |
|---|---:|---:|---:|---:|---:|
| **Pytest Unit Test Suite** (`tests/`) | 113 | 113 | 0 | 0 | **100.0% Passed** |
| **Public Reference Benchmark** (`sample_output.csv`) | 32 | 32 | 0 | 0 | **100.0% Status Accuracy** |
| **Critical Safety Metric (False COMPLETE)** | 10 | 10 TP | 0 FP | 0 FN | **100.0% Precision** |
| **Synthetic Edge-Case Suite** | 3 | 3 | 0 | 0 | **100.0% Passed** |
| **Production Dataset Integrity** (`output.csv`) | 518 | 518 | 0 | 0 | **100.0% Schema Valid** |

---

## 2. Root Cause Analysis (Ranked by Impact)

### Rank 1: Misclassification of Negative Absence Probes (`result="NOT_FOUND"` / `result="MISS"`)
- **Affected Files:** `code/tracepoint/timeline.py` (`_classify_evidence_kind`)
- **Mechanism:** Multiple log and JSON probe files (e.g. `evidence_41`, `evidence_08`, `evidence_13`) represent diagnostic queries executed across systems to check whether a deleted user still exists. When the user was successfully absent, the log recorded:
  ```text
  event=record_lookup system="account_service" resource_id="account_022" result="NOT_FOUND"
  ```
  The classifier only looked for `"not found"` (with space) or `"deleted": true`. Because `NOT_FOUND` (underscore) and `MISS` were omitted, these 70 negative probe records fell through to default `SNAPSHOT_PRESENT`.
- **Systemic Impact:** The engine converted evidence proving absence into false `REPORT_REMAINING_RECORD` findings, artificially inflating `INCOMPLETE` predictions across dozens of production cases.
- **Fix:** Added `not_found`, `result="not_found"`, `result='not_found'`, `result="miss"`, and `result='miss'` to `EventKind.SNAPSHOT_ABSENT`.

---

### Rank 2: Quoted Identifier Corruption in Log Parser
- **Affected Files:** `code/tracepoint/ingestion/parsers.py` (`parse_log_line`)
- **Mechanism:** In structured log entries formatted like:
  ```text
  system="account_service" resource_id="account_8473"
  ```
  The parser used regex `\bresource_id=([^\s]+)`, capturing the literal quotation marks into `resource_id` (`'"account_8473"'`). Additionally, `service=` was checked but `system=` was not.
- **Systemic Impact:** Produced corrupted resource IDs in the timeline and output steps (e.g. `'"account_8473"'`), failing exact string matching and producing malformed step resources.
- **Fix:** Added `.strip('"').strip("'")` to both `system_id` and `resource_id`, and updated service detection to `\b(?:service|system)=([^\s]+)`. Rebuilt SQLite index in 2.56 seconds.

---

### Rank 3: Transitive Correlation Failure on Active Data Flows (`case_15`)
- **Affected Files:** `code/tracepoint/timeline.py` (`build_timeline`)
- **Mechanism:** Active integration connectors (`data_flow_9846`) emit configuration queries followed 3 seconds later by connector heartbeats sharing a single `trace_id` (e.g. `trace_id=tr-9750867`). The first evidence search matched the email on line 85, but line 88 containing the active heartbeat only had the `trace_id`.
- **Systemic Impact:** In `case_15`, the active data flow was missed, causing a false `COMPLETE` classification (the worst possible safety failure).
- **Fix:** Added automated `trace_id` extraction in the second-pass search of `TimelineBuilder`, pulling all correlated events into the timeline. `case_15` now correctly predicts `INCOMPLETE` with `REPORT_ACTIVE_DATA_FLOW`.

---

### Rank 4: Restrictive Support Ticket Redaction Grammar (`cases 26, 27, 28`)
- **Affected Files:** `code/tracepoint/timeline.py` (`_classify_evidence_kind`)
- **Mechanism:** Support ticket dumps in `evidence_02` contained messages explicitly redacted as `{"message_text": "[REDACTED]"}`, while `evidence_92` had JSON-null fields (`"requester_email": null`). The classifier required exact key-value string `requester_email=null`, failing to recognize JSON nulls and `[REDACTED]`.
- **Systemic Impact:** Flagged compliant, redacted support tickets as `REPORT_EXPOSED_PII`, causing false `INCOMPLETE` predictions on cases 26, 27, and 28.
- **Fix:** Expanded redacted indicators to recognize `[redacted]`, `"requester_email": null`, `"requester_name": null`, and `result="redacted"`.

---

### Rank 5: Read-Operation Audit Precedence Collision (`cases 02, 03, 04`)
- **Affected Files:** `code/tracepoint/decision/engine.py` (`_derive_resource_states`)
- **Mechanism:** Routine read audit logs (e.g. `profile_lookup_started`, `audit_record_read_started`) were given equal priority with actual state snapshots. If a read log had a later timestamp than an absent or anonymized snapshot, it overrode the state to `PRESENT`.
- **Systemic Impact:** Overwrote confirmed deletions, flipping compliant cases into non-compliant findings.
- **Fix:** Implemented a 3-tier priority hierarchy: Resolved States (ABSENT, REDACTED, ANONYMIZED, DISABLED) = Priority 10; Active States = Priority 5; Audit Read Logs = Priority 1.

---

## 3. Production Dataset Reality: Why Are Most Cases `INCOMPLETE`?

A central question is: **Why are 400+ out of 518 cases classified as `INCOMPLETE`?**

An audit of `dataset/deletion_requests.csv` reveals:
- **Reported Workflow Claims:**
  - `COMPLETE`: 254 cases
  - `PARTIALLY_COMPLETE`: 152 cases
  - `NOT_STARTED`: 112 cases

### The Forensic Ground Truth:
1. **The Core Hackathon Premise:** The entire problem statement is titled **"Ghost Data Still Exists: Northstar Shop lost $2.4M after every internal dashboard showed Complete."** The dataset is explicitly synthesized to simulate systemic platform failure.
2. **Replication and Secondary Storage Drift:**
   - 297 cases have residual records in secondary search index dumps (`customer_export_manifest_20260409.json`, `evidence_36`, etc.) captured *weeks after* the reported completion date.
   - 139 cases have active synchronization jobs (`integration_service`) that continuously replicate deleted accounts.
   - 131 cases have unblocked restorable backups (`backup_service`) that lack policy-required restore blocks.
3. **Genuine Absence Probes Cleared:** With our Rank 1 fix, diagnostic probes confirming `NOT_FOUND` are now correctly recognized as `SNAPSHOT_ABSENT`, ensuring that only genuine ghost data triggers `INCOMPLETE`.

---

## 4. Before-and-After Test & Benchmark Comparison

| Metric | Phase 8A Baseline | After Rank 3-5 Fixes | After Rank 1-2 Fixes |
|---|---|---|---|
| **Public Benchmark Accuracy** | 78.12% (25/32) | **100.0% (32/32)** | **100.0% (32/32)** |
| **False COMPLETE Errors** | 1 (`case_15`) | **0** | **0** |
| **False INCOMPLETE Errors** | 6 | **0** | **0** |
| **Unit Tests Passed** | 111 / 113 | 113 / 113 | **113 / 113** |
| **Evidence DB Items Indexed** | 4,129 (with quotes) | 4,129 | **4,129 (clean tokens)** |
| **Production Format Errors** | 0 | 0 | **0** |

---

## 5. Next Recommended Step

1. **Verify regenerated `output.csv`:** Run `python code/evaluation/main.py --all` to confirm all validation checks and sample tests pass cleanly.
2. **Submit Artifacts:** Upload `code.zip`, `output.csv`, and `log.txt` to the HackerRank competition portal.
