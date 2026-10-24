"""Unit and integration tests for Phase 6: Deletion Policy Engine & Resurrection-Risk Detection."""

import pytest
import pandas as pd

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


@pytest.fixture
def registry() -> PolicyRuleRegistry:
    """Fixture providing loaded policy rule registry."""
    return PolicyRuleRegistry.load_from_csv()


@pytest.fixture
def evaluator(registry: PolicyRuleRegistry) -> PolicyEvaluator:
    """Fixture providing policy evaluator."""
    return PolicyEvaluator(registry)


@pytest.fixture
def analyzer() -> ResurrectionRiskAnalyzer:
    """Fixture providing resurrection risk analyzer."""
    return ResurrectionRiskAnalyzer()


# ---------------------------------------------------------------------------
# 1. Rule Registry & Categorization Tests
# ---------------------------------------------------------------------------


def test_registry_loads_all_rules(registry: PolicyRuleRegistry):
    """Verify all 15 rules from deletion_rules.csv are loaded."""
    assert len(registry.rules) == 15
    user_rule = registry.get_rule_for_category(DataCategory.USER_PROFILE)
    assert user_rule is not None
    assert user_rule.required_action == RequiredAction.DELETE

    audit_rule = registry.get_rule_for_category(DataCategory.SECURITY_AUDIT_LOG)
    assert audit_rule is not None
    assert audit_rule.required_action == RequiredAction.RETAIN

    tombstone_rule = registry.get_rule_for_category(DataCategory.ACCOUNT_TOMBSTONE)
    assert tombstone_rule is not None
    assert tombstone_rule.required_action == RequiredAction.REDACT


def test_infer_categories(registry: PolicyRuleRegistry):
    """Verify system and resource ID patterns infer correct DataCategory."""
    assert registry.infer_category("account_service", "account_001") == DataCategory.USER_PROFILE
    assert registry.infer_category("search_service", "search_052") == DataCategory.SEARCH_INDEX_ENTRY
    assert registry.infer_category("analytics_platform", "analytics_2034") == DataCategory.ANALYTICS_PROFILE
    assert registry.infer_category("security_service", "audit_record_9394") == DataCategory.SECURITY_AUDIT_LOG
    assert registry.infer_category("backup_service", "backup_3870") == DataCategory.BACKUP_ARCHIVE
    assert registry.infer_category("identity_service", "tombstone_440") == DataCategory.ACCOUNT_TOMBSTONE
    assert registry.infer_category("integration_service", "data_flow_9522") == DataCategory.DATA_FLOW
    assert registry.infer_category("support_platform", "support_864", "Conversation text") == DataCategory.SUPPORT_MESSAGE
    assert registry.infer_category("support_platform", "support_4218", "Attachment binary") == DataCategory.SUPPORT_ATTACHMENT


# ---------------------------------------------------------------------------
# 2. Policy Evaluation: DELETE, REDACT, ANONYMIZE, RETAIN
# ---------------------------------------------------------------------------


def test_evaluate_delete_rule_remaining_record(evaluator: PolicyEvaluator):
    """Resource present when rule requires DELETE flags REPORT_REMAINING_RECORD."""
    finding = evaluator.evaluate_resource(
        system_id="account_service",
        resource_id="account_005",
        observed_state=ObservedState.PRESENT,
        content='{"email": "test@example.com"}',
        supporting_evidence_id="evidence_54",
    )
    assert finding.is_violation
    assert finding.finding_operation == "REPORT_REMAINING_RECORD"
    assert finding.remediation_operation == "DELETE_RECORD"
    assert finding.verification_operation == "VERIFY_RECORD_ABSENT"
    assert finding.supporting_evidence_id == "evidence_54"


def test_evaluate_delete_rule_compliant_absent(evaluator: PolicyEvaluator):
    """Resource absent when rule requires DELETE is compliant."""
    finding = evaluator.evaluate_resource(
        system_id="account_service",
        resource_id="account_001",
        observed_state=ObservedState.ABSENT,
        content="",
        supporting_evidence_id="evidence_54",
    )
    assert not finding.is_violation
    assert finding.finding_operation is None
    assert finding.remediation_operation is None
    assert finding.verification_operation == "VERIFY_RECORD_ABSENT"


