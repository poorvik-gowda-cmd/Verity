"""Phase 8A: Proper Classification Metrics and Error Analysis.

Computes:
 - Confusion matrix with actual vs predicted classes.
 - Per-class precision, recall, F1 (COMPLETE, INCOMPLETE, NOT_APPLICABLE).
 - Macro, weighted, and micro averages.
 - Critical safety metrics (false COMPLETE, false INCOMPLETE, NA misclass).
 - Exact step-sequence accuracy and step-count accuracy.
 - Per-step operation matching with precision/recall/F1.
 - Evidence-reference validity.
 - Per-case failure diagnosis with root-cause categories.

Outputs:
 - evaluation/phase8a_metrics.md     — human-readable report
 - evaluation/phase8a_errors.json    — machine-readable per-case error detail
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

# Path setup
code_dir = Path(__file__).resolve().parent.parent
repo_root = code_dir.parent
for p in [str(code_dir), str(repo_root)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from config import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("tracepoint.phase8a")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STATUS_CLASSES = ["COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"]

FAILURE_CATEGORIES = [
    "Identity resolution",
    "Missing or incorrectly parsed evidence",
    "Timestamp/timeline logic",
    "Contradiction detection",
    "Retention policy",
    "Active restoration risk",
    "Decision-engine logic",
    "Step generation or ordering",
    "Ground-truth ambiguity",
]

FINDING_OPS = {
    "REPORT_REMAINING_RECORD",
    "REPORT_EXPOSED_PII",
    "REPORT_ACTIVE_DATA_FLOW",
    "REPORT_RESTORE_RISK",
}
REMEDIATION_OPS = {
    "DISABLE_DATA_FLOW",
    "BLOCK_RESTORE",
    "DELETE_RECORD",
    "REDACT_RECORD",
    "ANONYMIZE_RECORD",
}
VERIFICATION_OPS = {
    "VERIFY_RECORD_ABSENT",
    "VERIFY_RECORD_PRESENT",
    "VERIFY_RECORD_REDACTED",
    "VERIFY_RECORD_ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED",
    "VERIFY_RESTORE_BLOCKED",
}

# Load known evidence IDs once
_evidence_files_csv = config.evidence_files_csv
_system_activity_csv = config.system_activity_csv


def _load_known_evidence_ids() -> Set[str]:
    """Load valid evidence IDs from manifest and activity log."""
    ids: Set[str] = set()
    if _evidence_files_csv.exists():
        df = pd.read_csv(_evidence_files_csv)
        if "file_id" in df.columns:
            ids.update(df["file_id"].dropna().astype(str).unique())
    if _system_activity_csv.exists():
        df = pd.read_csv(_system_activity_csv)
        if "activity_id" in df.columns:
            ids.update(df["activity_id"].dropna().astype(str).unique())
    return ids


# ---------------------------------------------------------------------------
# Metric Computation Helpers
# ---------------------------------------------------------------------------

def safe_div(num: float, den: float, zero_val: float = 0.0) -> float:
    return num / den if den > 0 else zero_val


def compute_confusion_matrix(
    actual: List[str], predicted: List[str], classes: List[str]
) -> Dict[str, Dict[str, int]]:
    """Row = actual, Col = predicted."""
    matrix: Dict[str, Dict[str, int]] = {c: {c2: 0 for c2 in classes} for c in classes}
    for a, p in zip(actual, predicted):
        if a in matrix and p in matrix[a]:
            matrix[a][p] += 1
    return matrix


def per_class_metrics(
    actual: List[str], predicted: List[str], classes: List[str]
) -> Dict[str, Dict[str, float]]:
    """Compute precision, recall, F1, support for each class."""
    results: Dict[str, Dict[str, float]] = {}
    for cls in classes:
        tp = sum(1 for a, p in zip(actual, predicted) if a == cls and p == cls)
        fp = sum(1 for a, p in zip(actual, predicted) if a != cls and p == cls)
        fn = sum(1 for a, p in zip(actual, predicted) if a == cls and p != cls)
        support = sum(1 for a in actual if a == cls)

        precision = safe_div(tp, tp + fp)
        recall = safe_div(tp, tp + fn)
        f1 = safe_div(2 * precision * recall, precision + recall)

        results[cls] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": support,
            "tp": tp,
            "fp": fp,
            "fn": fn,
        }
    return results


def macro_avg(class_metrics: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    n = len(class_metrics)
    if n == 0:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    p = sum(m["precision"] for m in class_metrics.values()) / n
    r = sum(m["recall"] for m in class_metrics.values()) / n
    f = safe_div(2 * p * r, p + r)
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4)}


def weighted_avg(class_metrics: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    total_support = sum(m["support"] for m in class_metrics.values())
    if total_support == 0:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    p = sum(m["precision"] * m["support"] for m in class_metrics.values()) / total_support
    r = sum(m["recall"] * m["support"] for m in class_metrics.values()) / total_support
    f = safe_div(2 * p * r, p + r)
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4)}


def micro_avg(class_metrics: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    total_tp = sum(m["tp"] for m in class_metrics.values())
    total_fp = sum(m["fp"] for m in class_metrics.values())
    total_fn = sum(m["fn"] for m in class_metrics.values())
    p = safe_div(total_tp, total_tp + total_fp)
    r = safe_div(total_tp, total_tp + total_fn)
    f = safe_div(2 * p * r, p + r)
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4)}


# ---------------------------------------------------------------------------
# Step-Level Analysis
# ---------------------------------------------------------------------------

def parse_steps(steps_json_str: str) -> List[Dict[str, Any]]:
    """Parse steps_json from CSV into list of dicts."""
    try:
        parsed = json.loads(steps_json_str)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def step_signature(step: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """(system_id, resource_id, operation, state) tuple for matching."""
    return (
        str(step.get("system_id", "")),
        str(step.get("resource_id", "")),
        str(step.get("operation", "")),
        str(step.get("state", "")),
    )


def compare_step_sets(
    expected_steps: List[Dict[str, Any]],
    predicted_steps: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compare two step lists using set-based signature matching.

    Matching rule: A predicted step matches an expected step if they share
    the same (system_id, resource_id, operation, state) 4-tuple.
    """
    exp_sigs = [step_signature(s) for s in expected_steps]
    pred_sigs = [step_signature(s) for s in predicted_steps]

    exp_counter = Counter(exp_sigs)
    pred_counter = Counter(pred_sigs)

    all_sigs = set(exp_sigs) | set(pred_sigs)

    tp = sum(min(exp_counter[s], pred_counter[s]) for s in all_sigs)
    fp = sum(max(0, pred_counter[s] - exp_counter[s]) for s in all_sigs)
    fn = sum(max(0, exp_counter[s] - pred_counter[s]) for s in all_sigs)

    precision = safe_div(tp, tp + fp)
    recall = safe_div(tp, tp + fn)
    f1 = safe_div(2 * precision * recall, precision + recall)

    matched = set(exp_counter.keys()) & set(pred_counter.keys())
    only_expected = set(exp_counter.keys()) - set(pred_counter.keys())
    only_predicted = set(pred_counter.keys()) - set(exp_counter.keys())

    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "matched_sigs": [list(s) for s in matched],
        "missing_sigs": [list(s) for s in only_expected],
        "extra_sigs": [list(s) for s in only_predicted],
    }


