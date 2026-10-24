"""Resurrection risk analyzer for synchronization connectors and backup restore paths.

Detects active data flows, replication drift, and restore risks that could resurrect
personal data after deletion, strictly conforming to AGENTS.md §6.4 and §6.5.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple
import pandas as pd

from .models import FlowState, ResurrectionRisk


class ResurrectionRiskAnalyzer:
    """Detects active sync connectors and backup restore paths that risk data resurrection."""

    def __init__(self, activity_df: Optional[pd.DataFrame] = None) -> None:
        self.activity_df = activity_df

    def analyze_flow_resource(
        self,
        system_id: str,
        resource_id: str,
        observed_state: str,
        content: str = "",
        supporting_evidence_id: str = "",
        timestamp: Optional[str] = None,
        source_system: Optional[str] = None,
        target_system: Optional[str] = None,
    ) -> ResurrectionRisk:
        """Evaluate a data_flow or backup resource from evidence or activity log."""
        res_lc = resource_id.lower()
        sys_lc = system_id.lower()
        content_lc = content.lower()

        # Determine flow type
        is_backup = (
            "backup" in res_lc
            or "backup" in sys_lc
            or "restore" in content_lc
            or "archive" in content_lc
        )

        flow_type = "RESTORE_PATH" if is_backup else "SYNC_FLOW"

        # Determine flow state
        state_str = observed_state.upper() if observed_state else "UNKNOWN"
        flow_state = FlowState.UNKNOWN

        if any(k in state_str for k in ["ACTIVE", "ENABLED", "FOUND", "RESTORABLE", "CONFIRMED"]):
            flow_state = FlowState.CONFIRMED_ACTIVE
        elif any(k in state_str for k in ["DISABLED", "BLOCKED", "STOPPED"]):
            flow_state = FlowState.CONFIRMED_DISABLED
        elif "active: true" in content_lc or "remains enabled" in content_lc or "can still be restored" in content_lc:
            flow_state = FlowState.CONFIRMED_ACTIVE
        elif "active: false" in content_lc or "status: disabled" in content_lc or "blocked" in content_lc:
            flow_state = FlowState.CONFIRMED_DISABLED
        elif state_str in ["ABSENT", "NOT_FOUND"]:
            flow_state = FlowState.CONFIRMED_DISABLED
        else:
            # Missing evidence is NOT proof that flow is inactive
            flow_state = FlowState.POTENTIAL_RISK

        # Hazard flag: active flows and unblocked restores are hazards
        is_hazard = flow_state in (FlowState.CONFIRMED_ACTIVE, FlowState.POTENTIAL_RISK, FlowState.UNKNOWN)

        if flow_type == "RESTORE_PATH":
            finding_op = "REPORT_RESTORE_RISK" if is_hazard else None
            remediation_op = "BLOCK_RESTORE" if is_hazard else None
            verification_op = "VERIFY_RESTORE_BLOCKED"
            desc = (
                f"Backup restore path '{resource_id}' in '{system_id}' is active or restorable. "
                f"A database or archive restore could reinstate deleted customer records."
            )
        else:
            finding_op = "REPORT_ACTIVE_DATA_FLOW" if is_hazard else None
            remediation_op = "DISABLE_DATA_FLOW" if is_hazard else None
            verification_op = "VERIFY_DATA_FLOW_DISABLED"
            desc = (
                f"Data flow '{resource_id}' in '{system_id}' ({source_system or 'source'} -> "
                f"{target_system or 'downstream'}) is actively replicating or synchronizing records."
            )

        note = None
        if flow_state == FlowState.UNKNOWN:
            note = "State uncertain: missing positive evidence that flow is disabled."
        elif flow_state == FlowState.POTENTIAL_RISK:
            note = "Potential risk: flow registered in system topology without explicit disable event."

        return ResurrectionRisk(
            flow_type=flow_type,
            system_id=system_id,
            resource_id=resource_id,
            source_system=source_system,
            target_system=target_system,
            flow_state=flow_state,
            is_hazard=is_hazard,
            finding_operation=finding_op,
            remediation_operation=remediation_op,
            verification_operation=verification_op,
            supporting_evidence_id=supporting_evidence_id,
            timestamp=timestamp,
            description=desc,
            uncertainty_note=note,
        )

    def analyze_activity_for_resurrection_paths(
        self,
        timeline_events: List[Any],
        audit_at: Optional[str] = None,
    ) -> List[ResurrectionRisk]:
        """Inspect timeline events for active flows, sync recreations, and restore attempts."""
        risks: List[ResurrectionRisk] = []
        seen_resources: Set[str] = set()

        for event in timeline_events:
            res_id = getattr(event, "resource_id", None) or ""
            if not res_id:
                continue

            res_lc = res_id.lower()
            is_flow = res_lc.startswith("data_flow") or "connector" in res_lc
            is_backup = res_lc.startswith("backup") or "archive" in res_lc

            if not (is_flow or is_backup):
                continue

            if res_id in seen_resources:
                continue
            seen_resources.add(res_id)

            sys_id = getattr(event, "system_id", "") or "integration_service"
            meta = getattr(event, "metadata", {}) or {}
            event_type = meta.get("event_type", "")
            details = meta.get("details", "") or getattr(event, "description", "")
            raw_ts = getattr(event, "raw_timestamp", None)
            ev_id = getattr(event, "evidence_id", "") or ""

            # Check if event was a disable or block
            state_str = "ACTIVE"
            if event_type in ("DISABLE_DATA_FLOW", "BLOCK_RESTORE") and getattr(event, "reported_result", "") == "SUCCESS":
                state_str = "DISABLED"
            elif "remains enabled" in details or "can still be restored" in details or "repopulated" in details:
                state_str = "ACTIVE"

            risk = self.analyze_flow_resource(
                system_id=sys_id,
                resource_id=res_id,
                observed_state=state_str,
                content=details,
                supporting_evidence_id=ev_id,
                timestamp=raw_ts,
                source_system=meta.get("source_system"),
                target_system=meta.get("target_system"),
            )
            risks.append(risk)

        return risks
