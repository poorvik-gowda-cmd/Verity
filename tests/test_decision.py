"""Unit and integration tests for Phase 7: Proof-Driven Decision & Remediation Engine."""

import json
import pytest
import pandas as pd

from tracepoint.models import (
    AuditStep,
    DeletionCase,
    DeletionStatus,
    Operation,
    OutputRow,
    Phase,
    State,
)
from tracepoint.policy import (
    DataCategory,
    FlowState,
    ObservedState,
    PolicyEvaluator,
    PolicyFinding,
    PolicyRuleRegistry,
    RequiredAction,
    ResurrectionRisk,
    ResurrectionRiskAnalyzer,
)
from tracepoint.decision import (
    CaseDecision,
    DecisionEngine,
    DiagnosticLevel,
    RequestClassifier,
    StepGenerator,
    StepValidator,
    ValidationResult,
)


# ---------------------------------------------------------------------------
# 1. Request Classifier Tests
# ---------------------------------------------------------------------------


def test_request_classifier_deletion_intents():
    """Verify various language patterns are recognized as deletion requests."""
    classifier = RequestClassifier()

    cases = [
        "Please delete all my personal data and close my account.",
        "I want my account to be permanently deleted under GDPR Article 17.",
        "Solicito la eliminación completa de mis datos personales y mi cuenta.",
        "Je demande la suppression définitive de mes données et de mon profil.",
        "Bitte löschen Sie mein Konto und alle damit verbundenen personenbezogenen Daten.",
        "Peço a exclusão completa de todos os meus dados e encerramento da conta.",
        "Kindly purge all records associated with my profile immediately.",
        "I haven't used this in years, please close and remove my account.",
        "Please complete the deletion of my profile.",
    ]

    for text in cases:
        is_del, reason = classifier.classify(text)
        assert is_del, f"Expected deletion for: '{text}' (reason: {reason})"


def test_request_classifier_non_deletion_intents():
    """Verify non-deletion requests (access, export, rectification, opt-out) return False."""
    classifier = RequestClassifier()

    cases = [
        "I would like to access and download a copy of my personal data under GDPR Article 15.",
        "Please export all my transaction history and data portability file.",
        "I need to update my email address and correct my home address.",
        "Please rectify my phone number in your records.",
        "Unsubscribe me from all marketing emails and promotional newsletters.",
        "Please stop sending me emails, opt-out of marketing.",
        "What data do you have stored about me?",
        "Please tell me what information you are keeping on my profile.",
    ]

    for text in cases:
        is_del, reason = classifier.classify(text)
        assert not is_del, f"Expected non-deletion for: '{text}' (reason: {reason})"


def test_request_classifier_mixed_intent_deletion_prevails():
    """Verify that if deletion is requested alongside data copy/export, deletion intent prevails."""
    classifier = RequestClassifier()
    text = "Please delete my account and personal data, but first send me a copy of my receipts."
    is_del, reason = classifier.classify(text)
    assert is_del, f"Expected deletion to prevail in mixed intent, got reason: {reason}"


def test_request_classifier_empty_text():
    """Verify empty or whitespace-only text is rejected as non-deletion."""
    classifier = RequestClassifier()
    assert not classifier.classify("")[0]
    assert not classifier.classify("   \n\t")[0]


# ---------------------------------------------------------------------------
# 2. Step Generator Tests
# ---------------------------------------------------------------------------