def analyze_evidence_validity(
    steps: List[Dict[str, Any]], known_ids: Set[str]
) -> Dict[str, Any]:
    """Check whether evidence_id references in steps are valid."""
    total_refs = 0
    valid_refs = 0
    invalid_refs = 0
    empty_refs = 0
    invalid_ids: List[str] = []

    for step in steps:
        eid = str(step.get("evidence_id", ""))
        if not eid:
            empty_refs += 1
            continue
        total_refs += 1
        if eid in known_ids:
            valid_refs += 1
        else:
            invalid_refs += 1
            invalid_ids.append(eid)

    return {
        "total_evidence_refs": total_refs,
        "valid_refs": valid_refs,
        "invalid_refs": invalid_refs,
        "empty_refs": empty_refs,
        "invalid_ids": invalid_ids[:20],
    }


def check_remediation_as_completed(
    steps: List[Dict[str, Any]], status: str
) -> bool:
    """Check if remediation steps are incorrectly represented as completed.

    In INCOMPLETE cases, remediation steps should have empty evidence_id
    (they are proposed future work). If they have non-empty evidence_id,
    that suggests they're being represented as already completed.
    """
    if status != "INCOMPLETE":
        return False
    for step in steps:
        phase = step.get("phase", "")
        eid = str(step.get("evidence_id", ""))
        if phase == "REMEDIATION" and eid:
            return True
    return False


