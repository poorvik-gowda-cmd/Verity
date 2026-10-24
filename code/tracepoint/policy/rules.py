"""Policy rule registry and categorization engine for Tracepoint.

Loads deletion_rules.csv and infers data categories and required operations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional
import pandas as pd

try:
    from config import config
except ImportError:
    from code.config import config

from .models import DataCategory, PolicyRule, RequiredAction


class PolicyRuleRegistry:
    """Registry of platform deletion rules and system categorization heuristics."""

    def __init__(self, rules: List[PolicyRule]) -> None:
        self.rules = rules
        self._rules_by_category: Dict[DataCategory, PolicyRule] = {
            r.data_category: r for r in rules
        }

    @classmethod
    def load_from_csv(cls, csv_path: Optional[Path | str] = None) -> "PolicyRuleRegistry":
        """Load rules from deletion_rules.csv."""
        path = Path(csv_path) if csv_path else config.deletion_rules_csv
        df = pd.read_csv(path)

        rules: List[PolicyRule] = []
        for _, row in df.iterrows():
            cat_str = str(row["data_category"]).strip()
            act_str = str(row["required_action"]).strip()

            try:
                cat = DataCategory(cat_str)
            except ValueError:
                cat = DataCategory.UNKNOWN

            try:
                act = RequiredAction(act_str)
            except ValueError:
                act = RequiredAction.DELETE

            rules.append(
                PolicyRule(
                    rule_id=str(row["rule_id"]).strip(),
                    data_category=cat,
                    condition=str(row["condition"]).strip(),
                    required_action=act,
                )
            )

        return cls(rules)

    def get_rule_for_category(self, category: DataCategory) -> Optional[PolicyRule]:
        """Look up the PolicyRule for a specific DataCategory."""
        return self._rules_by_category.get(category)

    def infer_category(
        self,
        system_id: Optional[str],
        resource_id: Optional[str],
        content: str = "",
    ) -> DataCategory:
        """Infer DataCategory from system ID, resource ID, and content clues."""
        sys_lc = (system_id or "").lower()
        res_lc = (resource_id or "").lower()
        content_lc = content.lower()

        # 1. Identity service / tombstones
        if "tombstone" in res_lc or "tombstone" in content_lc:
            return DataCategory.ACCOUNT_TOMBSTONE

        # 2. Security audit log
        if "security" in sys_lc or res_lc.startswith("audit_record") or "audit_log" in res_lc:
            return DataCategory.SECURITY_AUDIT_LOG

        # 3. Legal hold record
        if "legal" in sys_lc or res_lc.startswith("legal_"):
            return DataCategory.LEGAL_HOLD_RECORD

        # 4. Backup service
        if "backup" in sys_lc or res_lc.startswith("backup_"):
            return DataCategory.BACKUP_ARCHIVE

        # 5. Integration / Data flow
        if "integration" in sys_lc or res_lc.startswith("data_flow"):
            return DataCategory.DATA_FLOW

        # 6. Search service
        if "search" in sys_lc or res_lc.startswith("search_"):
            return DataCategory.SEARCH_INDEX_ENTRY

        # 7. Analytics platform
        if "analytics" in sys_lc or res_lc.startswith("analytics_"):
            if "telemetry" in res_lc or "telemetry" in content_lc:
                return DataCategory.PRODUCT_TELEMETRY
            return DataCategory.ANALYTICS_PROFILE

        # 8. User export
        if "export" in sys_lc or res_lc.startswith("export_"):
            return DataCategory.USER_EXPORT

        # 9. Marketing profile
        if "marketing" in sys_lc or res_lc.startswith("marketing_"):
            return DataCategory.MARKETING_PROFILE

        # 10. Cache entry
        if "cache" in sys_lc or res_lc.startswith("cache_"):
            return DataCategory.CACHE_ENTRY

        # 11. Email delivery log
        if "email" in sys_lc or res_lc.startswith("email_"):
            return DataCategory.EMAIL_DELIVERY_LOG

        # 12. Support platform
        if "support" in sys_lc or res_lc.startswith("support_"):
            if "attachment" in content_lc or "attachment" in res_lc:
                return DataCategory.SUPPORT_ATTACHMENT
            return DataCategory.SUPPORT_MESSAGE

        # 13. Account service / user profile (default primary store)
        if "account" in sys_lc or res_lc.startswith("account_") or res_lc.startswith("user_"):
            return DataCategory.USER_PROFILE

        return DataCategory.UNKNOWN