def test_evaluate_redact_support_message_exposed_pii(evaluator: PolicyEvaluator):
    """Support message containing unredacted direct identifiers flags REPORT_EXPOSED_PII."""
    finding = evaluator.evaluate_resource(
        system_id="support_platform",
        resource_id="support_864",
        observed_state=ObservedState.PRESENT,
        content="Customer novak.tomasz@icloud.com contacted regarding order.",
        supporting_evidence_id="evidence_03",
        direct_identifiers={"novak.tomasz@icloud.com"},
    )
    assert finding.is_violation
    assert finding.finding_operation == "REPORT_EXPOSED_PII"
    assert finding.remediation_operation == "REDACT_RECORD"
    assert finding.verification_operation == "VERIFY_RECORD_REDACTED"
    assert "novak.tomasz@icloud.com" in finding.prohibited_identifiers


def test_evaluate_redact_support_message_compliant(evaluator: PolicyEvaluator):
    """Support message already redacted is compliant."""
    finding = evaluator.evaluate_resource(
        system_id="support_platform",
        resource_id="support_2667",
        observed_state=ObservedState.REDACTED,
        content="Customer [REDACTED] contacted regarding order.",
        supporting_evidence_id="evidence_03",
        direct_identifiers={"novak.tomasz@icloud.com"},
    )
    assert not finding.is_violation
    assert finding.finding_operation is None
    assert finding.verification_operation == "VERIFY_RECORD_REDACTED"


def test_evaluate_anonymize_analytics_profile(evaluator: PolicyEvaluator):
    """Analytics profile attributable to user flags REPORT_EXPOSED_PII and ANONYMIZE_RECORD."""
    finding = evaluator.evaluate_resource(
        system_id="analytics_platform",
        resource_id="analytics_2034",
        observed_state=ObservedState.PRESENT,
        content="Session for user novak.tomasz@icloud.com",
        supporting_evidence_id="evidence_03",
        direct_identifiers={"novak.tomasz@icloud.com"},
    )
    assert finding.is_violation
    assert finding.finding_operation == "REPORT_EXPOSED_PII"
    assert finding.remediation_operation == "ANONYMIZE_RECORD"
    assert finding.verification_operation == "VERIFY_RECORD_ANONYMIZED"


def test_evaluate_anonymize_analytics_compliant(evaluator: PolicyEvaluator):
    """Anonymized analytics record with no direct identifiers is compliant."""
    finding = evaluator.evaluate_resource(
        system_id="analytics_platform",
        resource_id="analytics_6830",
        observed_state=ObservedState.ANONYMIZED,
        content="Session events recorded for cohort A",
        supporting_evidence_id="evidence_03",
        direct_identifiers={"novak.tomasz@icloud.com"},
    )
    assert not finding.is_violation
    assert finding.finding_operation is None
    assert finding.verification_operation == "VERIFY_RECORD_ANONYMIZED"


# ---------------------------------------------------------------------------
# 3. Retention Exceptions & Account Tombstones
# ---------------------------------------------------------------------------


def test_evaluate_security_audit_log_retained_compliant(evaluator: PolicyEvaluator):
    """Required security audit log present in evidence is compliant and preserved."""
    finding = evaluator.evaluate_resource(
        system_id="security_service",
        resource_id="audit_record_9394",
        observed_state=ObservedState.PRESENT,
        content="2026-10-01T12:00:00Z | audit_record_9394 | user_32",
        supporting_evidence_id="evidence_03",
        user_id="user_32",
    )
    assert not finding.is_violation
    assert finding.finding_operation is None
    assert finding.verification_operation == "VERIFY_RECORD_PRESENT"


def test_evaluate_security_audit_log_missing_violation(evaluator: PolicyEvaluator):
    """If required audit record is absent, evaluator flags retention violation."""
    finding = evaluator.evaluate_resource(
        system_id="security_service",
        resource_id="audit_record_9394",
        observed_state=ObservedState.ABSENT,
        content="",
        supporting_evidence_id="evidence_03",
        user_id="user_32",
    )
    assert finding.is_violation
    assert finding.finding_operation == "REPORT_REMAINING_RECORD"
    assert finding.verification_operation == "VERIFY_RECORD_PRESENT"