def test_step_generator_remediation_ordering():
    """Verify data flows and restore risks are remediated BEFORE record deletions/redactions."""
    gen = StepGenerator()

    findings = [
        PolicyFinding(
            rule_id="RULE_001",
            data_category=DataCategory.USER_PROFILE,
            required_action=RequiredAction.DELETE,
            system_id="account_service",
            resource_id="acc_01",
            observed_state=ObservedState.PRESENT,
            is_violation=True,
            finding_operation="REPORT_REMAINING_RECORD",
            remediation_operation="DELETE_RECORD",
            verification_operation="VERIFY_RECORD_ABSENT",
            supporting_evidence_id="ev_01",
        ),
        PolicyFinding(
            rule_id="RULE_005",
            data_category=DataCategory.ANALYTICS_PROFILE,
            required_action=RequiredAction.ANONYMIZE,
            system_id="analytics_warehouse",
            resource_id="an_01",
            observed_state=ObservedState.PRESENT,
            is_violation=True,
            finding_operation="REPORT_REMAINING_RECORD",
            remediation_operation="ANONYMIZE_RECORD",
            verification_operation="VERIFY_RECORD_ANONYMIZED",
            supporting_evidence_id="ev_02",
        ),
    ]

    risks = [
        ResurrectionRisk(
            flow_type="SYNC_FLOW",
            system_id="sync_service",
            resource_id="sync_01",
            source_system="primary_db",
            target_system="replica_db",
            flow_state=FlowState.CONFIRMED_ACTIVE,
            is_hazard=True,
            finding_operation="REPORT_ACTIVE_DATA_FLOW",
            remediation_operation="DISABLE_DATA_FLOW",
            verification_operation="VERIFY_DATA_FLOW_DISABLED",
            supporting_evidence_id="ev_03",
        ),
        ResurrectionRisk(
            flow_type="RESTORE_PATH",
            system_id="backup_service",
            resource_id="backup_01",
            source_system="primary_db",
            target_system="archive",
            flow_state=FlowState.CONFIRMED_ACTIVE,
            is_hazard=True,
            finding_operation="REPORT_RESTORE_RISK",
            remediation_operation="BLOCK_RESTORE",
            verification_operation="VERIFY_RESTORE_BLOCKED",
            supporting_evidence_id="ev_04",
        ),
    ]

    steps = gen.generate_incomplete_steps(findings, risks)

    # Filter remediation steps
    remediation_ops = [s.operation.value for s in steps if s.phase == Phase.REMEDIATION]

    # DISABLE_DATA_FLOW and BLOCK_RESTORE must come before DELETE_RECORD and ANONYMIZE_RECORD
    disable_idx = remediation_ops.index("DISABLE_DATA_FLOW")
    block_idx = remediation_ops.index("BLOCK_RESTORE")
    delete_idx = remediation_ops.index("DELETE_RECORD")
    anonymize_idx = remediation_ops.index("ANONYMIZE_RECORD")

    assert disable_idx < delete_idx
    assert disable_idx < anonymize_idx
    assert block_idx < delete_idx
    assert block_idx < anonymize_idx


def test_step_generator_three_phase_structure():
    """Verify 3 phases appear strictly in FINDING -> REMEDIATION -> VERIFICATION order."""
    gen = StepGenerator()

    findings = [
        PolicyFinding(
            rule_id="RULE_001",
            data_category=DataCategory.USER_PROFILE,
            required_action=RequiredAction.DELETE,
            system_id="account_service",
            resource_id="acc_01",
            observed_state=ObservedState.PRESENT,
            is_violation=True,
            finding_operation="REPORT_REMAINING_RECORD",
            remediation_operation="DELETE_RECORD",
            verification_operation="VERIFY_RECORD_ABSENT",
            supporting_evidence_id="ev_01",
        )
    ]
    risks = []

    steps = gen.generate_incomplete_steps(findings, risks)
    assert len(steps) >= 3

    phases = [s.phase for s in steps]
    # Check monotonic phase rank
    phase_order = {Phase.FINDING: 0, Phase.REMEDIATION: 1, Phase.VERIFICATION: 2}
    ranks = [phase_order[p] for p in phases]
    assert ranks == sorted(ranks), "Phases are not monotonically increasing in rank"

    # Step numbering is consecutive 1..N
    assert [s.step_no for s in steps] == list(range(1, len(steps) + 1))


def test_step_generator_complete_steps_verification_only():
    """Verify COMPLETE cases generate only verification steps with evidence."""
    gen = StepGenerator()

    findings = [
        PolicyFinding(
            rule_id="RULE_001",
            data_category=DataCategory.USER_PROFILE,
            required_action=RequiredAction.DELETE,
            system_id="account_service",
            resource_id="acc_01",
            observed_state=ObservedState.ABSENT,
            is_violation=False,
            verification_operation="VERIFY_RECORD_ABSENT",
            supporting_evidence_id="ev_verified_absent",
        )
    ]
    risks = [
        ResurrectionRisk(
            flow_type="SYNC_FLOW",
            system_id="sync_service",
            resource_id="sync_01",
            source_system="primary_db",
            target_system="replica_db",
            flow_state=FlowState.CONFIRMED_DISABLED,
            is_hazard=False,
            verification_operation="VERIFY_DATA_FLOW_DISABLED",
            supporting_evidence_id="ev_verified_disabled",
        )
    ]

    steps = gen.generate_complete_steps(findings, risks)
    assert len(steps) == 2
    for s in steps:
        assert s.phase == Phase.VERIFICATION
        assert s.evidence_id != ""
        assert s.step_no in (1, 2)


# ---------------------------------------------------------------------------
# 3. Step Validator Tests
# ---------------------------------------------------------------------------


