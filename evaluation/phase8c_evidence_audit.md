# Phase 8C: Forensic Evidence Utilization Audit & Implementation Plan

**Repository:** `c:\Users\HP\hackerrank-orchestrate-october26`  
**Challenge:** HackerRank Orchestrate — *Ghost Data Still Exists*  
**Date:** 2026-10-25  
**Audit Scope:** Pipeline Inspection, Evidence Utilization, Gemini Integration, Error Root Causes, and Prioritized Plan  
**Mode:** Forensic Audit and Planning (Read-Only; Source Code Unmodified)

---

## 1. Executive Summary

This forensic audit evaluates the end-to-end evidence processing pipeline in Tracepoint. It investigates why the full-dataset execution made 0 Gemini API calls, reconciles benchmark evaluation counts (clarifying the 32 sample vs. 518 production case discrepancy and resolving the arithmetic rounding error in `phase8a_metrics.md`), diagnoses the critical safety defect in `case_15` (false COMPLETE), unpacks the six false INCOMPLETE cases (`case_02`, `case_03`, `case_04`, `case_26`, `case_27`, `case_28`), and presents a prioritized remediation roadmap.

### Key Audit Conclusions

1. **Evaluation Scope & Arithmetic Discrepancy Reconciled:**
   - The public ground-truth benchmark contains **32 sample requests** (`dataset/sample_deletion_requests.csv` with golden outputs in `dataset/sample_output.csv`). The production evaluation dataset (`dataset/deletion_requests.csv`) contains **518 requests**, for which no organizer ground-truth answers are provided to participants (evaluations are performed hidden post-submission per `AGENTS.md` §6.1).
   - In `evaluation/phase8a_metrics.md`, the reported string `24/32 (0.7812)` is an integer truncation defect in `code/evaluation/phase8a_evaluator.py` line 695: `int(results['status_accuracy'] * results['total_cases'])` where `results['status_accuracy']` was rounded to 4 decimals (`0.7812`). Calculating `int(0.7812 * 32)` yields `int(24.9984) = 24`. The actual number of status matches is **25 out of 32 (78.125%)**.

2. **Gemini Integration Status (0 API Calls Explained):**
   - The Gemini subsystem (`code/tracepoint/interpretation/`) is fully implemented with client, caching, model schemas, deterministic fallbacks, and token usage tracking.
   - However, **`GeminiInterpreter` is completely unwired from the production entry points**. Neither `code/main.py`, `code/tracepoint/ingestion/pipeline.py`, nor `code/tracepoint/decision/engine.py` imports or instantiates `GeminiInterpreter`.
   - The production pipeline operates in 100% deterministic local mode. While `.env` contains a valid `GEMINI_API_KEY`, no code path in `code/main.py` triggers an API invocation.

3. **Critical Safety Flaw Diagnosed (`case_15` False COMPLETE):**
   - In `case_15` (`lucia-torres@gmail.com`), active data flow `data_flow_9846` remains enabled and is documented as active in `evidence_01` (log event `connector_heartbeat enabled=true`).
   - The pipeline marked `case_15` as `COMPLETE` because:
     a) `TimelineBuilder.build_timeline()` applied a hardcoded `[-15min, +5min]` window around `reported_at` (`2026-08-22T12:00:00Z`), completely missing the `CHECK_DATA_FLOW` event in `system_activity.csv` at `12:51:10Z` (+51 minutes);
     b) Term search in `_evidence_events_for_terms()` only queries direct user identifiers (`email`, `user_id`, hashes). It failed to perform transitive resource expansion, so `data_flow_9846` was never queried in `dataset/evidence.db`.