# ---------------------------------------------------------------------------
# Failure Diagnosis
# ---------------------------------------------------------------------------

def diagnose_failure(
    case_id: str,
    actual_status: str,
    predicted_status: str,
    actual_steps: List[Dict[str, Any]],
    predicted_steps: List[Dict[str, Any]],
) -> List[str]:
    """Identify likely failure categories for a mismatched case."""
    categories: List[str] = []

    # Status mismatch analysis
    if actual_status != predicted_status:
        if actual_status == "NOT_APPLICABLE" or predicted_status == "NOT_APPLICABLE":
            categories.append("Decision-engine logic")
            # If one is NA and the other isn't, it's likely a request classification issue
            if actual_status == "NOT_APPLICABLE":
                categories.append("Missing or incorrectly parsed evidence")
            else:
                categories.append("Missing or incorrectly parsed evidence")

        if actual_status == "COMPLETE" and predicted_status == "INCOMPLETE":
            # We predicted INCOMPLETE when it should be COMPLETE
            # Possible causes: over-reporting violations, missed proof of compliance
            categories.append("Missing or incorrectly parsed evidence")
            categories.append("Contradiction detection")

        if actual_status == "INCOMPLETE" and predicted_status == "COMPLETE":
            # We predicted COMPLETE when it should be INCOMPLETE — safety issue
            categories.append("Missing or incorrectly parsed evidence")
            categories.append("Active restoration risk")

    # Step-level analysis even for status matches
    if actual_steps and predicted_steps:
        actual_ops = {step_signature(s) for s in actual_steps}
        pred_ops = {step_signature(s) for s in predicted_steps}

        missing = actual_ops - pred_ops
        extra = pred_ops - actual_ops

        # Check for identity-related issues
        actual_resources = {s.get("resource_id", "") for s in actual_steps}
        pred_resources = {s.get("resource_id", "") for s in predicted_steps}
        if actual_resources != pred_resources:
            if "Identity resolution" not in categories:
                categories.append("Identity resolution")

        # Check for system-level mismatches
        actual_systems = {s.get("system_id", "") for s in actual_steps}
        pred_systems = {s.get("system_id", "") for s in predicted_steps}
        missing_systems = actual_systems - pred_systems
        if missing_systems:
            if "Missing or incorrectly parsed evidence" not in categories:
                categories.append("Missing or incorrectly parsed evidence")

        # Check for phase ordering issues
        actual_phases = [s.get("phase", "") for s in actual_steps]
        pred_phases = [s.get("phase", "") for s in predicted_steps]
        if actual_phases != pred_phases and len(actual_steps) == len(predicted_steps):
            if "Step generation or ordering" not in categories:
                categories.append("Step generation or ordering")

        # Check for backup/restore/flow operations in expected but not predicted
        for sig in missing:
            op = sig[2]
            if op in ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"):
                if "Active restoration risk" not in categories:
                    categories.append("Active restoration risk")
            if op in ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"):
                if "Active restoration risk" not in categories:
                    categories.append("Active restoration risk")
            if op in ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"):
                if "Retention policy" not in categories:
                    categories.append("Retention policy")
            if op in ("ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"):
                if "Retention policy" not in categories:
                    categories.append("Retention policy")
            if op == "VERIFY_RECORD_PRESENT":
                if "Retention policy" not in categories:
                    categories.append("Retention policy")

        # Check evidence citations
        actual_eids = {s.get("evidence_id", "") for s in actual_steps if s.get("evidence_id")}
        pred_eids = {s.get("evidence_id", "") for s in predicted_steps if s.get("evidence_id")}
        if actual_eids != pred_eids:
            if "Missing or incorrectly parsed evidence" not in categories:
                categories.append("Missing or incorrectly parsed evidence")

    elif actual_steps and not predicted_steps:
        categories.append("Step generation or ordering")
    elif not actual_steps and predicted_steps:
        categories.append("Step generation or ordering")

    if not categories:
        categories.append("Ground-truth ambiguity")

    return list(dict.fromkeys(categories))  # deduplicate preserving order


