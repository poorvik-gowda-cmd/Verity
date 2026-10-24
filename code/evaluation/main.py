"""Tracepoint Evaluation and Validation Suite.

Validates output.csv against the authoritative HackerRank challenge specification,
evaluates public sample predictions against sample_output.csv without runtime shortcuts,
and tests synthetic edge cases covering ambiguous identity, contradictory evidence,
and active restoration risks.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

# Add code directory to path
code_dir = Path(__file__).resolve().parent.parent
repo_root = code_dir.parent
for p in [str(code_dir), str(repo_root)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from config import config
from tracepoint.models import (
    DeletionStatus,
    Phase,
    Operation,
    ALLOWED_OPERATION_STATES,
)
from tracepoint.policy.models import ObservedState
from tracepoint.decision.validator import StepValidator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("tracepoint.eval")


def validate_submission_csv(
    csv_path: Path,
    expected_case_ids: Optional[List[str]] = None,
    known_evidence_ids: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Perform rigorous independent validation on output.csv."""
    logger.info("Validating output CSV: %s", csv_path)
    if not csv_path.exists():
        return {"is_valid": False, "errors": [f"File not found: {csv_path}"]}

    errors: List[str] = []
    warnings: List[str] = []

    try:
        df = pd.read_csv(csv_path, dtype=str)
    except Exception as e:
        return {"is_valid": False, "errors": [f"Malformed CSV: {e}"]}

    # Check exact columns
    expected_cols = ["case_id", "deletion_status", "steps_json"]
    if list(df.columns) != expected_cols:
        errors.append(f"Column mismatch: expected {expected_cols}, got {list(df.columns)}")

    # Check row count
    row_count = len(df)
    if expected_case_ids is not None:
        if row_count != len(expected_case_ids):
            errors.append(f"Row count mismatch: expected {len(expected_case_ids)}, got {row_count}")

        # Check sorting and 1-to-1 case_ids
        actual_case_ids = df["case_id"].tolist()
        if actual_case_ids != sorted(actual_case_ids):
            errors.append("Rows are not sorted by case_id")
        if set(actual_case_ids) != set(expected_case_ids):
            missing = set(expected_case_ids) - set(actual_case_ids)
            unexpected = set(actual_case_ids) - set(expected_case_ids)
            if missing:
                errors.append(f"Missing case_ids: {len(missing)} cases (e.g. {list(missing)[:5]})")
            if unexpected:
                errors.append(f"Unexpected case_ids: {len(unexpected)} cases (e.g. {list(unexpected)[:5]})")

    validator = StepValidator(known_evidence_ids=known_evidence_ids)
    status_counts: Dict[str, int] = {}

    for idx, row in df.iterrows():
        case_id = row.get("case_id", f"row_{idx}")
        status_val = row.get("deletion_status", "")
        steps_val = row.get("steps_json", "")

        status_counts[status_val] = status_counts.get(status_val, 0) + 1

        try:
            status = DeletionStatus(status_val)
        except ValueError:
            errors.append(f"Case {case_id}: Invalid deletion_status '{status_val}'")
            continue

        try:
            steps_data = json.loads(steps_val)
        except json.JSONDecodeError as e:
            errors.append(f"Case {case_id}: Malformed steps_json: {e}")
            continue

        if not isinstance(steps_data, list):
            errors.append(f"Case {case_id}: steps_json is not an array")
            continue

        # Schema validation per step
        from tracepoint.models import AuditStep
        audit_steps: List[AuditStep] = []
        parse_err = False
        for s_idx, s_dict in enumerate(steps_data):
            try:
                audit_steps.append(AuditStep(**s_dict))
            except Exception as e:
                errors.append(f"Case {case_id} step {s_idx + 1}: Malformed step schema: {e}")
                parse_err = True

        if not parse_err:
            res = validator.validate_steps(audit_steps, status, case_id=case_id)
            if not res.is_valid:
                for err in res.errors:
                    errors.append(f"Case {case_id}: {err}")
            if res.warnings:
                for warn in res.warnings:
                    warnings.append(f"Case {case_id}: {warn}")

    is_valid = len(errors) == 0
    return {
        "is_valid": is_valid,
        "row_count": row_count,
        "status_counts": status_counts,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors[:50],  # cap at first 50
        "warnings": warnings[:50],
    }