4. **False INCOMPLETE Root Causes Identified:**
   - **`case_02`:** `account_002` was reported `PARTIAL` in `system_activity.csv`, but `evidence_54` (failover replica SQL) proved it was `ABSENT` at `audit_at`. `ContradictionDetector` treated the historical activity log entry as an irrecoverable contradiction, violating the specification rule that activity logs are merely claims superseded by evidence.
   - **`case_04`:** `analytics_901` in `evidence_56` had `"user_id": null, "email": null`. `_classify_evidence_kind()` only checked for strings like `"customer_reference": null`, missed `"user_id": null`, misclassified the record as `SNAPSHOT_PRESENT`, and triggered spurious `REPORT_EXPOSED_PII` and `ANONYMIZE_RECORD` steps.
   - **`case_26`, `case_27`, `case_28`:** Multi-resource cases verified by global resource export `evidence_92`. The workflow windowing either missed resources that completed outside `[-15min, +5min]` or conflated concurrent user operations executed by `deletion_worker`.

---

## 2. Evidence-to-Decision Architecture Map

The end-to-end data flow operates through 11 distinct stages. Below is the structural map indicating where evidence enters, how it transforms, and where data loss occurs:

```
[dataset/evidence/] (94 Files: 24 SQL, 28 JSON, 26 LOG, 4 PDF, 11 PNG, 1 TXT)
       │
       ▼
[Stage 1: Discovery] ─── pipeline.py::discover_files()
       │                 Associates manifest file_id & captured_at from evidence_files.csv
       ▼
[Stage 2: Dispatcher] ── pipeline.py::ingest_file() (dispatches by file extension)
       │
       ├── .sql  ──► parsers.py::parse_sql_file()      (969 records parsed via in-memory SQLite)
       ├── .json ──► parsers.py::parse_json_file()     (890 records parsed)
       ├── .log  ──► parsers.py::parse_log_file()      (2,211 trace lines parsed)
       ├── .txt  ──► parsers.py::parse_txt_file()      (44 lines parsed)
       ├── .pdf  ──► parsers.py::parse_pdf_file()      (4 stream objects parsed)
       └── .png  ──► parsers.py::parse_image_file()    [GAP 1: 11 images set to UNSUPPORTED, content=""]
       │
       ▼
[Stage 3: Evidence Index] ── indexer.py::EvidenceIndex (dataset/evidence.db: 4,129 items)
       │
       ▼
[Stage 4: Case Intake & Classification] ── main.py + request_classifier.py
       │                                   Filters non-deletion requests (NOT_APPLICABLE, steps=[])
       ▼
[Stage 5: Identity Resolution] ── resolver.py::IdentityResolver
       │                           Matches current email, historical audit log, or SHA-256 hash
       │                           Output: user_id, current_email, historical_emails, email_hashes
       ▼
[Stage 6: Timeline Assembly] ── timeline.py::TimelineBuilder
       │                        Term Search: email, user_id, account_<user_num>
       │                        [GAP 2: Missing transitive resource expansion]
       │                        [GAP 3: Narrow window (t0-15m, t1+5m) drops delayed/early worker events]
       │                        [GAP 4: Conflates concurrent worker events across multiple users]
       ▼
[Stage 7: Contradiction Detection] ── timeline.py::ContradictionDetector
       │                              [GAP 5: Treats historical activity claims as fatal contradictions
       │                               even when subsequent evidence proves compliant state]
       ▼
[Stage 8: Policy Evaluation] ── policy/evaluator.py::PolicyEvaluator
       │                        Evaluates rules from deletion_rules.csv
       │                        [GAP 6: String-matching fails to recognize null PII as ANONYMIZED]
       ▼
[Stage 9: Resurrection Risk Analysis] ── policy/resurrection.py::ResurrectionRiskAnalyzer
       │                                  Checks active data flows and restorable backups
       ▼
[Stage 10: Step Generation] ── decision/step_generator.py + decision/engine.py
       │                        Generates FINDING, REMEDIATION, VERIFICATION phases
       │                        [GAP 7: Hardcoded fallback steps injected when proof is ambiguous]
       ▼
[Stage 11: Validation & CSV Output] ── decision/validator.py -> output.csv (518 rows)
```

---

## 3. Per-Format Extraction and Indexing Audit

The repository contains 94 evidence files in `dataset/evidence/` across 5 directories, all indexed into `dataset/evidence.db` (4,129 rows).