# ---------------------------------------------------------------------------
# Main Evaluation
# ---------------------------------------------------------------------------

def run_phase8a_evaluation(
    predictions_csv: Optional[Path] = None,
    sample_requests_csv: Optional[Path] = None,
    sample_output_csv: Optional[Path] = None,
    run_pipeline_first: bool = True,
) -> Dict[str, Any]:
    """Run the complete Phase 8A evaluation.

    Parameters
    ----------
    predictions_csv : Path
        Where to write/read pipeline predictions on sample cases.
    sample_requests_csv : Path
        sample_deletion_requests.csv
    sample_output_csv : Path
        sample_output.csv with ground truth
    run_pipeline_first : bool
        If True, run the pipeline on sample requests first.

    Returns
    -------
    Full evaluation results dict.
    """
    sample_requests_csv = sample_requests_csv or config.sample_deletion_requests_csv
    sample_output_csv = sample_output_csv or config.sample_output_csv
    predictions_csv = predictions_csv or (repo_root / "scratch" / "phase8a_predictions.csv")

    known_evidence_ids = _load_known_evidence_ids()

    # Step 1: Run pipeline on sample requests
    if run_pipeline_first:
        logger.info("Running pipeline on sample requests to generate predictions...")
        from main import run_pipeline as _run_pipeline
        _run_pipeline(
            input_csv=sample_requests_csv,
            output_csv=predictions_csv,
            force_reindex=False,
        )
    else:
        if not predictions_csv.exists():
            raise FileNotFoundError(
                f"Predictions CSV not found: {predictions_csv}. "
                "Set run_pipeline_first=True or provide an existing predictions file."
            )

    # Step 2: Load ground truth and predictions
    df_gt = pd.read_csv(sample_output_csv, dtype=str).set_index("case_id")
    df_pred = pd.read_csv(predictions_csv, dtype=str).set_index("case_id")

    # Align by case_id
    common_ids = sorted(set(df_gt.index) & set(df_pred.index))
    if len(common_ids) != len(df_gt):
        logger.warning(
            "Missing predictions for %d cases",
            len(df_gt) - len(common_ids),
        )

    actual_statuses = [df_gt.loc[cid, "deletion_status"] for cid in common_ids]
    predicted_statuses = [df_pred.loc[cid, "deletion_status"] for cid in common_ids]
    total = len(common_ids)

    # ----- 1. Status-Level Metrics -----
    accuracy = safe_div(
        sum(1 for a, p in zip(actual_statuses, predicted_statuses) if a == p),
        total,
    )

    cm = compute_confusion_matrix(actual_statuses, predicted_statuses, STATUS_CLASSES)
    cls_metrics = per_class_metrics(actual_statuses, predicted_statuses, STATUS_CLASSES)
    macro = macro_avg(cls_metrics)
    weighted = weighted_avg(cls_metrics)
    micro = micro_avg(cls_metrics)

    # ----- 2. Critical Safety Metrics -----
    false_complete = []  # predicted COMPLETE but actual != COMPLETE
    false_incomplete = []  # predicted INCOMPLETE but actual != INCOMPLETE
    na_misclass = []  # NOT_APPLICABLE misclassified in either direction

    mismatches = []
    for cid, actual, pred in zip(common_ids, actual_statuses, predicted_statuses):
        if actual != pred:
            mismatches.append({
                "case_id": cid,
                "actual": actual,
                "predicted": pred,
            })
        if pred == "COMPLETE" and actual != "COMPLETE":
            false_complete.append(cid)
        if pred == "INCOMPLETE" and actual != "INCOMPLETE":
            false_incomplete.append(cid)
        if (actual == "NOT_APPLICABLE" and pred != "NOT_APPLICABLE") or \
           (actual != "NOT_APPLICABLE" and pred == "NOT_APPLICABLE"):
            na_misclass.append(cid)

    safety = {
        "complete_precision": cls_metrics.get("COMPLETE", {}).get("precision", 0.0),
        "complete_recall": cls_metrics.get("COMPLETE", {}).get("recall", 0.0),
        "false_complete_count": len(false_complete),
        "false_complete_case_ids": false_complete,
        "false_incomplete_count": len(false_incomplete),
        "false_incomplete_case_ids": false_incomplete,
        "na_misclassification_count": len(na_misclass),
        "na_misclassification_case_ids": na_misclass,
    }

    # ----- 3. Step-Level Metrics -----
    step_count_matches = 0
    exact_sequence_matches = 0
    total_step_tp = 0
    total_step_fp = 0
    total_step_fn = 0
    per_op_tp: Counter = Counter()
    per_op_fp: Counter = Counter()
    per_op_fn: Counter = Counter()
    evidence_validity_totals = {
        "total_refs": 0,
        "valid": 0,
        "invalid": 0,
        "empty": 0,
        "unsupported_ids": [],
    }
    remediation_as_completed_count = 0
    missing_verification_evidence_count = 0

    per_case_step_results: Dict[str, Dict] = {}

    for cid in common_ids:
        gt_steps = parse_steps(df_gt.loc[cid, "steps_json"])
        pred_steps = parse_steps(df_pred.loc[cid, "steps_json"])
        pred_status = df_pred.loc[cid, "deletion_status"]

        # Step count match
        if len(gt_steps) == len(pred_steps):
            step_count_matches += 1

        # Exact sequence match (order-sensitive, compare all fields)
        if gt_steps == pred_steps:
            exact_sequence_matches += 1

        # Set-based matching
        comparison = compare_step_sets(gt_steps, pred_steps)
        total_step_tp += comparison["tp"]
        total_step_fp += comparison["fp"]
        total_step_fn += comparison["fn"]

        # Per-operation matching
        gt_op_counter: Counter = Counter()
        pred_op_counter: Counter = Counter()
        for s in gt_steps:
            gt_op_counter[s.get("operation", "")] += 1
        for s in pred_steps:
            pred_op_counter[s.get("operation", "")] += 1

        all_ops = set(gt_op_counter.keys()) | set(pred_op_counter.keys())
        for op in all_ops:
            matched = min(gt_op_counter[op], pred_op_counter[op])
            per_op_tp[op] += matched
            per_op_fp[op] += max(0, pred_op_counter[op] - gt_op_counter[op])
            per_op_fn[op] += max(0, gt_op_counter[op] - pred_op_counter[op])

        # Evidence validity on predictions
        ev_result = analyze_evidence_validity(pred_steps, known_evidence_ids)
        evidence_validity_totals["total_refs"] += ev_result["total_evidence_refs"]
        evidence_validity_totals["valid"] += ev_result["valid_refs"]
        evidence_validity_totals["invalid"] += ev_result["invalid_refs"]
        evidence_validity_totals["empty"] += ev_result["empty_refs"]
        evidence_validity_totals["unsupported_ids"].extend(ev_result["invalid_ids"])

        # Remediation as completed check
        if check_remediation_as_completed(pred_steps, pred_status):
            remediation_as_completed_count += 1

        # Missing verification evidence for COMPLETE
        if pred_status == "COMPLETE":
            for s in pred_steps:
                if s.get("phase") == "VERIFICATION" and not s.get("evidence_id"):
                    missing_verification_evidence_count += 1

        per_case_step_results[cid] = comparison

    step_precision = safe_div(total_step_tp, total_step_tp + total_step_fp)
    step_recall = safe_div(total_step_tp, total_step_tp + total_step_fn)
    step_f1 = safe_div(2 * step_precision * step_recall, step_precision + step_recall)

    # Per-operation metrics
    all_operations = sorted(set(per_op_tp.keys()) | set(per_op_fp.keys()) | set(per_op_fn.keys()))
    per_op_metrics: Dict[str, Dict[str, float]] = {}
    for op in all_operations:
        tp = per_op_tp[op]
        fp = per_op_fp[op]
        fn = per_op_fn[op]
        p = safe_div(tp, tp + fp)
        r = safe_div(tp, tp + fn)
        f = safe_div(2 * p * r, p + r)
        per_op_metrics[op] = {
            "precision": round(p, 4),
            "recall": round(r, 4),
            "f1": round(f, 4),
            "tp": tp,
            "fp": fp,
            "fn": fn,
        }

    step_metrics = {
        "step_count_accuracy": round(safe_div(step_count_matches, total), 4),
        "step_count_matches": step_count_matches,
        "exact_sequence_accuracy": round(safe_div(exact_sequence_matches, total), 4),
        "exact_sequence_matches": exact_sequence_matches,
        "aggregate_step_precision": round(step_precision, 4),
        "aggregate_step_recall": round(step_recall, 4),
        "aggregate_step_f1": round(step_f1, 4),
        "per_operation_metrics": per_op_metrics,
        "evidence_validity": evidence_validity_totals,
        "remediation_as_completed_count": remediation_as_completed_count,
        "missing_verification_evidence_count": missing_verification_evidence_count,
    }

    # ----- 4. Per-Case Error Report -----
    per_case_errors: List[Dict[str, Any]] = []

    for cid in common_ids:
        actual = df_gt.loc[cid, "deletion_status"]
        pred = df_pred.loc[cid, "deletion_status"]
        gt_steps = parse_steps(df_gt.loc[cid, "steps_json"])
        pred_steps = parse_steps(df_pred.loc[cid, "steps_json"])

        status_match = (actual == pred)
        step_comparison = per_case_step_results.get(cid, {})

        failure_cats = []
        if not status_match or step_comparison.get("fn", 0) > 0 or step_comparison.get("fp", 0) > 0:
            failure_cats = diagnose_failure(cid, actual, pred, gt_steps, pred_steps)

        per_case_errors.append({
            "case_id": cid,
            "actual_status": actual,
            "predicted_status": pred,
            "status_match": status_match,
            "expected_step_count": len(gt_steps),
            "predicted_step_count": len(pred_steps),
            "step_tp": step_comparison.get("tp", 0),
            "step_fp": step_comparison.get("fp", 0),
            "step_fn": step_comparison.get("fn", 0),
            "step_precision": step_comparison.get("precision", 0),
            "step_recall": step_comparison.get("recall", 0),
            "step_f1": step_comparison.get("f1", 0),
            "failure_categories": failure_cats,
            "missing_steps": step_comparison.get("missing_sigs", []),
            "extra_steps": step_comparison.get("extra_sigs", []),
        })

    # ----- Aggregate failure-category distribution -----
    cat_counts: Counter = Counter()
    for case_err in per_case_errors:
        if not case_err["status_match"]:
            for cat in case_err["failure_categories"]:
                cat_counts[cat] += 1

    results = {
        "total_cases": total,
        "status_accuracy": round(accuracy, 4),
        "confusion_matrix": cm,
        "per_class_metrics": cls_metrics,
        "macro_avg": macro,
        "weighted_avg": weighted,
        "micro_avg": micro,
        "safety_metrics": safety,
        "step_metrics": step_metrics,
        "mismatches": mismatches,
        "mismatch_count": len(mismatches),
        "failure_category_distribution": dict(cat_counts.most_common()),
        "per_case_errors": per_case_errors,
    }

    return results


