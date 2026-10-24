"""Deterministic deletion-policy evaluator for Tracepoint.

Evaluates resource states against deletion rules, verifies retention requirements,
and flags exposed direct PII vs permitted internal reference identifiers.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from .models import (
    DataCategory,
    ObservedState,
    PolicyFinding,
    PolicyRule,
    RequiredAction,
)
from .rules import PolicyRuleRegistry


class PolicyEvaluator:
    """Evaluates resource records against the organizational deletion policy."""

    def __init__(self, registry: Optional[PolicyRuleRegistry] = None) -> None:
        self.registry = registry or PolicyRuleRegistry.load_from_csv()

    def evaluate_resource(
        self,
        system_id: str,
        resource_id: str,
        observed_state: ObservedState,
        content: str = "",
        supporting_evidence_id: str = "",
        user_id: Optional[str] = None,
        direct_identifiers: Optional[Set[str]] = None,
        explicit_category: Optional[DataCategory] = None,
    ) -> PolicyFinding:
        """Evaluate a single resource against applicable policy rules."""
        direct_ids = {d.strip().lower() for d in (direct_identifiers or set()) if d.strip()}

        category = explicit_category or self.registry.infer_category(
            system_id=system_id,
            resource_id=resource_id,
            content=content,
        )

        rule = self.registry.get_rule_for_category(category)
        if not rule:
            # Fallback default rule (rule_01 DELETE)
            rule = PolicyRule(
                rule_id="rule_01",
                data_category=category,
                condition="Default",
                required_action=RequiredAction.DELETE,
            )

        content_lc = content.lower()

        # Identify any exposed prohibited direct identifiers (email, phone, name, email hash)
        prohibited_found: List[str] = []
        for ident in direct_ids:
            if re.search(r"(?<![A-Za-z0-9_])" + re.escape(ident) + r"(?![A-Za-z0-9_])", content_lc):
                prohibited_found.append(ident)

        is_violation = False
        finding_op: Optional[str] = None
        remediation_op: Optional[str] = None
        verification_op: str = "VERIFY_RECORD_ABSENT"
        details: str = ""

        # ---------------------------------------------------------------------
        # Evaluation by Required Action
        # ---------------------------------------------------------------------
        if rule.required_action == RequiredAction.DELETE:
            verification_op = "VERIFY_RECORD_ABSENT"
            if observed_state in (ObservedState.PRESENT, ObservedState.ACTIVE):
                is_violation = True
                finding_op = "REPORT_REMAINING_RECORD"
                remediation_op = "DELETE_RECORD"
                details = f"Record '{resource_id}' in '{system_id}' still exists but policy requires full DELETE."
            else:
                details = f"Record '{resource_id}' is compliant (ABSENT)."

        elif rule.required_action == RequiredAction.REDACT:
            verification_op = "VERIFY_RECORD_REDACTED"
            # In ACCOUNT_TOMBSTONE and REDACT rules, stable user_id is permitted, but direct identifiers are prohibited.
            has_exposed_pii = bool(prohibited_found) or observed_state == ObservedState.PRESENT

            if category in (DataCategory.ACCOUNT_TOMBSTONE, DataCategory.SUPPORT_MESSAGE):
                if not prohibited_found and (
                    observed_state in (ObservedState.REDACTED, ObservedState.ABSENT)
                    or "pii_redacted" in content_lc
                    or "redacted" in content_lc
                    or '"email": null' in content_lc
                    or "requester_email=null" in content_lc
                ):
                    has_exposed_pii = False
                    observed_state = ObservedState.REDACTED

            if has_exposed_pii or observed_state != ObservedState.REDACTED:
                is_violation = True
                finding_op = "REPORT_EXPOSED_PII"
                remediation_op = "REDACT_RECORD"
                details = (
                    f"Record '{resource_id}' in '{system_id}' contains exposed direct identifiers: "
                    f"{prohibited_found or 'unredacted PII'}; policy requires REDACTION."
                )
            else:
                details = f"Record '{resource_id}' is compliant (REDACTED tombstone/message)."

        elif rule.required_action == RequiredAction.ANONYMIZE:
            verification_op = "VERIFY_RECORD_ANONYMIZED"
            # Anonymized records must no longer be attributable to the user
            is_attributable = bool(prohibited_found) or observed_state == ObservedState.PRESENT
            if user_id:
                uid_lc = user_id.lower()
                if re.search(r"(?<![A-Za-z0-9_])" + re.escape(uid_lc) + r"(?![A-Za-z0-9_])", content_lc):
                    is_attributable = True

            if not prohibited_found and (
                observed_state == ObservedState.ANONYMIZED
                or "customer_reference=null" in content_lc
                or '"customer_reference": null' in content_lc
            ):
                is_attributable = False
                observed_state = ObservedState.ANONYMIZED

            if is_attributable or observed_state != ObservedState.ANONYMIZED:
                is_violation = True
                finding_op = "REPORT_EXPOSED_PII"
                remediation_op = "ANONYMIZE_RECORD"
                details = f"Record '{resource_id}' in '{system_id}' is still attributable to user; policy requires ANONYMIZATION."
            else:
                details = f"Record '{resource_id}' is compliant (ANONYMIZED)."

        elif rule.required_action == RequiredAction.RETAIN:
            verification_op = "VERIFY_RECORD_PRESENT"
            # Must remain present
            if observed_state in (ObservedState.ABSENT, ObservedState.UNKNOWN):
                is_violation = True
                finding_op = "REPORT_REMAINING_RECORD"
                remediation_op = None
                details = f"Required retained record '{resource_id}' in '{system_id}' is missing; policy requires RETENTION."
            else:
                details = f"Retained record '{resource_id}' in '{system_id}' is compliant (PRESENT)."

        return PolicyFinding(
            rule_id=rule.rule_id,
            data_category=category,
            system_id=system_id,
            resource_id=resource_id,
            required_action=rule.required_action,
            observed_state=observed_state,
            is_violation=is_violation,
            finding_operation=finding_op,
            remediation_operation=remediation_op,
            verification_operation=verification_op,
            supporting_evidence_id=supporting_evidence_id,
            prohibited_identifiers=prohibited_found,
            details=details,
        )