| Format / Category | Discovered | Successfully Parsed | Unsupported / Skipped | Extraction Method | OCR / Vision Invoked | Persisted in Index | Provenance & Metadata Preserved | Downstream Retrieval Status |
|---|---|---|---|---|---|---|---|---|
| **SQL Databases** (`.sql`) | 24 | 24 | 0 | In-memory SQLite execution (`conn.executescript`); schema inspection & row dump | N/A | Yes (969 items) | Full (`source_path`, `evidence_id`, `table`, `columns`, `locator`) | **Partially Retrieved:** Primary identifiers matched; secondary IDs lacking user email missed unless discovered via activity log |
| **JSON Records** (`.json`) | 28 | 28 | 0 | Native `json.loads` parsing snapshots, profile exports, and system state objects | N/A | Yes (890 items) | Full (`source_type`, `locator`, `identifiers`) | **Partially Retrieved:** Objects with `customer_reference: null` or `user_id: null` lose link to case unless resource_id is expanded |
| **Operational Logs** (`.log`) | 26 | 26 | 0 | Regex extraction of ISO timestamps, service, resource_id, and key-value attributes | N/A | Yes (2,211 items) | Full (`line_number`, `system_id`, `resource_id`, `timestamp`) | **Retrieved:** High coverage for direct email and account matches |
| **Plain Text** (`.txt`) | 1 | 1 | 0 | Line-by-line pipe/delimited parsing | N/A | Yes (44 items) | Full (`line_number`, `timestamp`, `system_id`, `resource_id`) | **Retrieved:** Handover notes indexed with extracted identifiers |
| **Selectable PDFs** (`.pdf`) | 4 | 4 | 0 | Stream extraction decoding `/Length`, `FlateDecode`, and `ASCII85Decode` | N/A | Yes (4 items) | Medium (`locator`, `identifiers`, `token_count`) | **Retrieved:** Raw tokens concatenated into single text block per document |
| **Images & Screenshots** (`.png`) | 11 | 0 | 11 | Stat/metadata only; sets `status=UNSUPPORTED` | **NO (Not Invoked)** | Yes (11 items with `content=""`) | Minimal (`file_size_bytes`, `locator="image_file"`) | **Completely Lost:** 11 screenshots cannot be retrieved by text search |

### Concrete Examples of Lost Evidence

1. **Images (`evidence_05`, `evidence_08`, `evidence_14`, etc.):**
   - `dataset/evidence/images/support_ticket_screenshot_20260914.png` contains customer support interactions showing ticket closure and redaction. Because `parse_image_file()` generates empty content (`""`) with `status=UNSUPPORTED`, no identifiers or resource IDs are indexed. Downstream searches never match these files.
2. **Global System Resource Export (`evidence_92`):**
   - `evidence/records/service_resource_export_20260915_114455.json` contains status lookups across 8 enterprise services (accounts returning 404, disabled data flows, blocked backups, redacted support tickets).
   - In `evidence.db`, records for deleted or anonymized profiles have `identifiers=["account_3866"]` with `email=null`. Because the pipeline only searches for the user's email (`ananya-nair@gmail.com`), it fails to retrieve `account_3866` from `evidence_92` unless `account_3866` was already linked via `system_activity.csv`.

---

## 4. Evidence Retrieval and Identity-Correlation Audit

### 4.1 Single-Hop vs. Transitive Retrieval

- `TimelineBuilder._build_search_terms()` constructs queries using:
  `[user_id, account_<user_num>, supplied_email, current_email, historical_emails, email_hashes]`.
- **Defect:** In modern distributed architectures, deleted records no longer contain the email address. Instead, they are indexed under secondary resource identifiers (e.g., `data_flow_9846`, `audit_record_1596`, `backup_5970`).
- Because the pipeline performs only a single-hop search, secondary resources that lack direct email annotations are dropped from the timeline.

### 4.2 Workflow Windowing and Temporal Corruption

In `TimelineBuilder.build_timeline()` (lines 543–580):
```python
if reported_at:
    rep_dt = parse_timestamp(reported_at)
    if rep_dt:
        t0 = rep_dt - timedelta(minutes=15)
        t1 = rep_dt + timedelta(minutes=5)
        batch_mask = (
            (self._activity["source_system"] == "deletion_worker")
            & (self._activity["event_time"].notna())
        )
```

