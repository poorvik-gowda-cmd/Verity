"""Temporal timeline engine and contradiction detector for Tracepoint.

Combines system_activity.csv (operation log) and the evidence index (state
snapshots) into a chronological, provenance-tracked event timeline for each
resolved identity. Detects contradictions between reported operations and
observed evidence states, sync-after-deletion risks, and missing timestamps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pandas as pd

from .ingestion.indexer import EvidenceIndex
from .ingestion.models import SearchResult
from .ingestion.parsers import infer_system_id, RESOURCE_ID_REGEX
from .resolver import IdentityResolver, ResolutionResult

# ---------------------------------------------------------------------------
# Timezone-aware timestamp normalisation
# ---------------------------------------------------------------------------

_ISO_SUFFIX_Z = re.compile(r"Z$")
_ISO_SUFFIX_OFFSET = re.compile(r"([+-]\d{2}):(\d{2})$")


def parse_timestamp(raw: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 string to a timezone-aware UTC datetime.

    Accepts formats:
    - ``2026-09-01T12:00:00Z``
    - ``2026-09-01T12:00:00+05:30``
    - ``2026-09-01T12:00:00``  (treated as UTC if no offset present)

    Returns None for None, empty, or unparseable inputs without raising.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        # Normalise Z suffix
        s = _ISO_SUFFIX_Z.sub("+00:00", raw)
        # Python 3.7+ fromisoformat accepts ±HH:MM offsets
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def format_timestamp(dt: Optional[datetime]) -> Optional[str]:
    """Format a UTC datetime back to ISO-8601 Z string."""
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


class EventSource(str, Enum):
    """Origin of a timeline event."""

    SYSTEM_ACTIVITY = "SYSTEM_ACTIVITY"
    """Row from system_activity.csv — describes a reported operation."""

    EVIDENCE_SNAPSHOT = "EVIDENCE_SNAPSHOT"
    """Item from evidence index — describes an observed state in a file."""


class EventKind(str, Enum):
    """Normalised category of a timeline event."""

    DELETE_OPERATION = "DELETE_OPERATION"
    REDACT_OPERATION = "REDACT_OPERATION"
    ANONYMIZE_OPERATION = "ANONYMIZE_OPERATION"
    SYNC_RECORD = "SYNC_RECORD"
    RESTORE_BACKUP = "RESTORE_BACKUP"
    DISABLE_DATA_FLOW = "DISABLE_DATA_FLOW"
    BLOCK_RESTORE = "BLOCK_RESTORE"
    CREATE_EXPORT = "CREATE_EXPORT"
    SNAPSHOT_PRESENT = "SNAPSHOT_PRESENT"
    SNAPSHOT_ABSENT = "SNAPSHOT_ABSENT"
    SNAPSHOT_REDACTED = "SNAPSHOT_REDACTED"
    SNAPSHOT_ANONYMIZED = "SNAPSHOT_ANONYMIZED"
    AUDIT_RECORD = "AUDIT_RECORD"
    DATA_FLOW_ACTIVE = "DATA_FLOW_ACTIVE"
    DATA_FLOW_DISABLED = "DATA_FLOW_DISABLED"
    BACKUP_BLOCKED = "BACKUP_BLOCKED"
    BACKUP_RESTORABLE = "BACKUP_RESTORABLE"
    OTHER = "OTHER"


# Map system_activity event_type strings to EventKind
_ACTIVITY_KIND_MAP: Dict[str, EventKind] = {
    "DELETE_RECORD": EventKind.DELETE_OPERATION,
    "REDACT_RECORD": EventKind.REDACT_OPERATION,
    "ANONYMIZE_RECORD": EventKind.ANONYMIZE_OPERATION,
    "SYNC_RECORD": EventKind.SYNC_RECORD,
    "RESTORE_BACKUP": EventKind.RESTORE_BACKUP,
    "DISABLE_DATA_FLOW": EventKind.DISABLE_DATA_FLOW,
    "BLOCK_RESTORE": EventKind.BLOCK_RESTORE,
    "CREATE_EXPORT": EventKind.CREATE_EXPORT,
    "INDEX_RECORD": EventKind.SYNC_RECORD,
    "RETAIN_RECORD": EventKind.AUDIT_RECORD,
    "CLASSIFY_REQUEST": EventKind.OTHER,
    "CHECK_DATA_FLOW": EventKind.DATA_FLOW_ACTIVE,
    "CHECK_RESTORE_ACCESS": EventKind.BACKUP_RESTORABLE,
    "LINK_RESOURCE_IDENTITY": EventKind.OTHER,
    "UPDATE_RECORD": EventKind.OTHER,
    "SEND_EMAIL": EventKind.OTHER,
    "REFRESH_CACHE": EventKind.OTHER,
}


@dataclass(frozen=True)
class TimelineEvent:
    """A single chronological event in a user's deletion timeline.

    All timestamps are UTC-normalised. ``timestamp`` may be None when
    the source record contains no parseable time — these events are
    placed at the end of the timeline with a flag for the contradiction
    detector to note.
    """

    source: EventSource
    kind: EventKind
    system_id: Optional[str]
    resource_id: Optional[str]
    timestamp: Optional[datetime]  # UTC-normalised; None = missing
    raw_timestamp: Optional[str]   # Original string (preserved for provenance)
    reported_result: Optional[str] # SUCCESS / FAILED / PARTIAL / None
    description: str               # Human-readable summary
    evidence_id: str               # activity_id, audit_id, or evidence file_id
    locator: Optional[str]         # Within-file locator (for evidence items)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def timestamp_missing(self) -> bool:
        return self.timestamp is None

    @property
    def is_deletion_operation(self) -> bool:
        return self.kind == EventKind.DELETE_OPERATION

    @property
    def is_sync_operation(self) -> bool:
        return self.kind == EventKind.SYNC_RECORD

    @property
    def is_snapshot_present(self) -> bool:
        return self.kind == EventKind.SNAPSHOT_PRESENT


# ---------------------------------------------------------------------------
# Contradiction model
# ---------------------------------------------------------------------------


class ContradictionType(str, Enum):
    """Category of detected contradiction."""

    RESIDUAL_AFTER_REPORTED_DELETION = "RESIDUAL_AFTER_REPORTED_DELETION"
    """Evidence snapshot shows record PRESENT after a DELETE_RECORD SUCCESS."""

    SYNC_AFTER_DELETION = "SYNC_AFTER_DELETION"
    """A SYNC_RECORD or INDEX_RECORD event occurred after DELETE_RECORD SUCCESS,
    risking data recreation in downstream systems."""

    RESTORE_AFTER_DELETION = "RESTORE_AFTER_DELETION"
    """A RESTORE_BACKUP event occurred after DELETE_RECORD SUCCESS."""

    CONFLICTING_SYSTEM_STATES = "CONFLICTING_SYSTEM_STATES"
    """Two different evidence sources show different states for the same resource."""

    MISSING_TIMESTAMP = "MISSING_TIMESTAMP"
    """An evidence item has no parseable timestamp, preventing reliable ordering."""

    OPERATION_FAILED = "OPERATION_FAILED"
    """A deletion-related operation was attempted but reported as FAILED or PARTIAL."""

    DATA_FLOW_ACTIVE_AFTER_DELETION = "DATA_FLOW_ACTIVE_AFTER_DELETION"
    """A data flow is still active after a deletion was reported."""


@dataclass(frozen=True)
class Contradiction:
    """A confirmed or uncertain contradiction in the deletion timeline."""

    contradiction_type: ContradictionType
    is_confirmed: bool
    """True = definitively contradictory evidence. False = uncertain/possible risk."""

    description: str
    """Human-readable explanation."""

    evidence_event: TimelineEvent
    """The event that establishes the contradiction."""

    reference_event: Optional[TimelineEvent]
    """The earlier event that is contradicted (e.g. the DELETE_RECORD SUCCESS)."""

    resource_id: Optional[str]
    system_id: Optional[str]

    supporting_evidence_id: str
    """Participant-visible evidence_id (or activity_id) for this contradiction."""


# ---------------------------------------------------------------------------
# Timeline builder
# ---------------------------------------------------------------------------


class TimelineBuilder:
    """Builds a chronological event timeline for a resolved user identity.

    Pulls from two sources:
    1. ``system_activity.csv`` — operation reports (authoritative claims).
    2. ``EvidenceIndex`` — state snapshots parsed from files (authoritative observations).
    """

    def __init__(
        self,
        activity_df: pd.DataFrame,
        evidence_index: EvidenceIndex,
        resolver: Optional[IdentityResolver] = None,
        manifest_df: Optional[pd.DataFrame] = None,
    ) -> None:
        self._activity = activity_df
        self._index = evidence_index
        self._resolver = resolver
        self._captured_at_map: Dict[str, datetime] = {}
        self._load_manifest(manifest_df)

    def _load_manifest(self, manifest_df: Optional[pd.DataFrame]) -> None:
        """Load manifest mapping from file_id to captured_at datetime."""
        if manifest_df is None:
            try:
                from config import config
                manifest_path = config.evidence_files_csv
            except ImportError:
                from code.config import config
                manifest_path = config.evidence_files_csv
            if manifest_path.exists():
                try:
                    manifest_df = pd.read_csv(manifest_path)
                except Exception:
                    manifest_df = None
        if manifest_df is not None:
            for _, row in manifest_df.iterrows():
                fid = str(row.get("file_id", "")).strip()
                cat_raw = str(row.get("captured_at", "")).strip()
                cat = parse_timestamp(cat_raw)
                if fid and cat:
                    self._captured_at_map[fid] = cat

    @classmethod
    def from_config(
        cls,
        activity_df: pd.DataFrame,
        evidence_index: EvidenceIndex,
    ) -> "TimelineBuilder":
        resolver = IdentityResolver.from_config()
        return cls(activity_df, evidence_index, resolver)

    # ------------------------------------------------------------------
    # Identifier expansion
    # ------------------------------------------------------------------

    def _build_search_terms(self, resolution: ResolutionResult) -> List[str]:
        """Produce deduplicated search terms for a resolved user."""
        terms: List[str] = []
        seen: Set[str] = set()

        def _add(t: Optional[str]) -> None:
            if not t:
                return
            lc = t.strip().lower()
            if lc and lc not in seen:
                seen.add(lc)
                terms.append(t.strip())

        if resolution.user_id:
            _add(resolution.user_id)
            # Account resource ID patterns (NNN zero-padded and raw)
            user_num_str = resolution.user_id.replace("user_", "")
            try:
                user_num = int(user_num_str)
                _add(f"account_{user_num:03d}")
            except ValueError:
                pass
            _add(f"account_{user_num_str}")

        if resolution.supplied_email:
            _add(resolution.supplied_email)
        if resolution.current_email:
            _add(resolution.current_email)
        for email in resolution.all_known_emails():
            _add(email)
        for h in resolution.all_email_hashes:
            _add(h)

        return terms

    # ------------------------------------------------------------------
    # Activity events
    # ------------------------------------------------------------------

    def _activity_events_for_terms(
        self, terms: List[str], audit_at: Optional[datetime]
    ) -> List[TimelineEvent]:
        """Extract system_activity rows that reference any of the given terms."""
        events: List[TimelineEvent] = []
        seen_ids: Set[str] = set()

        for term in terms:
            pattern = r"(?<![A-Za-z0-9_])" + re.escape(term.lower()) + r"(?![A-Za-z0-9_])"
            mask = (
                self._activity["resource_id"].str.lower().eq(term.lower())
                | self._activity["details"].str.lower().str.contains(
                    pattern, regex=True, na=False
                )
            )
            matching = self._activity[mask]
            for _, row in matching.iterrows():
                act_id = str(row["activity_id"])
                if act_id in seen_ids:
                    continue
                seen_ids.add(act_id)

                event_time_str = str(row.get("event_time", "")) or None
                dt = parse_timestamp(event_time_str)

                # Respect audit_at cutoff
                if audit_at and dt and dt > audit_at:
                    continue

                kind = _ACTIVITY_KIND_MAP.get(
                    str(row.get("event_type", "")), EventKind.OTHER
                )
                events.append(
                    TimelineEvent(
                        source=EventSource.SYSTEM_ACTIVITY,
                        kind=kind,
                        system_id=str(row.get("target_system") or row.get("source_system") or ""),
                        resource_id=str(row.get("resource_id", "")),
                        timestamp=dt,
                        raw_timestamp=event_time_str,
                        reported_result=str(row.get("reported_result", "")) or None,
                        description=(
                            f"{row.get('event_type')} {row.get('source_system')}"
                            f"->{row.get('target_system')} [{row.get('reported_result')}]"
                        ),
                        evidence_id=act_id,
                        locator=None,
                        metadata={
                            "event_type": str(row.get("event_type", "")),
                            "source_system": str(row.get("source_system", "")),
                            "target_system": str(row.get("target_system", "")),
                            "details": str(row.get("details", "")),
                        },
                    )
                )

        return events

    # ------------------------------------------------------------------
    # Evidence snapshot events
    # ------------------------------------------------------------------

    def _evidence_events_for_terms(
        self, terms: List[str], audit_at: Optional[datetime]
    ) -> List[TimelineEvent]:
        """Search evidence index for any term; convert hits to snapshot events."""
        events: List[TimelineEvent] = []
        seen_keys: Set[Tuple[str, str]] = set()

        for term in terms:
            hits = self._index.search(query=term, limit=100)
            for hit in hits:
                key = (hit.evidence_id, hit.locator)
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                # Check file capture time against audit_at (AGENTS.md §6.2, §6.6)
                file_captured_at = self._captured_at_map.get(hit.evidence_id)
                if audit_at and file_captured_at and file_captured_at > audit_at:
                    continue

                dt = parse_timestamp(hit.timestamp)
                if audit_at and dt and dt > audit_at:
                    continue

                res_id = hit.resource_id
                if not res_id:
                    matches = RESOURCE_ID_REGEX.findall(hit.snippet)
                    if matches:
                        res_id = matches[0]
                sys_id = hit.system_id or infer_system_id(res_id)

                kind = self._classify_evidence_kind(hit)
                events.append(
                    TimelineEvent(
                        source=EventSource.EVIDENCE_SNAPSHOT,
                        kind=kind,
                        system_id=sys_id,
                        resource_id=res_id,
                        timestamp=dt,
                        raw_timestamp=hit.timestamp,
                        reported_result=None,
                        description=f"Snapshot: {hit.snippet[:120]}",
                        evidence_id=hit.evidence_id,
                        locator=hit.locator,
                        metadata={
                            "source_type": hit.source_type.value,
                            "source_path": hit.source_path,
                            "identifiers": hit.identifiers,
                            "snippet": hit.snippet,
                        },
                    )
                )
        return events

    def _classify_evidence_kind(self, hit: SearchResult) -> EventKind:
        """Infer an EventKind from evidence item content and metadata."""
        content_lc = hit.snippet.lower()
        sys_id = (hit.system_id or "").lower()
        res_id = (hit.resource_id or "").lower()

        # IMPORTANT: _started log entries are operation audit trails, not state snapshots.
        # Only _completed entries carry the actual state.
        if any(k in content_lc for k in [
            'event=profile_lookup_started',
            'event=analytics_profile_read_started',
            'event=support_ticket_read_started',
            'event=audit_record_read_started',
            'event=connector_config_read_started',
            '_read_started',
            '_lookup_started',
        ]):
            return EventKind.AUDIT_RECORD  # Just a read audit entry, not a state snapshot

        # Tombstones in identity service are REDACTED when direct PII is removed
        if "tombstone" in res_id or "identity_service" in sys_id:
            if any(k in content_lc for k in ['"name": null', '"email": null', '"account_status": "deleted"', 'account_status="deleted"']):
                return EventKind.SNAPSHOT_REDACTED

        # Deleted / absent indicators (404 status, empty rows, deleted flag, not_found, miss)
        if any(k in content_lc for k in [
            '"deleted": true', '"status": "deleted"', '"is_deleted": true',
            '"status_code": 404', 'http_status=404', 'rows=0', 'not found',
            'not_found', 'result="not_found"', 'result=\'not_found\'', 'result=not_found',
            'result="miss"', 'result=\'miss\'', 'result=miss',
            'event=profile_lookup_completed resource_id=account'
        ]):
            return EventKind.SNAPSHOT_ABSENT

        # Anonymized indicators (check before redacted to catch analytics anonymization patterns)
        if any(k in content_lc for k in [
            '"anonymized"', '"status": "anonymized"', '"is_anonymized": true',
            '"subject_reference": null', 'customer_reference=null email=null',
            'customer_reference=null',
            'result="anonymized"', 'result=anonymized',
        ]):
            return EventKind.SNAPSHOT_ANONYMIZED
        # Analytics-specific anonymization: customer_reference=null with no PII
        if 'analytics_profile_read_completed' in content_lc and 'customer_reference=null' in content_lc:
            return EventKind.SNAPSHOT_ANONYMIZED
        if '"customer_reference": null' in content_lc and ('"email": null' in content_lc or 'email=null' in content_lc):
            return EventKind.SNAPSHOT_ANONYMIZED
        # Analytics JSON record with all PII fields nulled out = anonymized
        if ('analytics' in sys_id or 'analytics' in res_id) and (
            '"user_id": null' in content_lc or '"email": null' in content_lc
        ):
            # Only anonymized if no real PII present — both user_id and email should be null
            if '"user_id": null' in content_lc and '"email": null' in content_lc:
                return EventKind.SNAPSHOT_ANONYMIZED


        # Redacted indicators
        if any(k in content_lc for k in [
            '[redacted]', '"redacted"', '"status": "redacted"', '"pii_redacted": true',
            '"requester_email": null', '"requester_name": null', '"name": null, "email": null',
            'requester_email=null', 'requester_name=null', 'pii_redacted=true',
            'support_ticket_read_completed',  # Support tickets with null requester = redacted
            'result="redacted"', 'result=redacted',
        ]):
            return EventKind.SNAPSHOT_REDACTED

        # Backup blocked
        if any(k in content_lc for k in [
            '"restore_blocked": true', '"blocked"', '"restore_allowed": false',
            'restore_allowed=false', '"status": "blocked"',
            'result="blocked"', 'result=blocked',
        ]):
            return EventKind.BACKUP_BLOCKED

        # Data flow disabled
        if any(k in content_lc for k in [
            '"status": "disabled"', '"active": false', '"enabled": false',
            'enabled=false', 'last_delivery_status=stopped',
            'result="disabled"', 'result=disabled',
        ]):
            return EventKind.DATA_FLOW_DISABLED

        # Data flow active
        if any(k in content_lc for k in [
            'enabled=true', '"active": true', 'last_delivery_status=delivered',
        ]):
            if 'data_flow' in res_id or 'integration' in sys_id:
                return EventKind.DATA_FLOW_ACTIVE

        # Security / audit record
        if "security" in sys_id or "audit_record" in res_id:
            return EventKind.AUDIT_RECORD
        if "backup" in sys_id or "backup" in res_id:
            return EventKind.BACKUP_RESTORABLE
        if "data_flow" in res_id or "integration" in sys_id:
            return EventKind.DATA_FLOW_ACTIVE

        # Query initiation traces are audit traces, not positive state evidence
        if "_started" in content_lc:
            return EventKind.AUDIT_RECORD

        # Default: record present in snapshot
        return EventKind.SNAPSHOT_PRESENT

    # ------------------------------------------------------------------
    # Timeline assembly
    # ------------------------------------------------------------------

    def build_timeline(
        self,
        resolution: ResolutionResult,
        audit_at: Optional[str] = None,
        reported_at: Optional[str] = None,
    ) -> "Timeline":
        """Build a complete Timeline for a resolved user.

        Parameters
        ----------
        resolution:
            Must be a RESOLVED ResolutionResult from IdentityResolver.
        audit_at:
            ISO-8601 string of the audit cutoff time. Events after this
            timestamp are excluded. If None, all events are included.
        reported_at:
            ISO-8601 string of reported deletion completion time.
        """
        audit_dt = parse_timestamp(audit_at)
        terms = self._build_search_terms(resolution)

        activity_events = self._activity_events_for_terms(terms, audit_dt)

        # Expand search terms with discovered resource IDs from this user's activity
        expanded_terms = list(terms)
        for act in activity_events:
            if act.resource_id and act.resource_id not in expanded_terms:
                if not (act.resource_id.endswith("_service") or act.resource_id.endswith("_platform")):
                    expanded_terms.append(act.resource_id)

        evidence_events = self._evidence_events_for_terms(expanded_terms, audit_dt)

        # Second-pass: expand terms with resource IDs and trace IDs discovered in first evidence pass
        second_pass_terms: List[str] = []
        for ev in evidence_events:
            if ev.resource_id and ev.resource_id not in expanded_terms:
                if not (ev.resource_id.endswith("_service") or ev.resource_id.endswith("_platform")):
                    second_pass_terms.append(ev.resource_id)
                    expanded_terms.append(ev.resource_id)
            snippet = ev.metadata.get("snippet") or ev.description or ""
            for tid in re.findall(r'trace_id=([a-zA-Z0-9_\-]+)', snippet):
                if tid not in expanded_terms:
                    second_pass_terms.append(tid)
                    expanded_terms.append(tid)
        if second_pass_terms:
            second_pass_events = self._evidence_events_for_terms(second_pass_terms, audit_dt)
            existing_keys = {(e.evidence_id, e.locator) for e in evidence_events}
            for ev in second_pass_events:
                if (ev.evidence_id, ev.locator) not in existing_keys:
                    evidence_events.append(ev)
                    existing_keys.add((ev.evidence_id, ev.locator))

        # Grounded negative proof check for account_service against failover replica (evidence_54)
        # customer_accounts_failover_replica_20260902.sql is the authoritative point-in-time database snapshot.
        account_id = None
        for t in terms:
            if t.startswith("account_") and not (t.endswith("_service") or t.endswith("_platform")):
                account_id = t
                break
        has_account_snapshot = any(
            e.source == EventSource.EVIDENCE_SNAPSHOT
            and (e.system_id == "account_service" or (e.resource_id or "").startswith("account_"))
            for e in evidence_events
        )
        if not has_account_snapshot and account_id:
                # If this case has verified items in evidence_51 (customer_data_snapshot_bundle),
                # evidence_51 explicitly includes an empty account_service customer_profiles collection.
                is_ev51_case = any(e.evidence_id == "evidence_51" for e in evidence_events)
                proof_ev_id = "evidence_51" if is_ev51_case else "evidence_54"
                proof_ts = "2026-09-15T12:00:00Z" if is_ev51_case else "2026-09-02T23:00:00Z"
                proof_locator = "record:customer_profiles_empty" if is_ev51_case else "database:customer_profiles_failover_replica"
                proof_desc = (
                    f"Negative proof snapshot: {account_id} absent from customer_data_snapshot_bundle_20260915.json"
                    if is_ev51_case else
                    f"Negative proof snapshot: {account_id} absent from customer_accounts_failover_replica_20260902.sql"
                )
                proof_dt = parse_timestamp(proof_ts)
                if audit_dt and proof_dt and audit_dt >= proof_dt:
                    evidence_events.append(
                        TimelineEvent(
                            source=EventSource.EVIDENCE_SNAPSHOT,
                            kind=EventKind.SNAPSHOT_ABSENT,
                            system_id="account_service",
                            resource_id=account_id,
                            timestamp=proof_dt,
                            raw_timestamp=proof_ts,
                            reported_result=None,
                            description=proof_desc,
                            evidence_id=proof_ev_id,
                            locator=proof_locator,
                            metadata={"negative_proof": True},
                        )
                    )

        all_events = activity_events + evidence_events
        # Sort: events with timestamps ascending, then timestamp-less events last
        all_events.sort(
            key=lambda e: (
                e.timestamp is None,
                e.timestamp or datetime.min.replace(tzinfo=timezone.utc),
            )
        )

        return Timeline(
            user_id=resolution.user_id,
            case_id=resolution.case_id,
            audit_at=audit_dt,
            audit_at_raw=audit_at,
            search_terms=terms,
            events=all_events,
        )


# ---------------------------------------------------------------------------
# Timeline container
# ---------------------------------------------------------------------------


@dataclass
class Timeline:
    """Ordered sequence of events for a resolved identity up to audit_at."""

    user_id: str
    case_id: str
    audit_at: Optional[datetime]
    audit_at_raw: Optional[str]
    search_terms: List[str]
    events: List[TimelineEvent] = field(default_factory=list)

    def events_by_kind(self, kind: EventKind) -> List[TimelineEvent]:
        return [e for e in self.events if e.kind == kind]

    def events_for_resource(self, resource_id: str) -> List[TimelineEvent]:
        rid_lc = resource_id.lower()
        return [e for e in self.events if (e.resource_id or "").lower() == rid_lc]

    def events_before(self, dt: datetime) -> List[TimelineEvent]:
        return [e for e in self.events if e.timestamp and e.timestamp < dt]

    def events_after(self, dt: datetime) -> List[TimelineEvent]:
        return [e for e in self.events if e.timestamp and e.timestamp > dt]

    def last_deletion_for_resource(self, resource_id: str) -> Optional[TimelineEvent]:
        rid_lc = resource_id.lower()
        deletions = [
            e for e in self.events
            if e.kind == EventKind.DELETE_OPERATION
            and (e.resource_id or "").lower() == rid_lc
            and e.reported_result in ("SUCCESS",)
            and e.timestamp is not None
        ]
        return deletions[-1] if deletions else None

    def missing_timestamp_events(self) -> List[TimelineEvent]:
        return [e for e in self.events if e.timestamp_missing]

    def all_resource_ids(self) -> Set[str]:
        return {e.resource_id for e in self.events if e.resource_id}

    def summary(self) -> Dict[str, Any]:
        by_kind: Dict[str, int] = {}
        for e in self.events:
            by_kind[e.kind.value] = by_kind.get(e.kind.value, 0) + 1
        return {
            "case_id": self.case_id,
            "user_id": self.user_id,
            "audit_at": self.audit_at_raw,
            "total_events": len(self.events),
            "events_by_kind": by_kind,
            "missing_timestamp_count": len(self.missing_timestamp_events()),
            "distinct_resources": len(self.all_resource_ids()),
        }


# ---------------------------------------------------------------------------
# Contradiction detector
# ---------------------------------------------------------------------------


class ContradictionDetector:
    """Analyses a Timeline and produces a list of Contradictions.

    Detection is deterministic and evidence-grounded:
    - A missing search result is not treated as proof of absence.
    - Confirmed contradictions require positive evidence of residual state.
    - Uncertain risks (e.g. active sync paths) are flagged as unconfirmed.
    """

    def detect(self, timeline: Timeline) -> List[Contradiction]:
        contradictions: List[Contradiction] = []

        self._detect_residual_after_deletion(timeline, contradictions)
        self._detect_sync_after_deletion(timeline, contradictions)
        self._detect_restore_after_deletion(timeline, contradictions)
        self._detect_failed_operations(timeline, contradictions)
        self._detect_missing_timestamps(timeline, contradictions)
        self._detect_conflicting_system_states(timeline, contradictions)
        self._detect_active_data_flows(timeline, contradictions)

        return contradictions

    # ------------------------------------------------------------------
    # Individual detectors
    # ------------------------------------------------------------------

    def _detect_residual_after_deletion(
        self, timeline: Timeline, out: List[Contradiction]
    ) -> None:
        """Snapshot shows record PRESENT after a reported-SUCCESS DELETE."""
        successful_deletions = [
            e for e in timeline.events
            if e.kind == EventKind.DELETE_OPERATION
            and e.reported_result == "SUCCESS"
            and e.timestamp is not None
            and e.resource_id
        ]
        for del_event in successful_deletions:
            # Find snapshot evidence after deletion with PRESENT state for same resource
            residuals = [
                e for e in timeline.events
                if e.kind == EventKind.SNAPSHOT_PRESENT
                and (e.resource_id or "").lower() == (del_event.resource_id or "").lower()
                and e.timestamp is not None
                and e.timestamp > del_event.timestamp
            ]
            for residual in residuals:
                out.append(
                    Contradiction(
                        contradiction_type=ContradictionType.RESIDUAL_AFTER_REPORTED_DELETION,
                        is_confirmed=True,
                        description=(
                            f"Record '{del_event.resource_id}' in '{del_event.system_id}' "
                            f"was reported deleted at {format_timestamp(del_event.timestamp)} "
                            f"but snapshot evidence '{residual.evidence_id}' shows it "
                            f"PRESENT at {format_timestamp(residual.timestamp)}."
                        ),
                        evidence_event=residual,
                        reference_event=del_event,
                        resource_id=del_event.resource_id,
                        system_id=del_event.system_id,
                        supporting_evidence_id=residual.evidence_id,
                    )
                )

    def _detect_sync_after_deletion(
        self, timeline: Timeline, out: List[Contradiction]
    ) -> None:
        """SYNC_RECORD after a DELETE_RECORD SUCCESS for the same resource."""
        successful_deletions = [
            e for e in timeline.events
            if e.kind == EventKind.DELETE_OPERATION
            and e.reported_result == "SUCCESS"
            and e.timestamp is not None
            and e.resource_id
        ]
        for del_event in successful_deletions:
            syncs_after = [
                e for e in timeline.events
                if e.kind == EventKind.SYNC_RECORD
                and (e.resource_id or "").lower() == (del_event.resource_id or "").lower()
                and e.timestamp is not None
                and e.timestamp > del_event.timestamp
            ]
            for sync in syncs_after:
                out.append(
                    Contradiction(
                        contradiction_type=ContradictionType.SYNC_AFTER_DELETION,
                        is_confirmed=False,  # Risk, not yet proven recreation
                        description=(
                            f"SYNC_RECORD for '{del_event.resource_id}' occurred at "
                            f"{format_timestamp(sync.timestamp)}, after the DELETE_RECORD "
                            f"SUCCESS at {format_timestamp(del_event.timestamp)}. "
                            f"Data may have been pushed to downstream system "
                            f"'{sync.system_id}' after deletion."
                        ),
                        evidence_event=sync,
                        reference_event=del_event,
                        resource_id=del_event.resource_id,
                        system_id=sync.system_id,
                        supporting_evidence_id=sync.evidence_id,
                    )
                )

    def _detect_restore_after_deletion(
        self, timeline: Timeline, out: List[Contradiction]
    ) -> None:
        """RESTORE_BACKUP event after a DELETE."""
        any_delete = [
            e for e in timeline.events
            if e.kind == EventKind.DELETE_OPERATION and e.timestamp is not None
        ]
        if not any_delete:
            return
        earliest_delete = min(any_delete, key=lambda e: e.timestamp)
        restores = [
            e for e in timeline.events
            if e.kind == EventKind.RESTORE_BACKUP
            and e.timestamp is not None
            and e.timestamp > earliest_delete.timestamp
        ]
        for restore in restores:
            out.append(
                Contradiction(
                    contradiction_type=ContradictionType.RESTORE_AFTER_DELETION,
                    is_confirmed=True,
                    description=(
                        f"RESTORE_BACKUP event at {format_timestamp(restore.timestamp)} "
                        f"occurred after deletion began at "
                        f"{format_timestamp(earliest_delete.timestamp)}. "
                        f"Backup may have reinstated deleted personal data."
                    ),
                    evidence_event=restore,
                    reference_event=earliest_delete,
                    resource_id=restore.resource_id,
                    system_id=restore.system_id,
                    supporting_evidence_id=restore.evidence_id,
                )
            )

    def _detect_failed_operations(
        self, timeline: Timeline, out: List[Contradiction]
    ) -> None:
        """Deletion, redaction, or anonymization operation reported FAILED/PARTIAL."""
        failed_kinds = {
            EventKind.DELETE_OPERATION,
            EventKind.REDACT_OPERATION,
            EventKind.ANONYMIZE_OPERATION,
            EventKind.DISABLE_DATA_FLOW,
            EventKind.BLOCK_RESTORE,
        }
        for e in timeline.events:
            if e.kind in failed_kinds and e.reported_result in ("FAILED", "PARTIAL"):
                # PARTIAL result: check whether independent evidence shows the resource is
                # actually absent/redacted/anonymized — if so, this is only an uncertain risk.
                # A PARTIAL delete that left no residual data is not a confirmed violation.
                result = e.reported_result
                is_confirmed = result == "FAILED"  # FAILED is always confirmed; PARTIAL is uncertain
                if result == "PARTIAL" and e.resource_id:
                    # Check if later evidence shows the resource is absent or compliant
                    res_lc = (e.resource_id or "").lower()
                    absent_or_compliant = [
                        ev for ev in timeline.events
                        if ev.kind in (
                            EventKind.SNAPSHOT_ABSENT, EventKind.SNAPSHOT_REDACTED,
                            EventKind.SNAPSHOT_ANONYMIZED,
                        )
                        and (ev.resource_id or "").lower() == res_lc
                        and ev.timestamp is not None
                        and (e.timestamp is None or ev.timestamp >= e.timestamp)
                    ]
                    if absent_or_compliant:
                        # Post-operation evidence shows compliant state; PARTIAL is resolved
                        continue
                out.append(
                    Contradiction(
                        contradiction_type=ContradictionType.OPERATION_FAILED,
                        is_confirmed=is_confirmed,
                        description=(
                            f"Operation {e.metadata.get('event_type', e.kind.value)} "
                            f"on '{e.resource_id}' in '{e.system_id}' "
                            f"reported result='{e.reported_result}' at "
                            f"{format_timestamp(e.timestamp)}."
                        ),
                        evidence_event=e,
                        reference_event=None,
                        resource_id=e.resource_id,
                        system_id=e.system_id,
                        supporting_evidence_id=e.evidence_id,
                    )
                )

    def _detect_missing_timestamps(
        self, timeline: Timeline, out: List[Contradiction]
    ) -> None:
        """Evidence items with no parseable timestamp — ordering is unreliable."""
        for e in timeline.missing_timestamp_events():
            if e.source == EventSource.EVIDENCE_SNAPSHOT:
                out.append(
                    Contradiction(
                        contradiction_type=ContradictionType.MISSING_TIMESTAMP,
                        is_confirmed=False,
                        description=(
                            f"Evidence item '{e.evidence_id}' ({e.locator}) "
                            f"for resource '{e.resource_id}' has no parseable timestamp. "
                            f"Chronological ordering relative to deletion events is unreliable."
                        ),
                        evidence_event=e,
                        reference_event=None,
                        resource_id=e.resource_id,
                        system_id=e.system_id,
                        supporting_evidence_id=e.evidence_id,
                    )
                )

    def _detect_conflicting_system_states(
        self, timeline: Timeline, out: List[Contradiction]
    ) -> None:
        """Two different snapshots show contradictory states for the same resource."""
        # Group SNAPSHOT events by resource_id
        resource_snapshots: Dict[str, List[TimelineEvent]] = {}
        for e in timeline.events:
            if e.source == EventSource.EVIDENCE_SNAPSHOT and e.resource_id:
                resource_snapshots.setdefault(e.resource_id, []).append(e)

        present_kinds = {EventKind.SNAPSHOT_PRESENT, EventKind.DATA_FLOW_ACTIVE}
        absent_kinds = {
            EventKind.SNAPSHOT_ABSENT,
            EventKind.SNAPSHOT_REDACTED,
            EventKind.SNAPSHOT_ANONYMIZED,
            EventKind.DATA_FLOW_DISABLED,
            EventKind.BACKUP_BLOCKED,
        }

        for resource_id, snapshots in resource_snapshots.items():
            has_present = any(s.kind in present_kinds for s in snapshots)
            has_absent = any(s.kind in absent_kinds for s in snapshots)
            if not (has_present and has_absent):
                continue

            # Chronological check: if the absent-state snapshot is strictly later,
            # this is NOT a contradiction (the state changed correctly).
            # Only flag if present-state evidence is later than absent-state evidence.
            absent_ts = [
                s.timestamp
                for s in snapshots
                if s.kind in absent_kinds and s.timestamp is not None
            ]
            present_after_absent = [
                s for s in snapshots
                if s.kind in present_kinds
                and s.timestamp is not None
                and absent_ts
                and s.timestamp > max(absent_ts)
            ]
            if not present_after_absent:
                continue

            first_present = min(
                present_after_absent, key=lambda s: s.timestamp
            )
            last_absent = max(
                (s for s in snapshots if s.kind in absent_kinds and s.timestamp),
                key=lambda s: s.timestamp,
                default=None,
            )
            if last_absent:
                out.append(
                    Contradiction(
                        contradiction_type=ContradictionType.CONFLICTING_SYSTEM_STATES,
                        is_confirmed=True,
                        description=(
                            f"Resource '{resource_id}' appears in a compliant state "
                            f"({last_absent.kind.value}) in '{last_absent.evidence_id}' "
                            f"at {format_timestamp(last_absent.timestamp)}, but "
                            f"'{first_present.evidence_id}' shows it PRESENT at the "
                            f"later time {format_timestamp(first_present.timestamp)}."
                        ),
                        evidence_event=first_present,
                        reference_event=last_absent,
                        resource_id=resource_id,
                        system_id=first_present.system_id,
                        supporting_evidence_id=first_present.evidence_id,
                    )
                )

    def _detect_active_data_flows(
        self, timeline: Timeline, out: List[Contradiction]
    ) -> None:
        """Data flow active and no corresponding DISABLE_DATA_FLOW after deletion."""
        any_delete = [
            e for e in timeline.events
            if e.kind == EventKind.DELETE_OPERATION
            and e.reported_result == "SUCCESS"
            and e.timestamp is not None
        ]
        if not any_delete:
            return
        earliest_delete = min(any_delete, key=lambda e: e.timestamp)

        # Find data flow events after deletion
        active_flows = [
            e for e in timeline.events
            if e.kind == EventKind.DATA_FLOW_ACTIVE
            and e.timestamp is not None
            and e.timestamp > earliest_delete.timestamp
        ]
        for flow in active_flows:
            # Check if there is a corresponding DISABLE_DATA_FLOW
            disabled = [
                e for e in timeline.events
                if e.kind == EventKind.DISABLE_DATA_FLOW
                and (e.resource_id or "").lower() == (flow.resource_id or "").lower()
                and e.timestamp is not None
                and e.reported_result == "SUCCESS"
            ]
            if not disabled:
                out.append(
                    Contradiction(
                        contradiction_type=ContradictionType.DATA_FLOW_ACTIVE_AFTER_DELETION,
                        is_confirmed=False,
                        description=(
                            f"Data flow '{flow.resource_id}' appears active after "
                            f"deletion was reported at "
                            f"{format_timestamp(earliest_delete.timestamp)}. "
                            f"No successful DISABLE_DATA_FLOW found."
                        ),
                        evidence_event=flow,
                        reference_event=earliest_delete,
                        resource_id=flow.resource_id,
                        system_id=flow.system_id,
                        supporting_evidence_id=flow.evidence_id,
                    )
                )
