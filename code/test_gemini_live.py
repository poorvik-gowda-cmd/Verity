"""Optional live verification script for Phase 5 Gemini Evidence Interpretation.

Runs up to 3 live test cases on representative evidence snippets when GEMINI_API_KEY
is configured. Never exposes API keys in output.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add code directory to path
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root / "code"))

from config import config
from tracepoint.interpretation import (
    EvidenceSnippet,
    GeminiInterpreter,
    InterpretationTarget,
)


def run_live_tests() -> None:
    """Execute up to three bounded live tests using Google Gemini."""
    print("=" * 70)
    print("TRACEPOINT PHASE 5: GEMINI EVIDENCE INTERPRETATION (LIVE TEST)")
    print("=" * 70)

    interpreter = GeminiInterpreter()

    if not interpreter.is_available:
        print("\n[INFO] GEMINI_API_KEY is not set or client unavailable.")
        print("To run live API tests, add your key to .env or set environment variable:")
        print("  $env:GEMINI_API_KEY='your_api_key_here'")
        print("Running demonstration with deterministic fallback...\n")
    else:
        print(f"\n[INFO] Gemini client active. Model: {interpreter.model}")
        print("Executing max 3 live test queries on representative evidence...\n")

    # 3 Focused Test Targets
    test_cases = [
        InterpretationTarget(
            case_id="case_05",
            user_id="user_05",
            query="Determine whether personal account data for user_05 remains in the failover replica snapshot.",
            evidence_snippets=[
                EvidenceSnippet(
                    evidence_id="evidence_54",
                    locator="table:accounts/row:5",
                    system_id="account_failover_replica",
                    resource_id="account_005",
                    timestamp="2026-09-02T22:45:00Z",
                    content='{"resource_id": "account_005", "email": "leila.haddad@yahoo.com", "name": "Leila Haddad", "status": "active"}',
                )
            ],
        ),
        InterpretationTarget(
            case_id="case_08",
            user_id="user_08",
            query="Assess whether customer support record contains prohibited identifiers or preserved tombstone.",
            evidence_snippets=[
                EvidenceSnippet(
                    evidence_id="evidence_12",
                    locator="doc:ticket_441",
                    system_id="support_service",
                    resource_id="ticket_441",
                    timestamp="2026-08-25T11:00:00Z",
                    content="Ticket 441 closed. Account reference user_08 preserved. Direct identifiers and email hash redacted per policy.",
                )
            ],
        ),
        InterpretationTarget(
            case_id="case_15",
            user_id="user_15",
            query="Analyze data flow sync activity and determine if replication process is still active.",
            evidence_snippets=[
                EvidenceSnippet(
                    evidence_id="evidence_33",
                    locator="pipeline:customer_sync",
                    system_id="sync_service",
                    resource_id="flow_account_sync",
                    timestamp="2026-09-04T14:30:00Z",
                    content="Scheduled sync job customer_sync executed. Process active: true. 120 customer records queued for replication.",
                )
            ],
        ),
    ]

    for idx, target in enumerate(test_cases, 1):
        print(f"--- Test Case {idx}/3: {target.case_id} ({target.user_id}) ---")
        print(f"Target Query : {target.query}")
        print(f"Snippets     : {[s.evidence_id for s in target.evidence_snippets]}")

        result = interpreter.interpret(target)

        print(f"Provider     : {result.provider} (model: {result.model})")
        print(f"Fallback Used: {result.fallback_used}")
        print(f"Cached Hit   : {result.cached}")
        print(f"Tokens In/Out: {result.tokens_in} / {result.tokens_out}")
        print(f"Latency      : {result.latency_ms:.1f} ms")
        print(f"Summary      : {result.summary}")
        print(f"Claims ({len(result.claims)}):")
        for c in result.claims:
            print(
                f"  * [{c.claim_type.value}] state={c.observed_state} "
                f"evidence={c.supporting_evidence_id} (conf={c.confidence:.2f})"
            )
            if c.uncertainty_note:
                print(f"    Note: {c.uncertainty_note}")
        print()

    # Summary
    print("=" * 70)
    print("AGGREGATED USAGE & COST SUMMARY:")
    print("=" * 70)
    summary = interpreter.usage_tracker.get_summary()
    for k, v in summary.items():
        print(f"  {k:22}: {v}")
    print("=" * 70)


if __name__ == "__main__":
    run_live_tests()