def test_validator_detects_violations():
    """Verify independent validator catches contract violations."""
    validator = StepValidator()

    # Violation 1: NOT_APPLICABLE with non-empty steps
    step1 = AuditStep(
        step_no=1,
        phase=Phase.FINDING,
        system_id="sys",
        resource_id="res",
        operation=Operation.REPORT_REMAINING_RECORD,
        state=State.PRESENT,
        evidence_id="ev_01",
    )
    res_na = validator.validate_steps([step1], DeletionStatus.NOT_APPLICABLE)
    assert not res_na.is_valid
    assert any(e.code == "NA_NON_EMPTY" for e in res_na.errors)

    # Violation 2: Incorrect step numbering
    step_bad_num = AuditStep(
        step_no=5,
        phase=Phase.FINDING,
        system_id="sys",
        resource_id="res",
        operation=Operation.REPORT_REMAINING_RECORD,
        state=State.PRESENT,
        evidence_id="ev_01",
    )
    res_num = validator.validate_steps([step_bad_num], DeletionStatus.INCOMPLETE)
    assert not res_num.is_valid
    assert any(e.code == "STEP_NUMBERING" for e in res_num.errors)

    # Violation 3: Incomplete case missing finding or remediation or verification
    res_missing = validator.validate_steps([step1], DeletionStatus.INCOMPLETE)
    assert not res_missing.is_valid
    assert any(e.code == "INCOMPLETE_MISSING_PHASE" for e in res_missing.errors)


def test_validator_known_evidence_ids():
    """Verify validator flags citations of evidence IDs not in the manifest."""
    validator = StepValidator(known_evidence_ids={"ev_01", "ev_02"})

    step = AuditStep(
        step_no=1,
        phase=Phase.VERIFICATION,
        system_id="sys",
        resource_id="res",
        operation=Operation.VERIFY_RECORD_ABSENT,
        state=State.ABSENT,
        evidence_id="ev_fabricated_999",
    )

    res = validator.validate_steps([step], DeletionStatus.COMPLETE)
    assert not res.is_valid
    assert any(e.code == "FABRICATED_EVIDENCE" for e in res.errors)


# ---------------------------------------------------------------------------
# 4. Decision Engine End-to-End Tests
# ---------------------------------------------------------------------------


def test_decision_engine_not_applicable_flow():
    """Verify non-deletion request is marked NOT_APPLICABLE with steps_json=[]."""
    engine = DecisionEngine()
    case = DeletionCase(
        case_id="case_na_01",
        email="user@example.com",
        requested_at="2026-10-01T10:00:00Z",
        reported_status="NOT_STARTED",
        reported_at="2026-10-01T10:00:00Z",
        audit_at="2026-10-02T10:00:00Z",
        request_text="I would like to export a copy of all my data under data portability rules.",
    )

    decision = engine.decide_case(case)
    assert decision.deletion_status == DeletionStatus.NOT_APPLICABLE
    assert decision.steps == []
    assert decision.output_row is not None
    assert decision.output_row.deletion_status == DeletionStatus.NOT_APPLICABLE
    assert decision.output_row.steps == []
    assert json.loads(decision.output_row.steps_json) == []
    assert decision.is_valid