**Flaws:**
1. **Narrow Window:** Deletion workflows in the dataset frequently execute over 1 to 2 hours. A window of `[-15min, +5min]` drops activities occurring before or after that slice.
2. **Cross-Case Pollution:** Any `deletion_worker` activity occurring in that 20-minute window across the *entire enterprise* is ingested into the timeline. If 3 users were processed simultaneously, all 3 users' accounts, data flows, and backups are merged into each individual's timeline.

---

## 5. Case-Level Findings for Representative Examples

### 5.1 `case_15` (Critical Safety Defect: False COMPLETE)
- **Subject:** `lucia-torres@gmail.com` (`user_15`)
- **Reported Status:** `COMPLETE` at `2026-08-22T12:00:00Z` (`audit_at: 2026-09-15T12:00:00Z`)
- **Ground Truth:** `INCOMPLETE` (Active data flow `data_flow_9846` remains enabled)
- **Pipeline Prediction:** `COMPLETE` (3 steps: absent account, present audit record)
- **Forensic Breakdown:**
  1. In `dataset/system_activity.csv` row 3385 (`activity_3386` at `2026-08-22T12:51:10Z`), `CHECK_DATA_FLOW` on `data_flow_9846` reported: *"Connector status check confirmed that data flow data_flow_9846 remains enabled."*
  2. Because `12:51:10Z` is 51 minutes after `reported_at` (`12:00:00Z`), the `t1 = rep_dt + timedelta(minutes=5)` filter excluded `activity_3386`.
  3. In `dataset/evidence/logs/service_events_ap_south_20260915.log` (`evidence_01`), row 88 confirms `data_flow_9846` is active: `event=connector_heartbeat enabled=true scheduler_registered=true`.
  4. Because `data_flow_9846` was never added to search terms, `evidence_01` was never queried for that flow.
  5. The decision engine found 0 violations and 0 hazards, erroneously returning `COMPLETE`.

### 5.2 `case_02` (False INCOMPLETE)
- **Subject:** `elin.lindberg@yahoo.se` (`user_02`)
- **Ground Truth:** `COMPLETE` (1 verification step: `account_002` verified absent in `evidence_54`)
- **Pipeline Prediction:** `INCOMPLETE` (4 steps: 1 finding, 1 remediation, 2 verifications)
- **Forensic Breakdown:**
  1. `system_activity.csv` row 3376 logged `DELETE_RECORD` on `account_002` with reported result `PARTIAL`.
  2. Snapshot `evidence_54` at `2026-09-02T23:00:00Z` verified `account_002` was completely `ABSENT` prior to `audit_at`.
  3. `ContradictionDetector` treated the `PARTIAL` result as a fatal `OPERATION_FAILED` contradiction.
  4. The engine overrode the evidence, marked the case `INCOMPLETE`, and synthesized fallback remediation steps for `account_02`.

### 5.3 `case_04` (False INCOMPLETE)
- **Subject:** `dahl.freja@gmail.com` (`user_04`)
- **Ground Truth:** `COMPLETE` (2 verification steps: `account_004` absent in `evidence_54`, `analytics_901` anonymized in `evidence_56`)
- **Pipeline Prediction:** `INCOMPLETE` (5 steps: finding PII, anonymize remediation, verifications)
- **Forensic Breakdown:**
  1. `evidence_56` contains: `{"resource_id": "analytics_901", "user_id": null, "email": null, "phone": null, "email_hash": null}`.
  2. `_classify_evidence_kind()` checked for `"customer_reference": null` or `"subject_reference": null`. It lacked a check for `"user_id": null, "email": null`.
  3. It defaulted to `EventKind.SNAPSHOT_PRESENT`.
  4. The policy evaluator saw an analytics record present with direct identifiers unresolved and generated unnecessary remediation steps.

