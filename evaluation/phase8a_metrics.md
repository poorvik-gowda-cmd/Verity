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
- **Exact step-count accuracy:** 0.2188 (7/32)
- **Aggregate step precision:** 0.1951
- **Aggregate step recall:** 0.8290
- **Aggregate step F1:** 0.3159

### Step Matching Rule

A predicted step matches an expected step when they share the same `(system_id, resource_id, operation, state)` 4-tuple.

### Per-Operation Precision/Recall/F1

| Operation | Precision | Recall | F1 | TP | FP | FN |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `ANONYMIZE_RECORD` | 1.0000 | 1.0000 | 1.0000 | 5 | 0 | 0 |
| `BLOCK_RESTORE` | 0.0484 | 1.0000 | 0.0923 | 3 | 59 | 0 |
| `DELETE_RECORD` | 0.2159 | 0.9048 | 0.3486 | 19 | 69 | 2 |
| `DISABLE_DATA_FLOW` | 0.8571 | 1.0000 | 0.9231 | 6 | 1 | 0 |
| `REDACT_RECORD` | 0.1250 | 1.0000 | 0.2222 | 6 | 42 | 0 |
| `REPORT_ACTIVE_DATA_FLOW` | 0.8571 | 1.0000 | 0.9231 | 6 | 1 | 0 |
| `REPORT_EXPOSED_PII` | 0.2075 | 1.0000 | 0.3438 | 11 | 42 | 0 |
| `REPORT_REMAINING_RECORD` | 0.2159 | 0.9048 | 0.3486 | 19 | 69 | 2 |
| `REPORT_RESTORE_RISK` | 0.0484 | 1.0000 | 0.0923 | 3 | 59 | 0 |
| `VERIFY_DATA_FLOW_DISABLED` | 0.4500 | 1.0000 | 0.6207 | 9 | 11 | 0 |
| `VERIFY_RECORD_ABSENT` | 0.3380 | 0.9600 | 0.5000 | 48 | 94 | 2 |
| `VERIFY_RECORD_ANONYMIZED` | 0.8571 | 0.5000 | 0.6316 | 6 | 1 | 6 |
| `VERIFY_RECORD_PRESENT` | 0.2353 | 1.0000 | 0.3810 | 24 | 78 | 0 |
| `VERIFY_RECORD_REDACTED` | 0.1852 | 1.0000 | 0.3125 | 10 | 44 | 0 |
| `VERIFY_RESTORE_BLOCKED` | 0.0800 | 1.0000 | 0.1481 | 6 | 69 | 0 |

## 7. Evidence and Structural Integrity

- **Total evidence references:** 263
- **Valid references:** 263
- **Invalid/unsupported references:** 0
- **Empty references (expected for remediation/proposed):** 557
- **Remediation incorrectly represented as completed:** 0
- **Missing verification evidence (COMPLETE cases):** 0

## 8. Failure Category Analysis

Distribution of likely root causes across status-mismatched cases:

No mismatches to diagnose.

## 9. Per-Case Error Diagnosis

All cases matched — no failures to diagnose.
