"""Tests for domain models and contract enforcement."""

import pytest
from pydantic import ValidationError

try:
    from tracepoint.models import (
        AuditStep,
        DeletionStatus,
        Operation,
        OutputRow,
        Phase,
        State,
    )
except ImportError:
    from code.tracepoint.models import (
        AuditStep,
        DeletionStatus,
        Operation,
        OutputRow,
        Phase,
        State,
    )


def test_valid_audit_step():
    """Verify that valid operation and state pairs instantiate correctly."""
    step = AuditStep(
        step_no=1,
        phase=Phase.FINDING,
        system_id="account_service",
        resource_id="account_001",
        operation=Operation.REPORT_REMAINING_RECORD,
        state=State.PRESENT,
        evidence_id="evidence_54",
    )
    assert step.step_no == 1
    assert step.phase == Phase.FINDING
    assert step.operation == Operation.REPORT_REMAINING_RECORD
    assert step.state == State.PRESENT


def test_invalid_operation_state_pair():
    """Verify that mismatching operation and state raises validation error."""
    with pytest.raises(ValidationError):
        AuditStep(
            step_no=1,
            phase=Phase.FINDING,
            system_id="account_service",
            resource_id="account_001",
            operation=Operation.REPORT_REMAINING_RECORD,
            state=State.ABSENT,  # REPORT_REMAINING_RECORD requires PRESENT
            evidence_id="evidence_54",
        )


def test_invalid_phase_operation_pair():
    """Verify that an operation used in the wrong phase raises validation error."""
    with pytest.raises(ValidationError):
        AuditStep(
            step_no=1,
            phase=Phase.FINDING,
            system_id="account_service",
            resource_id="account_001",
            operation=Operation.DELETE_RECORD,  # Remediation op in Finding phase
            state=State.ABSENT,
            evidence_id="",
        )


def test_output_row_complete_validation():
    """Verify that COMPLETE output row enforces verification steps with evidence."""
    step = AuditStep(
        step_no=1,
        phase=Phase.VERIFICATION,
        system_id="account_service",
        resource_id="account_001",
        operation=Operation.VERIFY_RECORD_ABSENT,
        state=State.ABSENT,
        evidence_id="evidence_54",
    )
    row = OutputRow(
        case_id="case_01",
        deletion_status=DeletionStatus.COMPLETE,
        steps=[step],
    )
    assert row.deletion_status == DeletionStatus.COMPLETE

    # Fails if COMPLETE case has empty evidence_id
    invalid_step = AuditStep(
        step_no=1,
        phase=Phase.VERIFICATION,
        system_id="account_service",
        resource_id="account_001",
        operation=Operation.VERIFY_RECORD_ABSENT,
        state=State.ABSENT,
        evidence_id="",
    )
    with pytest.raises(ValidationError):
        OutputRow(
            case_id="case_01",
            deletion_status=DeletionStatus.COMPLETE,
            steps=[invalid_step],
        )


def test_output_row_not_applicable_validation():
    """Verify that NOT_APPLICABLE requires empty steps array."""
    row = OutputRow(
        case_id="case_535",
        deletion_status=DeletionStatus.NOT_APPLICABLE,
        steps=[],
    )
    assert row.steps == []

    # Fails if NOT_APPLICABLE has steps
    step = AuditStep(
        step_no=1,
        phase=Phase.VERIFICATION,
        system_id="account_service",
        resource_id="account_001",
        operation=Operation.VERIFY_RECORD_ABSENT,
        state=State.ABSENT,
        evidence_id="evidence_01",
    )
    with pytest.raises(ValidationError):
        OutputRow(
            case_id="case_535",
            deletion_status=DeletionStatus.NOT_APPLICABLE,
            steps=[step],
        )