### 5.4 `case_26`, `case_27`, `case_28` (False INCOMPLETEs)
- **Ground Truth:** All three cases are `COMPLETE` with 6 verification steps citing `evidence_92`.
- **Pipeline Prediction:** All three predicted `INCOMPLETE` with 12–13 mixed steps.
- **Forensic Breakdown:**
  1. All 3 requests executed on `2026-08-30` between `10:45Z` and `12:00Z`.
  2. `timeline.py` captured batch events for multiple concurrent users in a single bucket.
  3. Resources belonging to `user_26`, `user_27`, and `user_28` (`data_flow_2295`, `data_flow_2337`, `backup_838`, `backup_5970`) were cross-assigned.
  4. Redacted support tickets in `evidence_92` (`support_4707`, `support_2351`) were misclassified as `REPORT_EXPOSED_PII` because the classifier did not recognise partial redactions.

---

## 6. Root-Cause Table for Pipeline Errors

| Case ID(s) | Error Type | Symptoms | Root Cause | Affected File & Function | Defect Status | Proposed Remediation |
|---|---|---|---|---|---|---|
| **case_15** | False COMPLETE (Safety Critical) | Missed active data flow `data_flow_9846`; predicted COMPLETE instead of INCOMPLETE | 1. Narrow workflow window (`[-15m, +5m]`) dropped `activity_3386` (at +51m).<br>2. Absence of transitive resource expansion prevented searching `evidence.db` for `data_flow_9846`. | `code/tracepoint/timeline.py`:<br>`TimelineBuilder.build_timeline()` & `_evidence_events_for_terms()` | **Confirmed Defect** | Expand deletion worker correlation window to `[-60m, +120m]` and iteratively add discovered `resource_id`s to search terms. |
| **case_02** | False INCOMPLETE | Generated unnecessary `DELETE_RECORD` and `REPORT_REMAINING_RECORD` for `account_02` | `ContradictionDetector` treated activity log claim `PARTIAL` as fatal contradiction, ignoring subsequent negative proof in `evidence_54`. | `code/tracepoint/timeline.py`:<br>`ContradictionDetector._detect_failed_operations()` | **Confirmed Defect** | Suppress `OPERATION_FAILED` when subsequent snapshot evidence at or before `audit_at` proves `SNAPSHOT_ABSENT`. |
| **case_04** | False INCOMPLETE | Generated unnecessary `ANONYMIZE_RECORD` on `analytics_901` | `_classify_evidence_kind()` failed to recognise `{"user_id": null, "email": null}` as `SNAPSHOT_ANONYMIZED`. | `code/tracepoint/timeline.py`:<br>`TimelineBuilder._classify_evidence_kind()` | **Confirmed Defect** | Add `"user_id": null` and `"email": null` JSON signatures to `SNAPSHOT_ANONYMIZED` classification rules. |
| **case_26, case_27, case_28** | False INCOMPLETE | Conflated resources between simultaneous users; generated duplicate remediation steps | Batch correlation grouped all `deletion_worker` events in window regardless of user ownership; redaction signatures in `evidence_92` misidentified. | `code/tracepoint/timeline.py`:<br>`TimelineBuilder.build_timeline()` & `_classify_evidence_kind()` | **Confirmed Defect** | Correlate `deletion_worker` batch events by verifying account reference links; classify redacted support records as `SNAPSHOT_REDACTED`. |
| **Global Pipeline** | 0 Gemini Calls | Token usage is 0 across all 518 cases | `GeminiInterpreter` in `code/tracepoint/interpretation/` is completely omitted from `code/main.py` execution graph. | `code/main.py`:<br>`run_pipeline()` & `build_decision_engine()` | **Confirmed Defect** | Wire `GeminiInterpreter` for ambiguous evidence interpretation and multimodal image parsing. |

---

## 7. Gemini and AI Integration Findings

1. **Client & Pipeline Disconnect:**
   - `code/tracepoint/interpretation/client.py` defines `GeminiInterpreter` with caching (`cache.py`), fallback (`fallback.py`), and usage tracking (`usage.py`).
   - `code/main.py` never imports `tracepoint.interpretation`. The pipeline is 100% deterministic local code.
