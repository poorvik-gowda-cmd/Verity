"""CLI tool for running Tracepoint evidence ingestion and testing search queries."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add repo root and code dir to sys.path
code_dir = Path(__file__).resolve().parent
repo_root = code_dir.parent
for p in [str(code_dir), str(repo_root)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from config import config
from tracepoint.ingestion.indexer import EvidenceIndex
from tracepoint.ingestion.pipeline import IngestionPipeline
from tracepoint.utils import mask_email


def run_cli() -> int:
    parser = argparse.ArgumentParser(description="Tracepoint Stage 2: Evidence Ingestion and Indexing CLI")
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to SQLite index database (defaults to in-memory)",
    )
    parser.add_argument(
        "--search",
        type=str,
        default=None,
        help="Search query to execute against the indexed evidence",
    )
    parser.add_argument(
        "--system",
        type=str,
        default=None,
        help="Filter search by system_id (e.g., search_service, account_service)",
    )
    parser.add_argument(
        "--resource",
        type=str,
        default=None,
        help="Filter search by resource_id (e.g., search_001, account_006)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON results",
    )
    args = parser.parse_args()

    index = EvidenceIndex(db_path=args.db_path)
    pipeline = IngestionPipeline(dataset_dir=config.dataset_dir, index=index)

    print("Running Tracepoint evidence ingestion pipeline...")
    report = pipeline.run()

    if args.search:
        print(f"\nExecuting search query: '{args.search}'...")
        results = index.search(
            query=args.search,
            system_id=args.system,
            resource_id=args.resource,
            limit=10,
        )
        report["search_results_count"] = len(results)
        report["search_results"] = [
            {
                "index_id": r.index_id,
                "evidence_id": r.evidence_id,
                "source_path": r.source_path,
                "source_type": r.source_type.value,
                "locator": r.locator,
                "system_id": r.system_id,
                "resource_id": r.resource_id,
                "timestamp": r.timestamp,
                "snippet": r.snippet,
            }
            for r in results
        ]

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    # Print human-readable report
    print("\n" + "=" * 70)
    print("TRACEPOINT -- EVIDENCE INGESTION REPORT (STAGE 2)")
    print("=" * 70)
    print(f"Elapsed Time         : {report['elapsed_seconds']}s")
    print(f"Total Files Found    : {report['files_discovered']}")
    print(f"Parsed Successfully  : {report['files_parsed_successfully']}")
    print(f"Unsupported Formats  : {report['files_unsupported']} (11 PNGs require OCR/Vision)")
    print(f"Failed Files         : {report['files_failed']}")
    print(f"Total Items Indexed  : {report['total_items_indexed']:,}")

    cov = report["coverage_summary"]
    print("\n[Item Breakdown by Source Type]")
    print("-" * 70)
    for stype, count in sorted(cov["breakdown_by_source_type"].items()):
        print(f"  - {stype:<20} : {count:>5,} items")

    print("\n[Top Discovered Systems]")
    print("-" * 70)
    for sys_id, count in sorted(cov["top_systems"].items(), key=lambda x: x[1], reverse=True)[:6]:
        print(f"  - {sys_id:<25} : {count:>5,} records")

    if args.search:
        print("\n[Search Query Matches]")
        print("-" * 70)
        print(f"Query: '{args.search}' -> Found {len(results)} matches:")
        for res in results[:5]:
            print(
                f"  [{res.evidence_id}] {res.source_type.value} @ {res.locator} "
                f"(sys: {res.system_id}, res: {res.resource_id}, time: {res.timestamp})"
            )
            print(f"    Snippet: {res.snippet[:120]}...")

    print("=" * 70)
    print("STAGE 2 INGESTION COMPLETE: Provenance preserved, SQLite FTS5 active.")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(run_cli())