def evaluate_public_samples() -> Dict[str, Any]:
    """Evaluate pipeline on the 32 public sample cases against sample_output.csv."""
    from main import run_pipeline

    logger.info("Evaluating public sample cases against sample_output.csv...")
    sample_requests_csv = config.sample_deletion_requests_csv
    sample_output_csv = config.sample_output_csv

    temp_pred_csv = repo_root / "scratch" / "eval_sample_predictions.csv"
    run_pipeline(
        input_csv=sample_requests_csv,
        output_csv=temp_pred_csv,
        force_reindex=False,
    )

    df_expected = pd.read_csv(sample_output_csv).set_index("case_id")
    df_predicted = pd.read_csv(temp_pred_csv).set_index("case_id")

    total = len(df_expected)
    status_matches = 0
    step_count_matches = 0
    exact_matches = 0

    per_case_results = []

    for case_id, exp_row in df_expected.iterrows():
        if case_id not in df_predicted.index:
            per_case_results.append({
                "case_id": case_id,
                "status_match": False,
                "expected_status": exp_row["deletion_status"],
                "predicted_status": "MISSING",
            })
            continue

        pred_row = df_predicted.loc[case_id]
        exp_status = exp_row["deletion_status"]
        pred_status = pred_row["deletion_status"]

        s_match = (exp_status == pred_status)
        if s_match:
            status_matches += 1

        exp_steps = json.loads(exp_row["steps_json"])
        pred_steps = json.loads(pred_row["steps_json"])

        c_match = len(exp_steps) == len(pred_steps)
        if c_match:
            step_count_matches += 1

        # Check full step sequence equivalence
        exact = s_match and exp_steps == pred_steps
        if exact:
            exact_matches += 1

        per_case_results.append({
            "case_id": case_id,
            "status_match": s_match,
            "expected_status": exp_status,
            "predicted_status": pred_status,
            "expected_steps_count": len(exp_steps),
            "predicted_steps_count": len(pred_steps),
            "exact_steps_match": exact,
        })

    status_accuracy = (status_matches / total) * 100 if total > 0 else 0.0
    step_count_accuracy = (step_count_matches / total) * 100 if total > 0 else 0.0

    logger.info("Sample Evaluation Results:")
    logger.info("  Status Accuracy:     %d / %d (%.1f%%)", status_matches, total, status_accuracy)
    logger.info("  Step Count Matches:  %d / %d (%.1f%%)", step_count_matches, total, step_count_accuracy)
    logger.info("  Exact Step Matches:  %d / %d", exact_matches, total)

    return {
        "total_sample_cases": total,
        "status_matches": status_matches,
        "status_accuracy_pct": round(status_accuracy, 2),
        "step_count_matches": step_count_matches,
        "step_count_accuracy_pct": round(step_count_accuracy, 2),
        "exact_matches": exact_matches,
        "details": per_case_results,
    }


def run_synthetic_edge_cases() -> Dict[str, Any]:
    """Execute synthetic tests for ambiguous identity, contradiction, and restoration risk."""
    from tracepoint.models import DeletionCase
    from main import build_decision_engine, ensure_evidence_index

    logger.info("Running synthetic edge case tests...")
    idx = ensure_evidence_index(config)
    engine = build_decision_engine(config, idx)

    # Edge Case 1: Ambiguous / unknown identity
    case_ambig = DeletionCase(
        case_id="synthetic_ambiguous_01",
        email="unknown.ghost.user.999@nonexistent.domain",
        requested_at="2026-09-01T12:00:00Z",
        reported_status="COMPLETE",
        reported_at="2026-09-02T12:00:00Z",
        audit_at="2026-09-05T12:00:00Z",
        request_text="Please delete all data associated with my account.",
    )
    dec_ambig = engine.decide_case(case_ambig)

    # Edge Case 2: Explicit contradiction (reported complete, but snapshot shows present)
    case_contra = DeletionCase(
        case_id="synthetic_contra_02",
        email="leila.haddad@yahoo.com",
        requested_at="2026-08-27T14:17:00Z",
        reported_status="COMPLETE",
        reported_at="2026-08-28T09:03:00Z",
        audit_at="2026-09-08T13:40:00Z",
        request_text="I deleted my account but email is still recognized. Remove it completely.",
    )
    dec_contra = engine.decide_case(case_contra)

    # Edge Case 3: Active restoration / data flow risk
    case_risk = DeletionCase(
        case_id="synthetic_restoration_03",
        email="mateo.rios@outlook.com",
        requested_at="2026-09-01T10:00:00Z",
        reported_status="COMPLETE",
        reported_at="2026-09-02T10:00:00Z",
        audit_at="2026-09-15T12:00:00Z",
        request_text="Please delete my account and ensure backups or sync jobs do not restore it.",
    )
    dec_risk = engine.decide_case(case_risk)

    results = {
        "edge_case_ambiguous_identity": {
            "case_id": case_ambig.case_id,
            "status": dec_ambig.deletion_status.value,
            "expected_status": "INCOMPLETE",
            "passed": dec_ambig.deletion_status == DeletionStatus.INCOMPLETE,
            "reason": dec_ambig.classification_reason,
        },
        "edge_case_contradictory_evidence": {
            "case_id": case_contra.case_id,
            "status": dec_contra.deletion_status.value,
            "expected_status": "INCOMPLETE",
            "passed": dec_contra.deletion_status == DeletionStatus.INCOMPLETE,
            "contradiction_count": dec_contra.contradiction_count,
            "steps_count": len(dec_contra.steps),
        },
        "edge_case_active_restoration_risk": {
            "case_id": case_risk.case_id,
            "status": dec_risk.deletion_status.value,
            "steps_count": len(dec_risk.steps),
            "hazard_count": dec_risk.hazard_count,
            "passed": len(dec_risk.steps) > 0,
        },
    }

    all_passed = all(v.get("passed", False) for v in results.values())
    logger.info("Synthetic edge cases passed: %s", all_passed)
    return {"all_passed": all_passed, "tests": results}