2. **Environment & Secrets:**
   - `.env` contains `GEMINI_API_KEY`. `code/config.py` successfully loads it into `config.gemini_api_key`.
   - No secrets are hardcoded in git-tracked files.
3. **Appropriate AI Boundaries:**
   - **Where Gemini is needed:**
     - **Multimodal Vision:** Interpreting the 11 PNG screenshots in `dataset/evidence/images/` (extracting ticket IDs, account numbers, and redaction states).
     - **Ambiguous Text Documents:** Extracting unstructured claims from PDF/TXT handover records where schema is non-standard.
   - **Where Gemini should NOT be used:**
     - Deletion policy rules, timestamp cutoff comparisons, and schema serialization must remain 100% deterministic to guarantee repeatability, 0 hallucinations, and compliance with challenge rules.

---

## 8. Evaluation Limitations & Verified Baseline Metrics

### 8.1 Reconciling the 32 vs. 518 Dataset Scope

- `dataset/sample_deletion_requests.csv` contains **32 requests** accompanied by `dataset/sample_output.csv` (the official public ground truth).
- `dataset/deletion_requests.csv` contains **518 requests**, representing the competition evaluation test set. There are no participant-visible ground truth labels for these 518 requests.
- All 518 requests are processed end-to-end by `code/main.py`, producing `output.csv` with exactly 518 valid rows sorted by `case_id`.

### 8.2 Correcting the Arithmetic Discrepancy

In `evaluation/phase8a_metrics.md`:
- Reported header: `Overall status accuracy: 0.7812 (24/32)`
- **Actual calculation:**
  - Status matches: 25 cases (4 COMPLETE, 21 INCOMPLETE)
  - Mismatches: 7 cases (`case_02`, `case_03`, `case_04`, `case_15`, `case_26`, `case_27`, `case_28`)
  - Accuracy: $25 / 32 = 0.78125$ (78.125%)
- **Bug Mechanism:** In `phase8a_evaluator.py` line 695: `int(results['status_accuracy'] * results['total_cases'])` evaluated `int(0.7812 * 32) = int(24.9984) = 24`. The correct display is **25/32 (78.12%)**.

### 8.3 Verified Baseline Classification Metrics (32 Sample Benchmark)

```
Confusion Matrix (Actual Rows \ Predicted Columns):
                 COMPLETE    INCOMPLETE    NOT_APPLICABLE
COMPLETE             4            6               0
INCOMPLETE           1           21               0
NOT_APPLICABLE       0            0               0

Per-Class Performance:
  COMPLETE:        Precision = 0.8000, Recall = 0.4000, F1 = 0.5333  (Support = 10)
  INCOMPLETE:      Precision = 0.7778, Recall = 0.9545, F1 = 0.8571  (Support = 22)
  NOT_APPLICABLE:  N/A (Support = 0 in public sample set)

Macro Averages:
  Macro Precision: 0.5259
  Macro Recall:    0.4515
  Macro F1:        0.4859

Critical Safety Metrics:
  False COMPLETEs:    1 (case_15 — DANGEROUS)
  False INCOMPLETEs:  6 (case_02, case_03, case_04, case_26, case_27, case_28)

Step Metrics:
  Exact Sequence Accuracy: 3 / 32 (9.38%)
  Exact Step-Count:        8 / 32 (25.00%)
  Aggregate Step F1:       0.5720 (Precision: 0.4507, Recall: 0.7824)
```

---

## 9. Prioritized Implementation Plan

The plan below is strictly ranked by correctness and safety impact.

```
┌────────────────────────────────────────────────────────────────────────┐
│                      PRIORITIZED ROADMAP                               │
├────────────────────────────────────────────────────────────────────────┤
│ Priority 1 (CRITICAL)  Fix case_15 active data flow & windowing        │
│ Priority 2 (CRITICAL)  Correct snapshot state classification (case_04) │
│ Priority 3 (HIGH)      Evidence precedence over activity logs (case_02)│
│ Priority 4 (HIGH)      Disambiguate concurrent batch events (26-28)    │
│ Priority 5 (MEDIUM)    Wire Gemini Vision for 11 PNG screenshots       │
│ Priority 6 (LOW)       Fix evaluator arithmetic display (25/32)        │
└────────────────────────────────────────────────────────────────────────┘
```

