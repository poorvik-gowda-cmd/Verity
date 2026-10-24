"""Deterministic fallback interpreter for evidence extraction.

Provides reliable, rule-based claim extraction when Google Gemini is unavailable,
unconfigured, times out, or returns invalid responses.
"""

from __future__ import annotations

import re
from typing import List, Set

from .models import (
    EvidenceClaim,
    EvidenceClaimType,
    EvidenceSnippet,
    GeminiExtractionOutput,
    InterpretationTarget,
)


class DeterministicFallbackInterpreter:
    """Extracts factual claims using deterministic regex and keyword heuristics."""

    def interpret(self, target: InterpretationTarget) -> GeminiExtractionOutput:
        """Analyze evidence snippets using deterministic patterns."""
        claims: List[EvidenceClaim] = []
        ambiguities: List[str] = []

        # Target terms to scan for
        search_terms: Set[str] = set()
        if target.user_id:
            search_terms.add(target.user_id.lower())
            # Add account variant
            uid_num = target.user_id.replace("user_", "")
            try:
                search_terms.add(f"account_{int(uid_num):03d}".lower())
            except ValueError:
                pass
            search_terms.add(f"account_{uid_num}".lower())

        for snippet in target.evidence_snippets:
            content_lc = snippet.content.lower()

            # Check for identifiers in snippet
            found_identifiers = []
            for term in search_terms:
                if re.search(r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])", content_lc):
                    found_identifiers.append(term)

            # Detect claim type
            claim_type: EvidenceClaimType = EvidenceClaimType.OTHER
            observed_state: str = "UNKNOWN"
            note: str = "Extracted via deterministic fallback heuristics."

            if any(k in content_lc for k in ['"deleted": true', '"status": "deleted"', 'account deleted', 'record deleted']):
                claim_type = EvidenceClaimType.RECORD_ABSENT
                observed_state = "ABSENT"
            elif any(k in content_lc for k in ['redacted', 'pii_redacted', 'pii removed', 'tombstone']):
                claim_type = EvidenceClaimType.TOMBSTONE_PRESERVED
                observed_state = "REDACTED"
            elif any(k in content_lc for k in ['"active": true', 'syncing', 'sync active', 'data flow active']):
                claim_type = EvidenceClaimType.DATA_FLOW_ACTIVE
                observed_state = "ACTIVE"
            elif any(k in content_lc for k in ['backup restorable', 'restore pending', 'unencrypted backup']):
                claim_type = EvidenceClaimType.RESTORE_RISK
                observed_state = "ACTIVE"
            elif found_identifiers or any(k in content_lc for k in ['active', 'confirmed', 'present', 'user_id', 'email']):
                claim_type = EvidenceClaimType.RECORD_PRESENT
                observed_state = "PRESENT"
            else:
                ambiguities.append(
                    f"Evidence {snippet.evidence_id} did not match definitive deterministic patterns."
                )

            claims.append(
                EvidenceClaim(
                    claim_type=claim_type,
                    supporting_evidence_id=snippet.evidence_id,
                    system_id=snippet.system_id,
                    resource_id=snippet.resource_id,
                    observed_state=observed_state,
                    timestamp=snippet.timestamp,
                    supporting_locator=snippet.locator,
                    identifiers_found=found_identifiers,
                    confidence=0.85,
                    uncertainty_note=note,
                )
            )

        summary = (
            f"Deterministic fallback processed {len(target.evidence_snippets)} snippets "
            f"and extracted {len(claims)} candidate claims."
        )

        return GeminiExtractionOutput(
            claims=claims,
            summary=summary,
            ambiguities=ambiguities,
        )