def main():
    parser = argparse.ArgumentParser(description="Tracepoint Evaluation Entry Point")
    parser.add_argument(
        "--csv",
        type=Path,
        default=config.root_output_csv,
        help="Path to output CSV to validate (default: output.csv at repo root)",
    )
    parser.add_argument(
        "--eval-samples",
        action="store_true",
        help="Run public sample evaluation against sample_output.csv",
    )
    parser.add_argument(
        "--edge-cases",
        action="store_true",
        help="Run synthetic edge case suite",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run validation, sample evaluation, and edge cases",
    )

    args = parser.parse_args()

    # Load known evidence IDs and request IDs
    activity_df = pd.read_csv(config.system_activity_csv)
    manifest_df = pd.read_csv(config.evidence_files_csv)
    known_evidence_ids = set(manifest_df["file_id"].dropna().astype(str).unique())
    known_evidence_ids.update(activity_df["activity_id"].dropna().astype(str).unique())

    requests_df = pd.read_csv(config.deletion_requests_csv)
    expected_case_ids = requests_df["case_id"].dropna().astype(str).tolist()

    if args.all or (not args.eval_samples and not args.edge_cases):
        # Validate output CSV
        val_res = validate_submission_csv(
            csv_path=args.csv,
            expected_case_ids=expected_case_ids,
            known_evidence_ids=known_evidence_ids,
        )
        print("\n" + "=" * 60)
        print("OUTPUT VALIDATION REPORT")
        print("=" * 60)
        print(f"File:       {args.csv}")
        print(f"Valid:      {val_res['is_valid']}")
        print(f"Rows:       {val_res.get('row_count', 0)}")
        print(f"Statuses:   {val_res.get('status_counts', {})}")
        print(f"Errors:     {val_res.get('error_count', 0)}")
        print(f"Warnings:   {val_res.get('warning_count', 0)}")
        if val_res.get("errors"):
            print("Sample Errors:")
            for e in val_res["errors"][:10]:
                print(f"  * {e}")
        print("=" * 60)

    if args.all or args.eval_samples:
        sample_res = evaluate_public_samples()
        print("\n" + "=" * 60)
        print("PUBLIC SAMPLE EVALUATION REPORT")
        print("=" * 60)
        print(f"Total Cases:     {sample_res['total_sample_cases']}")
        print(f"Status Matches:  {sample_res['status_matches']} ({sample_res['status_accuracy_pct']}%)")
        print(f"Step Matches:    {sample_res['step_count_matches']} ({sample_res['step_count_accuracy_pct']}%)")
        print(f"Exact Matches:   {sample_res['exact_matches']}")
        print("=" * 60)

    if args.all or args.edge_cases:
        edge_res = run_synthetic_edge_cases()
        print("\n" + "=" * 60)
        print("SYNTHETIC EDGE CASES REPORT")
        print("=" * 60)
        print(f"All Passed: {edge_res['all_passed']}")
        for name, t in edge_res["tests"].items():
            print(f"  * {name}: passed={t['passed']} (status={t.get('status')})")
        print("=" * 60)


if __name__ == "__main__":
    main()