def test_tombstone_allows_user_id_but_prohibits_direct_pii(evaluator: PolicyEvaluator):
    """Tombstone permits user_id, but flags exposed email/phone as violation."""
    # Tombstone with user_id AND exposed email
    finding_violation = evaluator.evaluate_resource(
        system_id="identity_service",
        resource_id="tombstone_9178",
        observed_state=ObservedState.PRESENT,
        content="user_id: user_32, email: novak.tomasz@icloud.com",
        supporting_evidence_id="evidence_51",
        user_id="user_32",
        direct_identifiers={"novak.tomasz@icloud.com"},
    )
    assert finding_violation.is_violation
    assert finding_violation.finding_operation == "REPORT_EXPOSED_PII"
    assert finding_violation.remediation_operation == "REDACT_RECORD"
    assert finding_violation.verification_operation == "VERIFY_RECORD_REDACTED"

    # Tombstone with user_id and PII redacted (compliant)
    finding_compliant = evaluator.evaluate_resource(
        system_id="identity_service",
        resource_id="tombstone_440",
        observed_state=ObservedState.REDACTED,
        content="user_id: user_32, status: tombstone, pii_redacted: true",
        supporting_evidence_id="evidence_51",
        user_id="user_32",
        direct_identifiers={"novak.tomasz@icloud.com"},
    )
    assert not finding_compliant.is_violation
    assert finding_compliant.verification_operation == "VERIFY_RECORD_REDACTED"


# ---------------------------------------------------------------------------
# 4. Resurrection Risk Detection
# ---------------------------------------------------------------------------


def test_resurrection_active_data_flow(analyzer: ResurrectionRiskAnalyzer):
    """Active data flow connector flags REPORT_ACTIVE_DATA_FLOW and DISABLE_DATA_FLOW."""
    risk = analyzer.analyze_flow_resource(
        system_id="integration_service",
        resource_id="data_flow_9522",
        observed_state="ACTIVE",
        content="Connector remains enabled",
        supporting_evidence_id="evidence_03",
        source_system="account_service",
        target_system="analytics_platform",
    )
    assert risk.is_hazard
    assert risk.flow_state == FlowState.CONFIRMED_ACTIVE
    assert risk.finding_operation == "REPORT_ACTIVE_DATA_FLOW"
    assert risk.remediation_operation == "DISABLE_DATA_FLOW"
    assert risk.verification_operation == "VERIFY_DATA_FLOW_DISABLED"


def test_resurrection_disabled_data_flow(analyzer: ResurrectionRiskAnalyzer):
    """Disabled data flow is compliant (no finding, verified disabled)."""
    risk = analyzer.analyze_flow_resource(
        system_id="integration_service",
        resource_id="data_flow_2901",
        observed_state="DISABLED",
        content="Process disabled",
        supporting_evidence_id="evidence_03",
    )
    assert not risk.is_hazard
    assert risk.flow_state == FlowState.CONFIRMED_DISABLED
    assert risk.finding_operation is None
    assert risk.verification_operation == "VERIFY_DATA_FLOW_DISABLED"


def test_resurrection_active_backup_restore_risk(analyzer: ResurrectionRiskAnalyzer):
    """Active backup archive flags REPORT_RESTORE_RISK and BLOCK_RESTORE."""
    risk = analyzer.analyze_flow_resource(
        system_id="backup_service",
        resource_id="backup_3870",
        observed_state="ACTIVE",
        content="Archive can still be restored",
        supporting_evidence_id="evidence_03",
    )
    assert risk.is_hazard
    assert risk.flow_state == FlowState.CONFIRMED_ACTIVE
    assert risk.finding_operation == "REPORT_RESTORE_RISK"
    assert risk.remediation_operation == "BLOCK_RESTORE"
    assert risk.verification_operation == "VERIFY_RESTORE_BLOCKED"


def test_resurrection_blocked_backup(analyzer: ResurrectionRiskAnalyzer):
    """Blocked backup restore access is compliant."""
    risk = analyzer.analyze_flow_resource(
        system_id="backup_service",
        resource_id="backup_706",
        observed_state="BLOCKED",
        content="Restore blocked",
        supporting_evidence_id="evidence_03",
    )
    assert not risk.is_hazard
    assert risk.flow_state == FlowState.CONFIRMED_DISABLED
    assert risk.finding_operation is None
    assert risk.verification_operation == "VERIFY_RESTORE_BLOCKED"


def test_missing_evidence_not_assumed_inactive(analyzer: ResurrectionRiskAnalyzer):
    """Missing evidence for registered data flow is treated as potential risk, not assumed safe."""
    risk = analyzer.analyze_flow_resource(
        system_id="integration_service",
        resource_id="data_flow_unknown",
        observed_state="UNKNOWN",
        content="",
        supporting_evidence_id="",
    )
    # Must flag potential risk, not assumed inactive
    assert risk.is_hazard
    assert risk.flow_state == FlowState.POTENTIAL_RISK
    assert risk.uncertainty_note is not None
