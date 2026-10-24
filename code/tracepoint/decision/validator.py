"""Independent step and output row validator for Tracepoint.

Validates generated audit steps and output rows against the Pydantic contract
and additional semantic rules. Produces clear diagnostics for every violation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from ..models import (
    ALLOWED_OPERATION_STATES,
    AuditStep,
    DeletionStatus,
    OutputRow,
    Phase,
    Operation,
    State,
)


class DiagnosticLevel(str, Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass
class ValidationDiagnostic:
    """A single validation finding."""

    level: DiagnosticLevel
    code: str
    message: str
    step_no: Optional[int] = None
    case_id: Optional[str] = None

    def __str__(self) -> str:
        loc = ""
        if self.case_id:
            loc += f"[{self.case_id}]"
        if self.step_no:
            loc += f"[step {self.step_no}]"
        return f"{self.level.value} {loc} {self.code}: {self.message}"


@dataclass
class ValidationResult:
    """Aggregated validation outcome."""

    is_valid: bool
    diagnostics: List[ValidationDiagnostic] = field(default_factory=list)

    @property
    def errors(self) -> List[ValidationDiagnostic]:
        return [d for d in self.diagnostics if d.level == DiagnosticLevel.ERROR]

    @property
    def warnings(self) -> List[ValidationDiagnostic]:
        return [d for d in self.diagnostics if d.level == DiagnosticLevel.WARNING]

    def summary(self) -> str:
        return (
            f"Valid={self.is_valid}, "
            f"Errors={len(self.errors)}, "
            f"Warnings={len(self.warnings)}, "
            f"Total={len(self.diagnostics)}"
        )


class StepValidator:
    """Validates audit steps and output rows against the output contract.

    Parameters
    ----------
    known_evidence_ids:
        Optional set of participant-visible evidence IDs. When provided,
        any cited evidence_id not in this set is flagged as fabricated.
    """

    def __init__(self, known_evidence_ids: Optional[Set[str]] = None) -> None:
        self._known_evidence_ids = known_evidence_ids

    def validate_steps(
        self,
        steps: List[AuditStep],
        deletion_status: DeletionStatus,
        case_id: str = "",
    ) -> ValidationResult:
        """Validate a list of AuditSteps against the output contract.

        Returns ValidationResult with diagnostics. is_valid is True only
        when there are no ERROR-level diagnostics.
        """
        diags: List[ValidationDiagnostic] = []

        # Rule 1: NOT_APPLICABLE must have empty steps
        if deletion_status == DeletionStatus.NOT_APPLICABLE:
            if len(steps) > 0:
                diags.append(ValidationDiagnostic(
                    level=DiagnosticLevel.ERROR,
                    code="NA_NON_EMPTY",
                    message="NOT_APPLICABLE case must have steps_json=[]",
                    case_id=case_id,
                ))
            return ValidationResult(
                is_valid=len([d for d in diags if d.level == DiagnosticLevel.ERROR]) == 0,
                diagnostics=diags,
            )

        # Rule 2: Consecutive step numbers starting from 1
        for i, step in enumerate(steps):
            if step.step_no != i + 1:
                diags.append(ValidationDiagnostic(
                    level=DiagnosticLevel.ERROR,
                    code="STEP_NUMBERING",
                    message=f"Expected step_no={i+1}, got {step.step_no}",
                    step_no=step.step_no,
                    case_id=case_id,
                ))

        # Rule 3: Phase/Operation/State validation
        for step in steps:
            phase_map = ALLOWED_OPERATION_STATES.get(step.phase, {})
            if step.operation not in phase_map:
                diags.append(ValidationDiagnostic(
                    level=DiagnosticLevel.ERROR,
                    code="INVALID_PHASE_OPERATION",
                    message=f"Operation {step.operation.value} not allowed in phase {step.phase.value}",
                    step_no=step.step_no,
                    case_id=case_id,
                ))
            else:
                expected_state = phase_map[step.operation]
                if step.state != expected_state:
                    diags.append(ValidationDiagnostic(
                        level=DiagnosticLevel.ERROR,
                        code="INVALID_OPERATION_STATE",
                        message=f"Operation {step.operation.value} requires state {expected_state.value}, got {step.state.value}",
                        step_no=step.step_no,
                        case_id=case_id,
                    ))

        # Rule 4: Phase ordering (FINDING < REMEDIATION < VERIFICATION)
        phase_rank = {Phase.FINDING: 0, Phase.REMEDIATION: 1, Phase.VERIFICATION: 2}
        for i in range(1, len(steps)):
            prev_rank = phase_rank.get(steps[i - 1].phase, -1)
            curr_rank = phase_rank.get(steps[i].phase, -1)
            if curr_rank < prev_rank:
                diags.append(ValidationDiagnostic(
                    level=DiagnosticLevel.ERROR,
                    code="PHASE_ORDER",
                    message=(
                        f"Step {steps[i].step_no} ({steps[i].phase.value}) "
                        f"precedes step {steps[i-1].step_no} ({steps[i-1].phase.value}) "
                        f"but phase order requires FINDING → REMEDIATION → VERIFICATION"
                    ),
                    step_no=steps[i].step_no,
                    case_id=case_id,
                ))

        # Rule 5: COMPLETE cases — verification only, all with evidence
        if deletion_status == DeletionStatus.COMPLETE:
            for step in steps:
                if step.phase != Phase.VERIFICATION:
                    diags.append(ValidationDiagnostic(
                        level=DiagnosticLevel.ERROR,
                        code="COMPLETE_NON_VERIFICATION",
                        message=f"COMPLETE case must only contain VERIFICATION steps, found {step.phase.value}",
                        step_no=step.step_no,
                        case_id=case_id,
                    ))
                if not step.evidence_id:
                    diags.append(ValidationDiagnostic(
                        level=DiagnosticLevel.ERROR,
                        code="COMPLETE_MISSING_EVIDENCE",
                        message=f"VERIFICATION step in COMPLETE case must have non-empty evidence_id",
                        step_no=step.step_no,
                        case_id=case_id,
                    ))

        # Rule 6: INCOMPLETE cases — must have all three phases
        if deletion_status == DeletionStatus.INCOMPLETE:
            phases_present = {s.phase for s in steps}
            for required_phase in (Phase.FINDING, Phase.REMEDIATION, Phase.VERIFICATION):
                if required_phase not in phases_present:
                    diags.append(ValidationDiagnostic(
                        level=DiagnosticLevel.ERROR,
                        code="INCOMPLETE_MISSING_PHASE",
                        message=f"INCOMPLETE case must include {required_phase.value} phase",
                        case_id=case_id,
                    ))

            # Finding steps should cite evidence
            for step in steps:
                if step.phase == Phase.FINDING and not step.evidence_id:
                    diags.append(ValidationDiagnostic(
                        level=DiagnosticLevel.WARNING,
                        code="FINDING_MISSING_EVIDENCE",
                        message=f"FINDING step should cite supporting evidence",
                        step_no=step.step_no,
                        case_id=case_id,
                    ))

            # Remediation and future verification must NOT have evidence
            for step in steps:
                if step.phase == Phase.REMEDIATION and step.evidence_id:
                    diags.append(ValidationDiagnostic(
                        level=DiagnosticLevel.WARNING,
                        code="REMEDIATION_HAS_EVIDENCE",
                        message="Proposed remediation step should have empty evidence_id",
                        step_no=step.step_no,
                        case_id=case_id,
                    ))

        # Rule 7: Fabricated evidence check
        if self._known_evidence_ids is not None:
            for step in steps:
                if step.evidence_id and step.evidence_id not in self._known_evidence_ids:
                    diags.append(ValidationDiagnostic(
                        level=DiagnosticLevel.ERROR,
                        code="FABRICATED_EVIDENCE",
                        message=f"evidence_id '{step.evidence_id}' is not in known evidence set",
                        step_no=step.step_no,
                        case_id=case_id,
                    ))

        # Rule 8: Duplicate steps
        step_keys = set()
        for step in steps:
            key = (step.phase.value, step.system_id, step.resource_id, step.operation.value)
            if key in step_keys:
                diags.append(ValidationDiagnostic(
                    level=DiagnosticLevel.WARNING,
                    code="DUPLICATE_STEP",
                    message=f"Duplicate step: {step.phase.value}/{step.operation.value} for {step.system_id}/{step.resource_id}",
                    step_no=step.step_no,
                    case_id=case_id,
                ))
            step_keys.add(key)

        is_valid = len([d for d in diags if d.level == DiagnosticLevel.ERROR]) == 0
        return ValidationResult(is_valid=is_valid, diagnostics=diags)

    def validate_output_row(self, row: OutputRow) -> ValidationResult:
        """Validate a complete OutputRow (case_id + status + steps)."""
        # First validate using Pydantic (already done at construction time)
        # Then validate steps semantically
        return self.validate_steps(
            steps=row.steps,
            deletion_status=row.deletion_status,
            case_id=row.case_id,
        )
