"""Data models and schemas for deletion policy evaluation and resurrection risk detection.

Conforms strictly to AGENTS.md §6.4 (Allowed Step Operations) and §6.5 (Deletion Decision Rules).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, Field


class DataCategory(str, Enum):
    """Data categories defined in deletion_rules.csv."""

    USER_PROFILE = "USER_PROFILE"
    SECONDARY_IDENTIFIER = "SECONDARY_IDENTIFIER"
    SEARCH_INDEX_ENTRY = "SEARCH_INDEX_ENTRY"
    CACHE_ENTRY = "CACHE_ENTRY"
    SUPPORT_MESSAGE = "SUPPORT_MESSAGE"
    SUPPORT_ATTACHMENT = "SUPPORT_ATTACHMENT"
    ANALYTICS_PROFILE = "ANALYTICS_PROFILE"
    PRODUCT_TELEMETRY = "PRODUCT_TELEMETRY"
    USER_EXPORT = "USER_EXPORT"
    MARKETING_PROFILE = "MARKETING_PROFILE"
    EMAIL_DELIVERY_LOG = "EMAIL_DELIVERY_LOG"
    SECURITY_AUDIT_LOG = "SECURITY_AUDIT_LOG"
    LEGAL_HOLD_RECORD = "LEGAL_HOLD_RECORD"
    BACKUP_ARCHIVE = "BACKUP_ARCHIVE"
    ACCOUNT_TOMBSTONE = "ACCOUNT_TOMBSTONE"
    DATA_FLOW = "DATA_FLOW"
    UNKNOWN = "UNKNOWN"


class RequiredAction(str, Enum):
    """Required policy action defined in deletion_rules.csv."""

    DELETE = "DELETE"
    REDACT = "REDACT"
    ANONYMIZE = "ANONYMIZE"
    RETAIN = "RETAIN"


class ObservedState(str, Enum):
    """Resource state observed in evidence or activity."""

    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    REDACTED = "REDACTED"
    ANONYMIZED = "ANONYMIZED"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class FlowState(str, Enum):
    """Status of a synchronization connector or backup restore path."""

    CONFIRMED_ACTIVE = "CONFIRMED_ACTIVE"
    CONFIRMED_DISABLED = "CONFIRMED_DISABLED"
    POTENTIAL_RISK = "POTENTIAL_RISK"
    UNKNOWN = "UNKNOWN"


class PolicyRule(BaseModel):
    """Rule definition loaded from deletion_rules.csv."""

    rule_id: str
    data_category: DataCategory
    condition: str
    required_action: RequiredAction


class PolicyFinding(BaseModel):
    """Evaluation finding for a specific resource against applicable policy."""

    rule_id: str = Field(description="Policy rule ID from deletion_rules.csv")
    data_category: DataCategory = Field(description="Data category evaluated")
    system_id: str = Field(description="System identifier containing the resource")
    resource_id: str = Field(description="Resource identifier (e.g. account_005, search_001)")
    required_action: RequiredAction = Field(description="Required policy action")
    observed_state: ObservedState = Field(description="Observed state in evidence")
    is_violation: bool = Field(description="True if the observed state violates the policy rule")
    finding_operation: Optional[str] = Field(
        default=None,
        description="Allowed finding operation: REPORT_REMAINING_RECORD or REPORT_EXPOSED_PII",
    )
    remediation_operation: Optional[str] = Field(
        default=None,
        description="Allowed remediation operation: DELETE_RECORD, REDACT_RECORD, ANONYMIZE_RECORD",
    )
    verification_operation: str = Field(
        description="Allowed verification operation: VERIFY_RECORD_ABSENT, VERIFY_RECORD_PRESENT, etc.",
    )
    supporting_evidence_id: str = Field(
        default="",
        description="Participant-visible evidence_id proving this finding",
    )
    prohibited_identifiers: List[str] = Field(
        default_factory=list,
        description="Prohibited direct identifiers (email, phone, name, hash) found when not permitted",
    )
    details: str = Field(default="", description="Detailed rationale of the finding")


class ResurrectionRisk(BaseModel):
    """Risk finding for an active data flow or unblocked backup restore path."""

    flow_type: str = Field(description="'SYNC_FLOW' or 'RESTORE_PATH'")
    system_id: str = Field(description="System managing the flow (e.g. integration_service, backup_service)")
    resource_id: str = Field(description="Flow or backup identifier (e.g. data_flow_9522, backup_3870)")
    source_system: Optional[str] = Field(default=None, description="Originating system")
    target_system: Optional[str] = Field(default=None, description="Downstream destination system")
    flow_state: FlowState = Field(description="Evaluated state of the flow")
    is_hazard: bool = Field(description="True if this flow can recreate or restore deleted data")
    finding_operation: Optional[str] = Field(
        default=None,
        description="REPORT_ACTIVE_DATA_FLOW or REPORT_RESTORE_RISK",
    )
    remediation_operation: Optional[str] = Field(
        default=None,
        description="DISABLE_DATA_FLOW or BLOCK_RESTORE",
    )
    verification_operation: str = Field(
        description="VERIFY_DATA_FLOW_DISABLED or VERIFY_RESTORE_BLOCKED",
    )
    supporting_evidence_id: str = Field(
        default="",
        description="Participant-visible evidence_id proving this risk",
    )
    timestamp: Optional[str] = Field(default=None, description="Timestamp of observed activity")
    description: str = Field(default="", description="Detailed description of the resurrection hazard")
    uncertainty_note: Optional[str] = Field(default=None, description="Explanation if state is UNKNOWN/POTENTIAL")


class PolicyEvaluationResult(BaseModel):
    """Aggregated policy evaluation outcome for a case."""

    case_id: str
    user_id: Optional[str]
    audit_at: Optional[str]
    policy_findings: List[PolicyFinding] = Field(default_factory=list)
    resurrection_risks: List[ResurrectionRisk] = Field(default_factory=list)
    retained_records_compliant: bool = True
    active_violations_count: int = 0
    active_hazards_count: int = 0

    @property
    def has_violations(self) -> bool:
        """True if any policy violation or active resurrection hazard exists."""
        return self.active_violations_count > 0 or self.active_hazards_count > 0
