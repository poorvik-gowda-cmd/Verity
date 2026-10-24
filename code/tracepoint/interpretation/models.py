"""Data models and validation schemas for Gemini evidence interpretation.

Enforces structured JSON extraction, strict evidence grounding, and guarantees
that Gemini interpretations cannot dictate deletion statuses or invent evidence IDs.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, Field, field_validator


class EvidenceClaimType(str, Enum):
    """Categorisation of a fact/claim extracted from evidence."""

    RECORD_PRESENT = "RECORD_PRESENT"
    RECORD_ABSENT = "RECORD_ABSENT"
    EXPOSED_PII = "EXPOSED_PII"
    DATA_FLOW_ACTIVE = "DATA_FLOW_ACTIVE"
    DATA_FLOW_DISABLED = "DATA_FLOW_DISABLED"
    RESTORE_RISK = "RESTORE_RISK"
    TOMBSTONE_PRESERVED = "TOMBSTONE_PRESERVED"
    OTHER = "OTHER"


class EvidenceSnippet(BaseModel):
    """Input evidence snippet passed to Gemini for interpretation."""

    evidence_id: str = Field(description="Evidence file or record identifier (e.g. evidence_54)")
    locator: Optional[str] = Field(default=None, description="Location within the evidence file")
    system_id: Optional[str] = Field(default=None, description="System name if known from metadata")
    resource_id: Optional[str] = Field(default=None, description="Resource identifier if known")
    timestamp: Optional[str] = Field(default=None, description="Capture or event timestamp if known")
    content: str = Field(description="Extracted text snippet or content excerpt")


class InterpretationTarget(BaseModel):
    """Specific inquiry targeted at Gemini for evidence interpretation."""

    case_id: str = Field(description="Audit request case identifier")
    user_id: Optional[str] = Field(default=None, description="Resolved internal user identifier")
    query: str = Field(description="Focused question or target (e.g. 'Assess personal data in support ticket')")
    evidence_snippets: List[EvidenceSnippet] = Field(
        default_factory=list,
        description="Bounded set of relevant evidence snippets for interpretation",
    )

    def allowed_evidence_ids(self) -> Set[str]:
        """Set of evidence IDs provided in this target."""
        return {s.evidence_id for s in self.evidence_snippets if s.evidence_id}


class EvidenceClaim(BaseModel):
    """Single verified claim extracted from an evidence snippet."""

    claim_type: EvidenceClaimType = Field(description="Category of the claim")
    supporting_evidence_id: str = Field(description="Must match an evidence_id from the supplied snippets")
    system_id: Optional[str] = Field(default=None, description="Associated system if explicitly stated")
    resource_id: Optional[str] = Field(default=None, description="Associated resource if explicitly stated")
    observed_state: Optional[str] = Field(default=None, description="State observed (e.g. PRESENT, ABSENT)")
    timestamp: Optional[str] = Field(default=None, description="Timestamp explicitly mentioned in evidence, if any")
    supporting_locator: Optional[str] = Field(default=None, description="Within-evidence locator or line reference")
    identifiers_found: List[str] = Field(default_factory=list, description="Any personal identifiers located")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Extraction confidence score")
    uncertainty_note: Optional[str] = Field(default=None, description="Explanation of ambiguity if confidence < 1.0")


class GeminiExtractionOutput(BaseModel):
    """Structured response schema returned by Gemini and validated."""

    claims: List[EvidenceClaim] = Field(default_factory=list, description="Extracted factual claims")
    summary: str = Field(default="", description="Concise interpretation summary of the evidence snippets")
    ambiguities: List[str] = Field(default_factory=list, description="Identified ambiguities or missing context")


class InterpretationResult(BaseModel):
    """End-to-end outcome of evidence interpretation, including provenance and metrics."""

    target: InterpretationTarget
    claims: List[EvidenceClaim]
    summary: str
    ambiguities: List[str]
    rejected_claims_count: int = 0
    fallback_used: bool = False
    provider: str = "google"
    model: str = "gemini-2.5-flash"
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    cached: bool = False
    error_message: Optional[str] = None


def validate_and_filter_grounding(
    output: GeminiExtractionOutput,
    allowed_evidence_ids: Set[str],
) -> Tuple[List[EvidenceClaim], int]:
    """Filter claims to ensure strict grounding against supplied evidence snippets.

    Rejects any claim that cites an evidence_id not present in allowed_evidence_ids.
    Returns (valid_claims, rejected_count).
    """
    valid_claims: List[EvidenceClaim] = []
    rejected_count = 0

    for claim in output.claims:
        clean_eid = claim.supporting_evidence_id.strip()
        if clean_eid in allowed_evidence_ids:
            valid_claims.append(claim)
        else:
            rejected_count += 1

    return valid_claims, rejected_count
