"""Domain models and schema definitions for Tracepoint."""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class Phase(str, Enum):
    FINDING = "FINDING"
    REMEDIATION = "REMEDIATION"
    VERIFICATION = "VERIFICATION"


class Operation(str, Enum):
    # Findings
    REPORT_REMAINING_RECORD = "REPORT_REMAINING_RECORD"
    REPORT_EXPOSED_PII = "REPORT_EXPOSED_PII"
    REPORT_ACTIVE_DATA_FLOW = "REPORT_ACTIVE_DATA_FLOW"
    REPORT_RESTORE_RISK = "REPORT_RESTORE_RISK"

    # Remediation
    DISABLE_DATA_FLOW = "DISABLE_DATA_FLOW"
    BLOCK_RESTORE = "BLOCK_RESTORE"
    DELETE_RECORD = "DELETE_RECORD"
    REDACT_RECORD = "REDACT_RECORD"
    ANONYMIZE_RECORD = "ANONYMIZED_RECORD" if False else "ANONYMIZE_RECORD"

    # Verification
    VERIFY_RECORD_ABSENT = "VERIFY_RECORD_ABSENT"
    VERIFY_RECORD_PRESENT = "VERIFY_RECORD_PRESENT"
    VERIFY_RECORD_REDACTED = "VERIFY_RECORD_REDACTED"
    VERIFY_RECORD_ANONYMIZED = "VERIFY_RECORD_ANONYMIZED"
    VERIFY_DATA_FLOW_DISABLED = "VERIFY_DATA_FLOW_DISABLED"
    VERIFY_RESTORE_BLOCKED = "VERIFY_RESTORE_BLOCKED"


class State(str, Enum):
    PRESENT = "PRESENT"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    BLOCKED = "BLOCKED"
    ABSENT = "ABSENT"
    REDACTED = "REDACTED"
    ANONYMIZED = "ANONYMIZED"


class DeletionStatus(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


# Strict pairing of Phase -> Operation -> State
ALLOWED_OPERATION_STATES: Dict[Phase, Dict[Operation, State]] = {
    Phase.FINDING: {
        Operation.REPORT_REMAINING_RECORD: State.PRESENT,
        Operation.REPORT_EXPOSED_PII: State.PRESENT,
        Operation.REPORT_ACTIVE_DATA_FLOW: State.ACTIVE,
        Operation.REPORT_RESTORE_RISK: State.ACTIVE,
    },
    Phase.REMEDIATION: {
        Operation.DISABLE_DATA_FLOW: State.DISABLED,
        Operation.BLOCK_RESTORE: State.BLOCKED,
        Operation.DELETE_RECORD: State.ABSENT,
        Operation.REDACT_RECORD: State.REDACTED,
        Operation.ANONYMIZE_RECORD: State.ANONYMIZED,
    },
    Phase.VERIFICATION: {
        Operation.VERIFY_RECORD_ABSENT: State.ABSENT,
        Operation.VERIFY_RECORD_PRESENT: State.PRESENT,
        Operation.VERIFY_RECORD_REDACTED: State.REDACTED,
        Operation.VERIFY_RECORD_ANONYMIZED: State.ANONYMIZED,
        Operation.VERIFY_DATA_FLOW_DISABLED: State.DISABLED,
        Operation.VERIFY_RESTORE_BLOCKED: State.BLOCKED,
    },
}


class AuditStep(BaseModel):
    """A single audit step in steps_json."""

    step_no: int = Field(ge=1, description="1-indexed consecutive step number")
    phase: Phase
    system_id: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    operation: Operation
    state: State
    evidence_id: str = Field(default="")

    @model_validator(mode="after")
    def validate_operation_and_state(self) -> AuditStep:
        phase_map = ALLOWED_OPERATION_STATES.get(self.phase, {})
        if self.operation not in phase_map:
            raise ValueError(
                f"Operation '{self.operation.value}' is not permitted in phase '{self.phase.value}'"
            )
        expected_state = phase_map[self.operation]
        if self.state != expected_state:
            raise ValueError(
                f"State '{self.state.value}' is invalid for operation '{self.operation.value}'. "
                f"Expected state '{expected_state.value}'."
            )
        return self


class DeletionCase(BaseModel):
    """Input request case schema."""

    case_id: str
    email: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    request_text: str


class OutputRow(BaseModel):
    """Single row schema for final output.csv."""

    case_id: str
    deletion_status: DeletionStatus
    steps: List[AuditStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_case_rules(self) -> OutputRow:
        if self.deletion_status == DeletionStatus.NOT_APPLICABLE:
            if len(self.steps) > 0:
                raise ValueError("NOT_APPLICABLE cases must have empty steps_json: []")
        elif self.deletion_status == DeletionStatus.COMPLETE:
            for s in self.steps:
                if s.phase != Phase.VERIFICATION:
                    raise ValueError(
                        f"COMPLETE cases must contain only VERIFICATION steps, found {s.phase.value}"
                    )
                if not s.evidence_id:
                    raise ValueError(
                        f"VERIFICATION step {s.step_no} in COMPLETE case must have a non-empty evidence_id"
                    )
        elif self.deletion_status == DeletionStatus.INCOMPLETE:
            has_finding = any(s.phase == Phase.FINDING for s in self.steps)
            has_remediation = any(s.phase == Phase.REMEDIATION for s in self.steps)
            has_verification = any(s.phase == Phase.VERIFICATION for s in self.steps)
            if not has_finding or not has_remediation or not has_verification:
                raise ValueError(
                    "INCOMPLETE cases must include FINDING, REMEDIATION, and VERIFICATION phases"
                )
        return self

    @property
    def steps_json(self) -> str:
        """Serialize steps as a valid JSON string."""
        return json.dumps([s.model_dump(mode="json") for s in self.steps], separators=(",", ":"))

    def to_csv_dict(self) -> Dict[str, str]:
        """Convert to dict matching output.csv columns: case_id,deletion_status,steps_json."""
        return {
            "case_id": self.case_id,
            "deletion_status": self.deletion_status.value,
            "steps_json": self.steps_json,
        }