def test_decision_engine_incomplete_due_to_violation():
    """Verify an active violation causes INCOMPLETE with validated 3-phase steps."""
    registry = PolicyRuleRegistry.load_from_csv()
    evaluator = PolicyEvaluator(registry)
    analyzer = ResurrectionRiskAnalyzer()

    # Mock resolver & timeline builder
    class DummyResolver:
        def resolve(self, case_id, email):
            from tracepoint.resolver import ResolutionResult, ResolutionStatus
            return ResolutionResult(
                case_id=case_id,
                supplied_email=email,
                status=ResolutionStatus.RESOLVED,
                user_id="user_001",
                primary_link=None,
                current_email=email,
            )

        def get_user_identity(self, user_id):
            return {"name": "Alice Smith", "phone": "+15551234567", "email_hash": "hash123"}

    class DummyTimelineBuilder:
        def build_timeline(self, resolution, audit_at):
            from tracepoint.timeline import EventKind, EventSource, Timeline, TimelineEvent, parse_timestamp
            events = [
                TimelineEvent(
                    source=EventSource.EVIDENCE_SNAPSHOT,
                    kind=EventKind.SNAPSHOT_PRESENT,
                    system_id="account_service",
                    resource_id="acc_001",
                    timestamp=parse_timestamp("2026-10-01T12:00:00Z"),
                    raw_timestamp="2026-10-01T12:00:00Z",
                    reported_result=None,
                    description="User profile still present in DB snapshot",
                    evidence_id="ev_db_snap_1",
                    locator=None,
                    metadata={"details": "Alice Smith alice@example.com"},
                )
            ]
            return Timeline(
                user_id=resolution.user_id or "user_001",
                case_id=resolution.case_id,
                audit_at=parse_timestamp(audit_at),
                audit_at_raw=audit_at,
                search_terms=[resolution.supplied_email],
                events=events,
            )

    engine = DecisionEngine(
        resolver=DummyResolver(),
        timeline_builder=DummyTimelineBuilder(),
        policy_evaluator=evaluator,
        resurrection_analyzer=analyzer,
        known_evidence_ids={"ev_db_snap_1"},
    )

    case = DeletionCase(
        case_id="case_inc_01",
        email="alice@example.com",
        requested_at="2026-09-01T10:00:00Z",
        reported_status="COMPLETE",
        reported_at="2026-09-02T10:00:00Z",
        audit_at="2026-10-01T15:00:00Z",
        request_text="Please delete my account and all associated personal data permanently.",
    )

    decision = engine.decide_case(case)
    assert decision.deletion_status == DeletionStatus.INCOMPLETE
    assert len(decision.steps) >= 3
    assert decision.is_valid
    assert decision.output_row is not None

    # Check finding cites real evidence, remediation uses empty string
    findings = [s for s in decision.steps if s.phase == Phase.FINDING]
    remediations = [s for s in decision.steps if s.phase == Phase.REMEDIATION]
    verifications = [s for s in decision.steps if s.phase == Phase.VERIFICATION]

    assert len(findings) > 0
    assert len(remediations) > 0
    assert len(verifications) > 0

    assert findings[0].evidence_id == "ev_db_snap_1"
    assert remediations[0].evidence_id == ""
    assert verifications[0].evidence_id == ""


def test_decision_engine_complete_when_verified():
    """Verify compliant records with positive evidence yield COMPLETE status."""
    registry = PolicyRuleRegistry.load_from_csv()
    evaluator = PolicyEvaluator(registry)
    analyzer = ResurrectionRiskAnalyzer()

    class CompliantResolver:
        def resolve(self, case_id, email):
            from tracepoint.resolver import ResolutionResult, ResolutionStatus
            return ResolutionResult(
                case_id=case_id,
                supplied_email=email,
                status=ResolutionStatus.RESOLVED,
                user_id="user_002",
                primary_link=None,
                current_email=email,
            )

        def get_user_identity(self, user_id):
            return {"name": "Bob Jones", "phone": "+15559876543", "email_hash": "hashbob"}

    class CompliantTimelineBuilder:
        def build_timeline(self, resolution, audit_at):
            from tracepoint.timeline import EventKind, EventSource, Timeline, TimelineEvent, parse_timestamp
            events = [
                # Verified absent from account service
                TimelineEvent(
                    source=EventSource.EVIDENCE_SNAPSHOT,
                    kind=EventKind.SNAPSHOT_ABSENT,
                    system_id="account_service",
                    resource_id="acc_002",
                    timestamp=parse_timestamp("2026-10-01T12:00:00Z"),
                    raw_timestamp="2026-10-01T12:00:00Z",
                    reported_result=None,
                    description="User account record verified absent",
                    evidence_id="ev_verified_db_02",
                    locator=None,
                ),
            ]
            return Timeline(
                user_id=resolution.user_id or "user_002",
                case_id=resolution.case_id,
                audit_at=parse_timestamp(audit_at),
                audit_at_raw=audit_at,
                search_terms=[resolution.supplied_email],
                events=events,
            )

    engine = DecisionEngine(
        resolver=CompliantResolver(),
        timeline_builder=CompliantTimelineBuilder(),
        policy_evaluator=evaluator,
        resurrection_analyzer=analyzer,
        known_evidence_ids={"ev_verified_db_02"},
    )

    case = DeletionCase(
        case_id="case_comp_01",
        email="bob@example.com",
        requested_at="2026-09-01T10:00:00Z",
        reported_status="COMPLETE",
        reported_at="2026-09-02T10:00:00Z",
        audit_at="2026-10-01T15:00:00Z",
        request_text="Please erase my personal profile and data.",
    )

    decision = engine.decide_case(case)
    assert decision.deletion_status == DeletionStatus.COMPLETE
    assert len(decision.steps) >= 1
    assert all(s.phase == Phase.VERIFICATION for s in decision.steps)
    assert all(s.evidence_id == "ev_verified_db_02" for s in decision.steps)
    assert decision.is_valid
