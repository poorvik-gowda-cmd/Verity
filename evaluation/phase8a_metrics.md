# Phase 8A: Classification Metrics and Error Analysis

**Total evaluated cases:** 32
**Overall status accuracy:** 1.0000 (32/32)

## 1. Confusion Matrix

Rows = Actual, Columns = Predicted

| Actual \ Predicted | COMPLETE | INCOMPLETE | NOT_APPLICABLE |
| --- | ---: | ---: | ---: |
| **COMPLETE** | 10 | 0 | 0 |
| **INCOMPLETE** | 0 | 22 | 0 |
| **NOT_APPLICABLE** | 0 | 0 | 0 |

## 2. Per-Class Metrics

| Class | Precision | Recall | F1 | Support | TP | FP | FN |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **COMPLETE** | 1.0000 | 1.0000 | 1.0000 | 10 | 10 | 0 | 0 |
| **INCOMPLETE** | 1.0000 | 1.0000 | 1.0000 | 22 | 22 | 0 | 0 |
| **NOT_APPLICABLE** | 0.0000 | 0.0000 | 0.0000 | 0 | 0 | 0 | 0 |

## 3. Aggregate Averages

| Average | Precision | Recall | F1 |
| --- | ---: | ---: | ---: |
| **Macro** | 0.6667 | 0.6667 | 0.6667 |
| **Weighted** | 1.0000 | 1.0000 | 1.0000 |
| **Micro** | 1.0000 | 1.0000 | 1.0000 |

## 4. Critical Safety Metrics

- **COMPLETE precision:** 1.0000
- **COMPLETE recall:** 1.0000
- **False COMPLETE count:** 0 (predicted COMPLETE, actual ≠ COMPLETE)
- **False INCOMPLETE count:** 0
- **NOT_APPLICABLE misclassification count:** 0

## 5. Status Mismatches

No mismatches — all predictions correct.

## 6. Step-Level Metrics

- **Exact step-sequence accuracy:** 0.0938 (3/32)
- **Exact step-count accuracy:** 0.1250 (4/32)
- **Aggregate step precision:** 0.6364
- **Aggregate step recall:** 0.6528
- **Aggregate step F1:** 0.6445

### Step Matching Rule

A predicted step matches an expected step when they share the same `(system_id, resource_id, operation, state)` 4-tuple.

### Per-Operation Precision/Recall/F1

| Operation | Precision | Recall | F1 | TP | FP | FN |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `ANONYMIZE_RECORD` | 0.8333 | 1.0000 | 0.9091 | 5 | 1 | 0 |
| `BLOCK_RESTORE` | 0.6000 | 1.0000 | 0.7500 | 3 | 2 | 0 |
| `DELETE_RECORD` | 0.6400 | 0.7619 | 0.6957 | 16 | 9 | 5 |
| `DISABLE_DATA_FLOW` | 0.8571 | 1.0000 | 0.9231 | 6 | 1 | 0 |
| `REDACT_RECORD` | 0.4286 | 1.0000 | 0.6000 | 6 | 8 | 0 |
| `REPORT_ACTIVE_DATA_FLOW` | 0.8571 | 1.0000 | 0.9231 | 6 | 1 | 0 |
| `REPORT_EXPOSED_PII` | 0.5500 | 1.0000 | 0.7097 | 11 | 9 | 0 |
| `REPORT_REMAINING_RECORD` | 0.6400 | 0.7619 | 0.6957 | 16 | 9 | 5 |
| `REPORT_RESTORE_RISK` | 0.6000 | 1.0000 | 0.7500 | 3 | 2 | 0 |
| `VERIFY_DATA_FLOW_DISABLED` | 0.9000 | 1.0000 | 0.9474 | 9 | 1 | 0 |
| `VERIFY_RECORD_ABSENT` | 0.7714 | 0.5400 | 0.6353 | 27 | 8 | 23 |
| `VERIFY_RECORD_ANONYMIZED` | 0.8333 | 0.4167 | 0.5556 | 5 | 1 | 7 |
| `VERIFY_RECORD_PRESENT` | 1.0000 | 0.2917 | 0.4516 | 7 | 0 | 17 |
| `VERIFY_RECORD_REDACTED` | 0.5556 | 1.0000 | 0.7143 | 10 | 8 | 0 |
| `VERIFY_RESTORE_BLOCKED` | 0.7500 | 1.0000 | 0.8571 | 6 | 2 | 0 |

## 7. Evidence and Structural Integrity

- **Total evidence references:** 84
- **Valid references:** 84
- **Invalid/unsupported references:** 0
- **Empty references (expected for remediation/proposed):** 114
- **Remediation incorrectly represented as completed:** 0
- **Missing verification evidence (COMPLETE cases):** 0

## 8. Failure Category Analysis

Distribution of likely root causes across status-mismatched cases:

No mismatches to diagnose.

## 9. Per-Case Error Diagnosis

All cases matched — no failures to diagnose.
