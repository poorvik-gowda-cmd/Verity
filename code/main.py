"""Tracepoint Deletion Audit Pipeline - Main Entry Point.

Orchestrates evidence ingestion, identity resolution, temporal timeline assembly,
contradiction detection, policy evaluation, resurrection-risk detection, and
proof-driven decision generation into the authoritative output.csv format.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

import pandas as pd

# Ensure code directory is in sys.path
code_dir = Path(__file__).resolve().parent
repo_root = code_dir.parent
for p in [str(code_dir), str(repo_root)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from config import config, ProjectConfig
from tracepoint.models import DeletionCase, DeletionStatus, OutputRow
from tracepoint.resolver import IdentityResolver
from tracepoint.ingestion.indexer import EvidenceIndex
from tracepoint.ingestion.pipeline import IngestionPipeline
from tracepoint.timeline import TimelineBuilder, ContradictionDetector
from tracepoint.policy import (
    PolicyRuleRegistry,
    PolicyEvaluator,
    ResurrectionRiskAnalyzer,
)
from tracepoint.decision import (
    DecisionEngine,
    StepGenerator,
    StepValidator,
    RequestClassifier,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("tracepoint.main")


def ensure_evidence_index(
    cfg: ProjectConfig, force_reindex: bool = False
) -> EvidenceIndex:
    """Build or load the SQLite evidence database."""
    db_path = cfg.dataset_dir / "evidence.db"
    index = EvidenceIndex(db_path=db_path)

    # Check if database has indexed items
    cursor = index.conn.cursor()
    cursor.execute("SELECT count(*) FROM evidence_items;")
    item_count = cursor.fetchone()[0]

    if item_count == 0 or force_reindex:
        logger.info("Evidence index at %s is empty or force reindex requested; populating...", db_path)
        pipeline = IngestionPipeline(dataset_dir=cfg.dataset_dir, index=index)
        report = pipeline.run()
        logger.info(
            "Ingestion complete: %d items indexed from %d files (parsed=%d, failed=%d) in %.2fs",
            report.get("total_items_indexed", 0),
            report.get("files_discovered", 0),
            report.get("files_parsed_successfully", 0),
            report.get("files_failed", 0),
            report.get("elapsed_seconds", 0.0),
        )
    else:
        logger.info("Loaded existing evidence index from %s with %d items.", db_path, item_count)

    return index


def build_decision_engine(
    cfg: ProjectConfig, index: EvidenceIndex
) -> DecisionEngine:
    """Initialize all Tracepoint subsystems and assemble the decision engine."""
    logger.info("Initializing Tracepoint subsystems...")
    activity_df = pd.read_csv(cfg.system_activity_csv)
    manifest_df = pd.read_csv(cfg.evidence_files_csv)

    # Gather known grounded evidence IDs
    known_evidence_ids: Set[str] = set()
    if "file_id" in manifest_df.columns:
        known_evidence_ids.update(manifest_df["file_id"].dropna().astype(str).unique())
    if "activity_id" in activity_df.columns:
        known_evidence_ids.update(activity_df["activity_id"].dropna().astype(str).unique())

    # Build individual subsystems
    resolver = IdentityResolver.from_config(cfg)
    timeline_builder = TimelineBuilder(
        activity_df=activity_df,
        evidence_index=index,
        resolver=resolver,
    )
    contradiction_detector = ContradictionDetector()
    policy_registry = PolicyRuleRegistry.load_from_csv(cfg.deletion_rules_csv)
    policy_evaluator = PolicyEvaluator(policy_registry)
    resurrection_analyzer = ResurrectionRiskAnalyzer()
    validator = StepValidator(known_evidence_ids=known_evidence_ids)

    engine = DecisionEngine(
        resolver=resolver,
        timeline_builder=timeline_builder,
        contradiction_detector=contradiction_detector,
        policy_evaluator=policy_evaluator,
        resurrection_analyzer=resurrection_analyzer,
        validator=validator,
        known_evidence_ids=known_evidence_ids,
    )
    return engine


def run_pipeline(
    input_csv: Path,
    output_csv: Path,
    cfg: Optional[ProjectConfig] = None,
    limit: Optional[int] = None,
    force_reindex: bool = False,
) -> Dict[str, Any]:
    """Execute end-to-end deletion audit pipeline over input CSV."""
    cfg = cfg or config
    start_time = time.time()
    logger.info("Starting Tracepoint pipeline")
    logger.info("  Input:  %s", input_csv)
    logger.info("  Output: %s", output_csv)

    # 1. Evidence database
    index = ensure_evidence_index(cfg, force_reindex=force_reindex)

    # 2. Subsystems
    engine = build_decision_engine(cfg, index)

    # 3. Read input requests
    df_requests = pd.read_csv(input_csv)
    if "case_id" in df_requests.columns:
        df_requests = df_requests.sort_values("case_id").reset_index(drop=True)

    if limit and limit > 0:
        logger.info("Limiting processing to first %d cases", limit)
        df_requests = df_requests.head(limit)

    total_cases = len(df_requests)
    logger.info("Processing %d requests...", total_cases)

    status_counts: Dict[str, int] = {
        DeletionStatus.COMPLETE.value: 0,
        DeletionStatus.INCOMPLETE.value: 0,
        DeletionStatus.NOT_APPLICABLE.value: 0,
    }

    output_rows: List[Dict[str, str]] = []
    validation_failures = 0

    for idx, row in df_requests.iterrows():
        case = DeletionCase(
            case_id=str(row["case_id"]),
            email=str(row["email"]),
            requested_at=str(row["requested_at"]),
            reported_status=str(row.get("reported_status", "")),
            reported_at=str(row.get("reported_at", "")),
            audit_at=str(row["audit_at"]),
            request_text=str(row.get("request_text", "")),
        )

        decision = engine.decide_case(case)
        status_counts[decision.deletion_status.value] = (
            status_counts.get(decision.deletion_status.value, 0) + 1
        )

        # Validate decision output
        if decision.validation_result and not decision.validation_result.is_valid:
            validation_failures += 1

        # Format output row
        row_dict = decision.output_row.to_csv_dict() if decision.output_row else {
            "case_id": case.case_id,
            "deletion_status": decision.deletion_status.value,
            "steps_json": "[]",
        }
        output_rows.append(row_dict)

        if (idx + 1) % 50 == 0 or (idx + 1) == total_cases:
            logger.info("Processed %d / %d cases", idx + 1, total_cases)

    # 4. Write output CSV
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["case_id", "deletion_status", "steps_json"],
            quoting=csv.QUOTE_MINIMAL,
        )
        writer.writeheader()
        writer.writerows(output_rows)

    # If writing full evaluation to root output.csv, also write to dataset/output.csv
    if output_csv == cfg.root_output_csv:
        cfg.template_output_csv.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg.template_output_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["case_id", "deletion_status", "steps_json"],
                quoting=csv.QUOTE_MINIMAL,
            )
            writer.writeheader()
            writer.writerows(output_rows)
        logger.info("Mirrored output to %s", cfg.template_output_csv)

    elapsed = time.time() - start_time
    logger.info("Pipeline completed in %.2f seconds", elapsed)
    logger.info("Output written to: %s", output_csv)
    logger.info("Status distribution: %s", status_counts)
    if validation_failures > 0:
        logger.warning("%d cases had validation warnings/errors", validation_failures)
    else:
        logger.info("All output cases passed independent validation cleanly!")

    return {
        "total_cases": total_cases,
        "elapsed_seconds": round(elapsed, 2),
        "status_counts": status_counts,
        "validation_failures": validation_failures,
        "output_path": str(output_csv),
    }


def main():
    parser = argparse.ArgumentParser(description="Tracepoint Deletion Audit CLI")
    parser.add_argument(
        "--input",
        type=Path,
        default=config.deletion_requests_csv,
        help="Input CSV file path (default: dataset/deletion_requests.csv)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=config.root_output_csv,
        help="Output CSV file path (default: output.csv at repo root)",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Run pipeline on sample_deletion_requests.csv",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of processed cases (useful for testing)",
    )
    parser.add_argument(
        "--force-reindex",
        action="store_true",
        help="Force re-ingesting and re-indexing evidence files into SQLite DB",
    )

    args = parser.parse_args()

    input_file = config.sample_deletion_requests_csv if args.sample else args.input
    output_file = args.output

    run_pipeline(
        input_csv=input_file,
        output_csv=output_file,
        limit=args.limit,
        force_reindex=args.force_reindex,
    )


if __name__ == "__main__":
    main()