# ---------------------------------------------------------------------------
# Report Generation
# ---------------------------------------------------------------------------

def generate_markdown_report(results: Dict[str, Any], output_path: Path) -> None:
    """Write evaluation/phase8a_metrics.md."""
    lines: List[str] = []
    a = lines.append

    a("# Phase 8A: Classification Metrics and Error Analysis")
    a("")
    a(f"**Total evaluated cases:** {results['total_cases']}")
    a(f"**Overall status accuracy:** {results['status_accuracy']:.4f} "
      f"({int(results['status_accuracy'] * results['total_cases'])}/{results['total_cases']})")
    a("")

    # --- Confusion Matrix ---
    a("## 1. Confusion Matrix")
    a("")
    a("Rows = Actual, Columns = Predicted")
    a("")
    cm = results["confusion_matrix"]
    classes = STATUS_CLASSES
    header = "| Actual \\ Predicted | " + " | ".join(classes) + " |"
    sep = "| --- | " + " | ".join(["---:"] * len(classes)) + " |"
    a(header)
    a(sep)
    for cls in classes:
        row_vals = " | ".join(str(cm[cls][c]) for c in classes)
        a(f"| **{cls}** | {row_vals} |")
    a("")

    # --- Per-Class Metrics ---
    a("## 2. Per-Class Metrics")
    a("")
    a("| Class | Precision | Recall | F1 | Support | TP | FP | FN |")
    a("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    pcm = results["per_class_metrics"]
    for cls in classes:
        m = pcm.get(cls, {})
        a(f"| **{cls}** | {m.get('precision', 0):.4f} | {m.get('recall', 0):.4f} | "
          f"{m.get('f1', 0):.4f} | {m.get('support', 0)} | {m.get('tp', 0)} | {m.get('fp', 0)} | {m.get('fn', 0)} |")
    a("")

    # --- Averages ---
    a("## 3. Aggregate Averages")
    a("")
    a("| Average | Precision | Recall | F1 |")
    a("| --- | ---: | ---: | ---: |")
    for name, key in [("Macro", "macro_avg"), ("Weighted", "weighted_avg"), ("Micro", "micro_avg")]:
        avg = results[key]
        a(f"| **{name}** | {avg['precision']:.4f} | {avg['recall']:.4f} | {avg['f1']:.4f} |")
    a("")

    # --- Critical Safety Metrics ---
    a("## 4. Critical Safety Metrics")
    a("")
    safety = results["safety_metrics"]
    a(f"- **COMPLETE precision:** {safety['complete_precision']:.4f}")
    a(f"- **COMPLETE recall:** {safety['complete_recall']:.4f}")
    a(f"- **False COMPLETE count:** {safety['false_complete_count']} "
      f"(predicted COMPLETE, actual ≠ COMPLETE)")
    if safety["false_complete_case_ids"]:
        a(f"  - Case IDs: {', '.join(safety['false_complete_case_ids'])}")
    a(f"- **False INCOMPLETE count:** {safety['false_incomplete_count']}")
    if safety["false_incomplete_case_ids"]:
        a(f"  - Case IDs: {', '.join(safety['false_incomplete_case_ids'])}")
    a(f"- **NOT_APPLICABLE misclassification count:** {safety['na_misclassification_count']}")
    if safety["na_misclassification_case_ids"]:
        a(f"  - Case IDs: {', '.join(safety['na_misclassification_case_ids'])}")
    a("")

    # --- Mismatches ---
    a("## 5. Status Mismatches")
    a("")
    if results["mismatches"]:
        a("| Case ID | Actual | Predicted |")
        a("| --- | --- | --- |")
        for mm in results["mismatches"]:
            a(f"| {mm['case_id']} | {mm['actual']} | {mm['predicted']} |")
    else:
        a("No mismatches — all predictions correct.")
    a("")

    # --- Step-Level Metrics ---
    sm = results["step_metrics"]
    a("## 6. Step-Level Metrics")
    a("")
    a(f"- **Exact step-sequence accuracy:** {sm['exact_sequence_accuracy']:.4f} "
      f"({sm['exact_sequence_matches']}/{results['total_cases']})")
    a(f"- **Exact step-count accuracy:** {sm['step_count_accuracy']:.4f} "
      f"({sm['step_count_matches']}/{results['total_cases']})")
    a(f"- **Aggregate step precision:** {sm['aggregate_step_precision']:.4f}")
    a(f"- **Aggregate step recall:** {sm['aggregate_step_recall']:.4f}")
    a(f"- **Aggregate step F1:** {sm['aggregate_step_f1']:.4f}")
    a("")

    a("### Step Matching Rule")
    a("")
    a("A predicted step matches an expected step when they share the same "
      "`(system_id, resource_id, operation, state)` 4-tuple.")
    a("")

    # --- Per-Operation Metrics ---
    a("### Per-Operation Precision/Recall/F1")
    a("")
    a("| Operation | Precision | Recall | F1 | TP | FP | FN |")
    a("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for op, om in sorted(sm["per_operation_metrics"].items()):
        a(f"| `{op}` | {om['precision']:.4f} | {om['recall']:.4f} | "
          f"{om['f1']:.4f} | {om['tp']} | {om['fp']} | {om['fn']} |")
    a("")

    # --- Evidence and Structural Checks ---
    a("## 7. Evidence and Structural Integrity")
    a("")
    ev = sm["evidence_validity"]
    a(f"- **Total evidence references:** {ev['total_refs']}")
    a(f"- **Valid references:** {ev['valid']}")
    a(f"- **Invalid/unsupported references:** {ev['invalid']}")
    a(f"- **Empty references (expected for remediation/proposed):** {ev['empty']}")
    if ev.get("unsupported_ids"):
        uniq = list(set(ev["unsupported_ids"]))[:10]
        a(f"- **Sample unsupported IDs:** {', '.join(uniq)}")
    a(f"- **Remediation incorrectly represented as completed:** {sm['remediation_as_completed_count']}")
    a(f"- **Missing verification evidence (COMPLETE cases):** {sm['missing_verification_evidence_count']}")
    a("")

    # --- Failure Diagnosis ---
    a("## 8. Failure Category Analysis")
    a("")
    a("Distribution of likely root causes across status-mismatched cases:")
    a("")
    fcd = results.get("failure_category_distribution", {})
    if fcd:
        a("| Category | Count |")
        a("| --- | ---: |")
        for cat, count in sorted(fcd.items(), key=lambda x: -x[1]):
            a(f"| {cat} | {count} |")
    else:
        a("No mismatches to diagnose.")
    a("")

    # --- Per-Case Failure Diagnosis ---
    a("## 9. Per-Case Error Diagnosis")
    a("")
    error_cases = [e for e in results["per_case_errors"] if not e["status_match"]]
    if error_cases:
        for err in error_cases:
            a(f"### {err['case_id']}")
            a(f"- **Actual:** `{err['actual_status']}` → **Predicted:** `{err['predicted_status']}`")
            a(f"- **Expected steps:** {err['expected_step_count']}, **Predicted steps:** {err['predicted_step_count']}")
            a(f"- **Step match:** P={err['step_precision']:.2f} R={err['step_recall']:.2f} F1={err['step_f1']:.2f}")
            a(f"- **Failure categories:** {', '.join(err['failure_categories'])}")
            if err["missing_steps"]:
                a(f"- **Missing steps:** {len(err['missing_steps'])} — e.g. "
                  f"{['/'.join(s) for s in err['missing_steps'][:3]]}")
            if err["extra_steps"]:
                a(f"- **Extra steps:** {len(err['extra_steps'])} — e.g. "
                  f"{['/'.join(s) for s in err['extra_steps'][:3]]}")
            a("")
    else:
        a("All cases matched — no failures to diagnose.")
    a("")

    # Write
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Phase 8A metrics report written to %s", output_path)


def generate_json_report(results: Dict[str, Any], output_path: Path) -> None:
    """Write evaluation/phase8a_errors.json."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(results, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Phase 8A JSON error report written to %s", output_path)


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Phase 8A Evaluation and Error Analysis")
    parser.add_argument(
        "--predictions",
        type=Path,
        default=None,
        help="Path to predictions CSV (default: scratch/phase8a_predictions.csv)",
    )
    parser.add_argument(
        "--no-run",
        action="store_true",
        help="Skip pipeline execution, use existing predictions file",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=repo_root / "evaluation",
        help="Directory for output reports (default: evaluation/)",
    )
    args = parser.parse_args()

    start = time.time()
    logger.info("Starting Phase 8A evaluation...")

    results = run_phase8a_evaluation(
        predictions_csv=args.predictions,
        run_pipeline_first=not args.no_run,
    )

    # Generate reports
    md_path = args.output_dir / "phase8a_metrics.md"
    json_path = args.output_dir / "phase8a_errors.json"

    generate_markdown_report(results, md_path)
    generate_json_report(results, json_path)

    elapsed = time.time() - start
    logger.info("Phase 8A evaluation completed in %.2f seconds", elapsed)

    # Print summary
    print("\n" + "=" * 70)
    print("PHASE 8A EVALUATION SUMMARY")
    print("=" * 70)
    print(f"  Status Accuracy:          {results['status_accuracy']:.4f} "
          f"({int(results['status_accuracy'] * results['total_cases'])}/{results['total_cases']})")
    print(f"  Macro F1:                 {results['macro_avg']['f1']:.4f}")
    print(f"  Weighted F1:              {results['weighted_avg']['f1']:.4f}")
    print(f"  Mismatches:               {results['mismatch_count']}")
    print(f"  False COMPLETEs:          {results['safety_metrics']['false_complete_count']}")
    print(f"  Exact step sequence:      {results['step_metrics']['exact_sequence_matches']}/{results['total_cases']}")
    print(f"  Step-level F1:            {results['step_metrics']['aggregate_step_f1']:.4f}")
    print(f"  Reports:                  {md_path}")
    print(f"                            {json_path}")
    print("=" * 70)


if __name__ == "__main__":
    main()