### Priority 1: Prevent False COMPLETE & Expand Resource Correlation (Critical)
- **Defect:** Confirmed defect. `case_15` misses active data flow `data_flow_9846`.
- **Target Files:** `code/tracepoint/timeline.py` (`build_timeline`, `_evidence_events_for_terms`).
- **Approach:**
  1. Expand deletion worker correlation window from `[-15m, +5m]` to `[-60m, +120m]` relative to `reported_at`.
  2. Implement iterative transitive search: when an activity event reveals an associated `resource_id` (e.g., `data_flow_9846`, `account_4745`), immediately search `evidence.db` for that `resource_id`.
- **Regression Test:** Create `tests/test_active_flow_regression.py` asserting that any case with an active data flow event cannot be marked COMPLETE.
- **Success Criteria:** `case_15` transitions from `COMPLETE` to `INCOMPLETE` with `REPORT_ACTIVE_DATA_FLOW` step; False COMPLETE count drops to **0**.

### Priority 2: Fix Null PII Anonymization Classification (Critical)
- **Defect:** Confirmed defect. `analytics_901` in `case_04` misclassified as `SNAPSHOT_PRESENT`.
- **Target Files:** `code/tracepoint/timeline.py` (`_classify_evidence_kind`).
- **Approach:** Add patterns checking for `"user_id": null` and `"email": null` within JSON objects to emit `EventKind.SNAPSHOT_ANONYMIZED`.
- **Success Criteria:** `case_04` transitions from `INCOMPLETE` to `COMPLETE` with exact 2-step verification.

### Priority 3: Enforce Evidence Precedence Over Activity Claims (High)
- **Defect:** Confirmed defect. `case_02` fails because historical `PARTIAL` activity log overrides negative proof snapshot `evidence_54`.
- **Target Files:** `code/tracepoint/timeline.py` (`ContradictionDetector._detect_failed_operations`).
- **Approach:** In `_detect_failed_operations`, check if a subsequent snapshot exists at or before `audit_at` proving `SNAPSHOT_ABSENT`. If absent proof exists, treat the failure as superseded/resolved.
- **Success Criteria:** `case_02` transitions from `INCOMPLETE` to `COMPLETE` citing `evidence_54`.

### Priority 4: Disambiguate Concurrent User Batch Events (High)
- **Defect:** Confirmed defect. Cases 26, 27, and 28 cross-pollinate resources from simultaneous worker runs.
- **Target Files:** `code/tracepoint/timeline.py` (`build_timeline`).
- **Approach:** Validate that batch events linked to a case share a verified identifier link or account foreign key before including them in the timeline.
- **Success Criteria:** Eliminates phantom steps in cases 26, 27, and 28; step precision rises from 0.45 to >0.70.

### Priority 5: Wire Gemini Multimodal Vision for PNG Evidence (Medium)
- **Defect:** Confirmed defect. 11 PNG screenshots are unparsed (`content=""`).
- **Target Files:** `code/tracepoint/ingestion/parsers.py` (`parse_image_file`), `code/tracepoint/interpretation/client.py`.
- **Approach:**
  1. When processing `.png` files, invoke `GeminiInterpreter.extract_from_image()` using `gemini-2.5-flash` with structured JSON schema.
  2. Cache responses in `dataset/interpretation_cache.json` to ensure deterministic offline replay.
- **Success Criteria:** 11 image files parsed into structured evidence items; zero failed/unsupported files in `evidence.db`.

### Priority 6: Correct Metric Reporting Discrepancy (Low)
- **Defect:** Confirmed display bug.
- **Target Files:** `code/evaluation/phase8a_evaluator.py` line 695.
- **Approach:** Replace `int(results['status_accuracy'] * results['total_cases'])` with the exact integer match count `results['status_matches']`.
- **Success Criteria:** Report displays `25/32 (78.12%)`.
