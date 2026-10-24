"""Audit step generation from policy findings and resurrection risks.

Converts structured PolicyFinding and ResurrectionRisk objects into the
3-phase step sequence required by the output contract.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..models import (
    ALLOWED_OPERATION_STATES,
    AuditStep,
    DeletionStatus,
    Operation,
    Phase,
    State,
)
from ..policy.models import (
    PolicyFinding,
    ResurrectionRisk,
)


# Canonical sort order for remediation operations.
# Per AGENTS.md §6.5: disable flows and block restores before records.
_REMEDIATION_ORDER: Dict[str, int] = {
    "DISABLE_DATA_FLOW": 0,
    "BLOCK_RESTORE": 1,
    "DELETE_RECORD": 2,
    "REDACT_RECORD": 3,
    "ANONYMIZE_RECORD": 4,
}

# Verification sort order mirrors remediation + retained/present check
_VERIFICATION_ORDER: Dict[str, int] = {
    "VERIFY_RECORD_ABSENT": 0,
    "VERIFY_RECORD_PRESENT": 1,
    "VERIFY_RECORD_REDACTED": 2,
    "VERIFY_RECORD_ANONYMIZED": 3,
    "VERIFY_DATA_FLOW_DISABLED": 4,
    "VERIFY_RESTORE_BLOCKED": 5,
}

# Finding sort order
_FINDING_ORDER: Dict[str, int] = {
    "REPORT_REMAINING_RECORD": 0,
    "REPORT_EXPOSED_PII": 1,
    "REPORT_ACTIVE_DATA_FLOW": 2,
    "REPORT_RESTORE_RISK": 3,
}


def _sort_key(phase: str, operation: str, system_id: str, resource_id: str) -> Tuple:
    """Generate a deterministic sort key for step ordering."""
    if phase == "FINDING":
        op_order = _FINDING_ORDER.get(operation, 99)
    elif phase == "REMEDIATION":
        op_order = _REMEDIATION_ORDER.get(operation, 99)
    else:
        op_order = _VERIFICATION_ORDER.get(operation, 99)

    # Phase order: FINDING=0, REMEDIATION=1, VERIFICATION=2
    phase_order = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}.get(phase, 99)
    return (phase_order, op_order, system_id, resource_id)


class StepGenerator:
    """Generates ordered AuditStep lists from policy findings and resurrection risks."""

    def generate_complete_steps(
        self,
        policy_findings: List[PolicyFinding],
        resurrection_risks: List[ResurrectionRisk],
        evidence_id_for_verification: str = "",
    ) -> List[AuditStep]:
        """Generate verification-only steps for a COMPLETE case.

        For COMPLETE cases, include only verification steps backed by evidence.
        Every step must have a non-empty evidence_id.
        """
        raw_steps: List[Tuple[Tuple, dict]] = []

        for finding in policy_findings:
            if finding.is_violation:
                # Cannot be COMPLETE if there are violations
                continue

            verification_op = finding.verification_operation
            ev_id = finding.supporting_evidence_id or evidence_id_for_verification

            if not ev_id:
                continue  # COMPLETE requires evidence

            op_enum = Operation(verification_op)
            expected_state = ALLOWED_OPERATION_STATES[Phase.VERIFICATION][op_enum]

            key = _sort_key("VERIFICATION", verification_op, finding.system_id, finding.resource_id)
            raw_steps.append((key, {
                "phase": Phase.VERIFICATION,
                "system_id": finding.system_id,
                "resource_id": finding.resource_id,
                "operation": op_enum,
                "state": expected_state,
                "evidence_id": ev_id,
            }))

        # Add verification for compliant flows/restores
        for risk in resurrection_risks:
            if risk.is_hazard:
                continue  # Cannot be COMPLETE if there are hazards

            verification_op = risk.verification_operation
            ev_id = risk.supporting_evidence_id or evidence_id_for_verification

            if not ev_id:
                continue

            op_enum = Operation(verification_op)
            expected_state = ALLOWED_OPERATION_STATES[Phase.VERIFICATION][op_enum]

            key = _sort_key("VERIFICATION", verification_op, risk.system_id, risk.resource_id)
            raw_steps.append((key, {
                "phase": Phase.VERIFICATION,
                "system_id": risk.system_id,
                "resource_id": risk.resource_id,
                "operation": op_enum,
                "state": expected_state,
                "evidence_id": ev_id,
            }))

        # Deduplicate by (system_id, resource_id, operation)
        seen = set()
        unique_steps = []
        for key, step_data in sorted(raw_steps, key=lambda x: x[0]):
            dedup_key = (step_data["system_id"], step_data["resource_id"], step_data["operation"].value)
            if dedup_key not in seen:
                seen.add(dedup_key)
                unique_steps.append(step_data)

        return self._number_steps(unique_steps)

    def generate_incomplete_steps(
        self,
        policy_findings: List[PolicyFinding],
        resurrection_risks: List[ResurrectionRisk],
    ) -> List[AuditStep]:
        """Generate 3-phase steps for an INCOMPLETE case.

        Phase order: FINDING → REMEDIATION → VERIFICATION.
        Findings and completed verification cite real evidence.
        Proposed remediation and future verification use empty evidence_id.
        """
        findings: List[Tuple[Tuple, dict]] = []
        remediations: List[Tuple[Tuple, dict]] = []
        verifications: List[Tuple[Tuple, dict]] = []

        # Process policy findings
        for f in policy_findings:
            if f.is_violation and f.finding_operation:
                op_enum = Operation(f.finding_operation)
                expected_state = ALLOWED_OPERATION_STATES[Phase.FINDING][op_enum]

                key = _sort_key("FINDING", f.finding_operation, f.system_id, f.resource_id)
                findings.append((key, {
                    "phase": Phase.FINDING,
                    "system_id": f.system_id,
                    "resource_id": f.resource_id,
                    "operation": op_enum,
                    "state": expected_state,
                    "evidence_id": f.supporting_evidence_id or "",
                }))

            if f.is_violation and f.remediation_operation:
                op_enum = Operation(f.remediation_operation)
                expected_state = ALLOWED_OPERATION_STATES[Phase.REMEDIATION][op_enum]

                key = _sort_key("REMEDIATION", f.remediation_operation, f.system_id, f.resource_id)
                remediations.append((key, {
                    "phase": Phase.REMEDIATION,
                    "system_id": f.system_id,
                    "resource_id": f.resource_id,
                    "operation": op_enum,
                    "state": expected_state,
                    "evidence_id": "",  # Proposed, not completed
                }))

            # Verification for every finding (both violated and compliant)
            verification_op = f.verification_operation
            op_enum = Operation(verification_op)
            expected_state = ALLOWED_OPERATION_STATES[Phase.VERIFICATION][op_enum]

            key = _sort_key("VERIFICATION", verification_op, f.system_id, f.resource_id)
            verifications.append((key, {
                "phase": Phase.VERIFICATION,
                "system_id": f.system_id,
                "resource_id": f.resource_id,
                "operation": op_enum,
                "state": expected_state,
                "evidence_id": "",  # Future verification
            }))

        # Process resurrection risks
        for risk in resurrection_risks:
            if risk.is_hazard and risk.finding_operation:
                op_enum = Operation(risk.finding_operation)
                expected_state = ALLOWED_OPERATION_STATES[Phase.FINDING][op_enum]

                key = _sort_key("FINDING", risk.finding_operation, risk.system_id, risk.resource_id)
                findings.append((key, {
                    "phase": Phase.FINDING,
                    "system_id": risk.system_id,
                    "resource_id": risk.resource_id,
                    "operation": op_enum,
                    "state": expected_state,
                    "evidence_id": risk.supporting_evidence_id or "",
                }))

            if risk.is_hazard and risk.remediation_operation:
                op_enum = Operation(risk.remediation_operation)
                expected_state = ALLOWED_OPERATION_STATES[Phase.REMEDIATION][op_enum]

                key = _sort_key("REMEDIATION", risk.remediation_operation, risk.system_id, risk.resource_id)
                remediations.append((key, {
                    "phase": Phase.REMEDIATION,
                    "system_id": risk.system_id,
                    "resource_id": risk.resource_id,
                    "operation": op_enum,
                    "state": expected_state,
                    "evidence_id": "",
                }))

            # Verification for the risk's resource
            verification_op = risk.verification_operation
            op_enum = Operation(verification_op)
            expected_state = ALLOWED_OPERATION_STATES[Phase.VERIFICATION][op_enum]

            key = _sort_key("VERIFICATION", verification_op, risk.system_id, risk.resource_id)
            verifications.append((key, {
                "phase": Phase.VERIFICATION,
                "system_id": risk.system_id,
                "resource_id": risk.resource_id,
                "operation": op_enum,
                "state": expected_state,
                "evidence_id": "",
            }))

        # Combine, sort, deduplicate, and number
        all_raw = findings + remediations + verifications
        seen = set()
        ordered = []
        for key, step_data in sorted(all_raw, key=lambda x: x[0]):
            dedup_key = (
                step_data["phase"].value,
                step_data["system_id"],
                step_data["resource_id"],
                step_data["operation"].value,
            )
            if dedup_key not in seen:
                seen.add(dedup_key)
                ordered.append(step_data)

        return self._number_steps(ordered)

    def _number_steps(self, step_dicts: List[dict]) -> List[AuditStep]:
        """Assign consecutive step_no values and create AuditStep objects."""
        steps = []
        for i, sd in enumerate(step_dicts, start=1):
            steps.append(AuditStep(step_no=i, **sd))
        return steps
