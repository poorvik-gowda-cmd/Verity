"""Unit and integration tests for Phase 4: Temporal Timeline & Contradiction Detection."""

from datetime import datetime, timezone
from pathlib import Path
import pytest
import pandas as pd

from tracepoint.ingestion.indexer import EvidenceIndex
from tracepoint.ingestion.models import ExtractedItem, ExtractionStatus, SourceType
from tracepoint.ingestion.pipeline import IngestionPipeline
from tracepoint.resolver import IdentityLink, IdentityResolver, MatchMethod, ResolutionResult, ResolutionStatus
from tracepoint.timeline import (
    Contradiction,
    ContradictionDetector,
    ContradictionType,
    EventKind,
    EventSource,
    Timeline,
    TimelineBuilder,
    TimelineEvent,
    format_timestamp,
    parse_timestamp,
)


# ---------------------------------------------------------------------------
# 1. Timestamp parsing & normalisation
# ---------------------------------------------------------------------------


def test_parse_timestamp_utc_z():
    """Verify standard ISO-8601 with Z suffix parsed to UTC datetime."""
    dt = parse_timestamp("2026-09-01T12:00:00Z")
    assert dt is not None
    assert dt.tzinfo == timezone.utc
    assert dt.year == 2026
    assert dt.month == 9
    assert dt.day == 1
    assert dt.hour == 12
    assert dt.minute == 0
    assert dt.second == 0


def test_parse_timestamp_with_positive_offset():
    """Verify ISO-8601 with +05:30 offset is normalised to UTC equivalent."""
    dt = parse_timestamp("2026-09-01T17:30:00+05:30")
    assert dt is not None
    assert dt.tzinfo == timezone.utc
    assert dt.hour == 12
    assert dt.minute == 0


def test_parse_timestamp_naive_string():
    """Verify naive timestamp string is safely treated as UTC."""
    dt = parse_timestamp("2026-09-01T12:00:00")
    assert dt is not None
    assert dt.tzinfo == timezone.utc
    assert dt.hour == 12


def test_parse_timestamp_invalid_and_empty():
    """Verify invalid, empty, and None strings return None without raising."""
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None
    assert parse_timestamp("   ") is None
    assert parse_timestamp("not-a-date") is None
    assert parse_timestamp("2026-99-99T99:99:99Z") is None


def test_format_timestamp():
    """Verify UTC datetime is formatted back to standard ISO-8601 Z string."""
    dt = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert format_timestamp(dt) == "2026-09-01T12:00:00Z"
    assert format_timestamp(None) is None


# ---------------------------------------------------------------------------
# 2. Timeline event model & chronological sorting
# ---------------------------------------------------------------------------


def test_timeline_event_properties():
    """Verify TimelineEvent properties and flags."""
    dt = parse_timestamp("2026-09-01T10:00:00Z")
    ev = TimelineEvent(
        source=EventSource.SYSTEM_ACTIVITY,
        kind=EventKind.DELETE_OPERATION,
        system_id="account_service",
        resource_id="account_001",
        timestamp=dt,
        raw_timestamp="2026-09-01T10:00:00Z",
        reported_result="SUCCESS",
        description="Delete account",
        evidence_id="act_01",
        locator=None,
    )
    assert not ev.timestamp_missing
    assert ev.is_deletion_operation
    assert not ev.is_sync_operation
    assert not ev.is_snapshot_present


def test_timeline_chronological_sorting():
    """Verify events are sorted ascending by timestamp, with None placed last."""
    t1 = parse_timestamp("2026-09-01T10:00:00Z")
    t2 = parse_timestamp("2026-09-02T10:00:00Z")
    t3 = parse_timestamp("2026-09-03T10:00:00Z")

    e_none = TimelineEvent(
        source=EventSource.EVIDENCE_SNAPSHOT,
        kind=EventKind.SNAPSHOT_PRESENT,
        system_id="s1",
        resource_id="r1",
        timestamp=None,
        raw_timestamp=None,
        reported_result=None,
        description="No time",
        evidence_id="ev_none",
        locator=None,
    )
    e1 = TimelineEvent(
        source=EventSource.SYSTEM_ACTIVITY,
        kind=EventKind.DELETE_OPERATION,
        system_id="s1",
        resource_id="r1",
        timestamp=t1,
        raw_timestamp="2026-09-01T10:00:00Z",
        reported_result="SUCCESS",
        description="Delete",
        evidence_id="act_01",
        locator=None,
    )
    e2 = TimelineEvent(
        source=EventSource.SYSTEM_ACTIVITY,
        kind=EventKind.SYNC_RECORD,
        system_id="s2",
        resource_id="r1",
        timestamp=t2,
        raw_timestamp="2026-09-02T10:00:00Z",
        reported_result="SUCCESS",
        description="Sync",
        evidence_id="act_02",
        locator=None,
    )
    e3 = TimelineEvent(
        source=EventSource.EVIDENCE_SNAPSHOT,
        kind=EventKind.SNAPSHOT_PRESENT,
        system_id="s2",
        resource_id="r1",
        timestamp=t3,
        raw_timestamp="2026-09-03T10:00:00Z",
        reported_result=None,
        description="Snapshot",
        evidence_id="ev_01",
        locator=None,
    )

    # Shuffled input
    events = [e3, e_none, e1, e2]
    events.sort(
        key=lambda e: (
            e.timestamp is None,
            e.timestamp or datetime.min.replace(tzinfo=timezone.utc),
        )
    )

    assert events[0] == e1
    assert events[1] == e2
    assert events[2] == e3
    assert events[3] == e_none
    assert events[3].timestamp_missing


# ---------------------------------------------------------------------------
# 3. Contradiction detection unit tests
# ---------------------------------------------------------------------------


def test_detect_residual_after_reported_deletion():
    """Detect record PRESENT in snapshot after DELETE_RECORD SUCCESS."""
    t_del = parse_timestamp("2026-09-01T10:00:00Z")
    t_snap = parse_timestamp("2026-09-02T10:00:00Z")

    del_event = TimelineEvent(
        source=EventSource.SYSTEM_ACTIVITY,
        kind=EventKind.DELETE_OPERATION,
        system_id="account_service",
        resource_id="account_001",
        timestamp=t_del,
        raw_timestamp="2026-09-01T10:00:00Z",
        reported_result="SUCCESS",
        description="DELETE",
        evidence_id="act_del",
        locator=None,
    )
    snap_event = TimelineEvent(
        source=EventSource.EVIDENCE_SNAPSHOT,
        kind=EventKind.SNAPSHOT_PRESENT,
        system_id="account_service",
        resource_id="account_001",
        timestamp=t_snap,
        raw_timestamp="2026-09-02T10:00:00Z",
        reported_result=None,
        description="Still present",
        evidence_id="ev_residual",
        locator="table:accounts/row:1",
    )

    timeline = Timeline(
        user_id="user_001",
        case_id="case_01",
        audit_at=parse_timestamp("2026-09-05T00:00:00Z"),
        audit_at_raw="2026-09-05T00:00:00Z",
        search_terms=["account_001"],
        events=[del_event, snap_event],
    )

    detector = ContradictionDetector()
    contradictions = detector.detect(timeline)

    residual_contradictions = [
        c for c in contradictions
        if c.contradiction_type == ContradictionType.RESIDUAL_AFTER_REPORTED_DELETION
    ]
    assert len(residual_contradictions) == 1
    assert residual_contradictions[0].is_confirmed
    assert residual_contradictions[0].supporting_evidence_id == "ev_residual"
    assert residual_contradictions[0].resource_id == "account_001"


def test_no_residual_if_snapshot_before_deletion():
    """Do not flag snapshot that occurred prior to deletion."""
    t_snap = parse_timestamp("2026-08-30T10:00:00Z")
    t_del = parse_timestamp("2026-09-01T10:00:00Z")

    snap_event = TimelineEvent(
        source=EventSource.EVIDENCE_SNAPSHOT,
        kind=EventKind.SNAPSHOT_PRESENT,
        system_id="account_service",
        resource_id="account_001",
        timestamp=t_snap,
        raw_timestamp="2026-08-30T10:00:00Z",
        reported_result=None,
        description="Present before deletion",
        evidence_id="ev_prior",
        locator=None,
    )
    del_event = TimelineEvent(
        source=EventSource.SYSTEM_ACTIVITY,
        kind=EventKind.DELETE_OPERATION,
        system_id="account_service",
        resource_id="account_001",
        timestamp=t_del,
        raw_timestamp="2026-09-01T10:00:00Z",
        reported_result="SUCCESS",
        description="DELETE",
        evidence_id="act_del",
        locator=None,
    )

    timeline = Timeline(
        user_id="user_001",
        case_id="case_01",
        audit_at=parse_timestamp("2026-09-05T00:00:00Z"),
        audit_at_raw="2026-09-05T00:00:00Z",
        search_terms=["account_001"],
        events=[snap_event, del_event],
    )

    detector = ContradictionDetector()
    contradictions = detector.detect(timeline)
    residuals = [
        c for c in contradictions
        if c.contradiction_type == ContradictionType.RESIDUAL_AFTER_REPORTED_DELETION
    ]
    assert len(residuals) == 0


def test_detect_sync_after_deletion():
    """Detect SYNC_RECORD event occurring after DELETE_RECORD SUCCESS."""
    t_del = parse_timestamp("2026-09-01T10:00:00Z")
    t_sync = parse_timestamp("2026-09-01T10:05:00Z")

    del_event = TimelineEvent(
        source=EventSource.SYSTEM_ACTIVITY,
        kind=EventKind.DELETE_OPERATION,
        system_id="account_service",
        resource_id="account_001",
        timestamp=t_del,
        raw_timestamp="2026-09-01T10:00:00Z",
        reported_result="SUCCESS",
        description="DELETE",
        evidence_id="act_del",
        locator=None,
    )
    sync_event = TimelineEvent(
        source=EventSource.SYSTEM_ACTIVITY,
        kind=EventKind.SYNC_RECORD,
        system_id="analytics_export",
        resource_id="account_001",
        timestamp=t_sync,
        raw_timestamp="2026-09-01T10:05:00Z",
        reported_result="SUCCESS",
        description="SYNC to analytics",
        evidence_id="act_sync",
        locator=None,
    )

    timeline = Timeline(
        user_id="user_001",
        case_id="case_01",
        audit_at=parse_timestamp("2026-09-05T00:00:00Z"),
        audit_at_raw="2026-09-05T00:00:00Z",
        search_terms=["account_001"],
        events=[del_event, sync_event],
    )

    detector = ContradictionDetector()
    contradictions = detector.detect(timeline)
    sync_contradictions = [
        c for c in contradictions
        if c.contradiction_type == ContradictionType.SYNC_AFTER_DELETION
    ]
    assert len(sync_contradictions) == 1
    assert not sync_contradictions[0].is_confirmed  # Recreation risk
    assert sync_contradictions[0].supporting_evidence_id == "act_sync"


def test_detect_restore_after_deletion():
    """Detect RESTORE_BACKUP event occurring after deletion was initiated."""
    t_del = parse_timestamp("2026-09-01T10:00:00Z")
    t_restore = parse_timestamp("2026-09-03T10:00:00Z")

    del_event = TimelineEvent(
        source=EventSource.SYSTEM_ACTIVITY,
        kind=EventKind.DELETE_OPERATION,
        system_id="account_service",
        resource_id="account_001",
        timestamp=t_del,
        raw_timestamp="2026-09-01T10:00:00Z",
        reported_result="SUCCESS",
        description="DELETE",
        evidence_id="act_del",
        locator=None,
    )
    restore_event = TimelineEvent(
        source=EventSource.SYSTEM_ACTIVITY,
        kind=EventKind.RESTORE_BACKUP,
        system_id="backup_service",
        resource_id="account_001",
        timestamp=t_restore,
        raw_timestamp="2026-09-03T10:00:00Z",
        reported_result="SUCCESS",
        description="RESTORE from cold storage",
        evidence_id="act_restore",
        locator=None,
    )

    timeline = Timeline(
        user_id="user_001",
        case_id="case_01",
        audit_at=parse_timestamp("2026-09-05T00:00:00Z"),
        audit_at_raw="2026-09-05T00:00:00Z",
        search_terms=["account_001"],
        events=[del_event, restore_event],
    )

    detector = ContradictionDetector()
    contradictions = detector.detect(timeline)
    restore_contradictions = [
        c for c in contradictions
        if c.contradiction_type == ContradictionType.RESTORE_AFTER_DELETION
    ]
    assert len(restore_contradictions) == 1
    assert restore_contradictions[0].is_confirmed
    assert restore_contradictions[0].supporting_evidence_id == "act_restore"


def test_detect_failed_operations():
    """Detect operations that reported FAILED or PARTIAL results."""
    t_op = parse_timestamp("2026-09-01T10:00:00Z")
    failed_event = TimelineEvent(
        source=EventSource.SYSTEM_ACTIVITY,
        kind=EventKind.REDACT_OPERATION,
        system_id="support_service",
        resource_id="ticket_999",
        timestamp=t_op,
        raw_timestamp="2026-09-01T10:00:00Z",
        reported_result="FAILED",
        description="REDACT FAILED",
        evidence_id="act_failed",
        locator=None,
        metadata={"event_type": "REDACT_RECORD"},
    )

    timeline = Timeline(
        user_id="user_001",
        case_id="case_01",
        audit_at=parse_timestamp("2026-09-05T00:00:00Z"),
        audit_at_raw="2026-09-05T00:00:00Z",
        search_terms=["ticket_999"],
        events=[failed_event],
    )

    detector = ContradictionDetector()
    contradictions = detector.detect(timeline)
    failed_contradictions = [
        c for c in contradictions
        if c.contradiction_type == ContradictionType.OPERATION_FAILED
    ]
    assert len(failed_contradictions) == 1
    assert failed_contradictions[0].is_confirmed
    assert failed_contradictions[0].supporting_evidence_id == "act_failed"


def test_detect_missing_timestamps():
    """Detect evidence snapshot with missing timestamp."""
    e_missing = TimelineEvent(
        source=EventSource.EVIDENCE_SNAPSHOT,
        kind=EventKind.SNAPSHOT_PRESENT,
        system_id="backup_service",
        resource_id="backup_file_01",
        timestamp=None,
        raw_timestamp=None,
        reported_result=None,
        description="Snapshot with no time",
        evidence_id="ev_untime",
        locator="page:1",
    )

    timeline = Timeline(
        user_id="user_001",
        case_id="case_01",
        audit_at=parse_timestamp("2026-09-05T00:00:00Z"),
        audit_at_raw="2026-09-05T00:00:00Z",
        search_terms=["backup_file_01"],
        events=[e_missing],
    )

    detector = ContradictionDetector()
    contradictions = detector.detect(timeline)
    missing_contradictions = [
        c for c in contradictions
        if c.contradiction_type == ContradictionType.MISSING_TIMESTAMP
    ]
    assert len(missing_contradictions) == 1
    assert not missing_contradictions[0].is_confirmed
    assert missing_contradictions[0].supporting_evidence_id == "ev_untime"


def test_detect_conflicting_system_states():
    """Detect contradiction when an absent/compliant snapshot is followed by a present snapshot."""
    t1 = parse_timestamp("2026-09-01T10:00:00Z")
    t2 = parse_timestamp("2026-09-02T10:00:00Z")

    snap_absent = TimelineEvent(
        source=EventSource.EVIDENCE_SNAPSHOT,
        kind=EventKind.SNAPSHOT_ABSENT,
        system_id="account_service",
        resource_id="account_001",
        timestamp=t1,
        raw_timestamp="2026-09-01T10:00:00Z",
        reported_result=None,
        description="Tombstone deleted",
        evidence_id="ev_tombstone",
        locator=None,
    )
    snap_present = TimelineEvent(
        source=EventSource.EVIDENCE_SNAPSHOT,
        kind=EventKind.SNAPSHOT_PRESENT,
        system_id="account_failover_replica",
        resource_id="account_001",
        timestamp=t2,
        raw_timestamp="2026-09-02T10:00:00Z",
        reported_result=None,
        description="Active record found",
        evidence_id="ev_present",
        locator=None,
    )

    timeline = Timeline(
        user_id="user_001",
        case_id="case_01",
        audit_at=parse_timestamp("2026-09-05T00:00:00Z"),
        audit_at_raw="2026-09-05T00:00:00Z",
        search_terms=["account_001"],
        events=[snap_absent, snap_present],
    )

    detector = ContradictionDetector()
    contradictions = detector.detect(timeline)
    conflict_contradictions = [
        c for c in contradictions
        if c.contradiction_type == ContradictionType.CONFLICTING_SYSTEM_STATES
    ]
    assert len(conflict_contradictions) == 1
    assert conflict_contradictions[0].is_confirmed
    assert conflict_contradictions[0].supporting_evidence_id == "ev_present"


# ---------------------------------------------------------------------------
# 4. TimelineBuilder & audit_at cutoff enforcement
# ---------------------------------------------------------------------------


def test_audit_at_cutoff_enforcement():
    """Verify events strictly after audit_at are excluded from timeline."""
    activity_data = pd.DataFrame([
        {
            "activity_id": "act_before",
            "event_time": "2026-09-01T10:00:00Z",
            "source_system": "account_service",
            "target_system": "account_service",
            "event_type": "DELETE_RECORD",
            "resource_id": "account_001",
            "details": "Account deleted account_001",
            "reported_result": "SUCCESS",
        },
        {
            "activity_id": "act_after",
            "event_time": "2026-09-10T10:00:00Z",
            "source_system": "account_service",
            "target_system": "account_service",
            "event_type": "DELETE_RECORD",
            "resource_id": "account_001",
            "details": "Later operation account_001",
            "reported_result": "SUCCESS",
        },
    ])

    index = EvidenceIndex()
    # Add an item before and after cutoff
    index.add_item(
        ExtractedItem(
            source_path="evidence/db1.sql",
            evidence_id="ev_before",
            source_type=SourceType.DATABASE_SQL,
            locator="row:1",
            content='{"resource_id": "account_001"}',
            system_id="db1",
            resource_id="account_001",
            timestamp="2026-09-02T10:00:00Z",
            identifiers=["account_001"],
        )
    )
    index.add_item(
        ExtractedItem(
            source_path="evidence/db2.sql",
            evidence_id="ev_after",
            source_type=SourceType.DATABASE_SQL,
            locator="row:1",
            content='{"resource_id": "account_001"}',
            system_id="db2",
            resource_id="account_001",
            timestamp="2026-09-15T10:00:00Z",
            identifiers=["account_001"],
        )
    )

    resolution = ResolutionResult(
        case_id="case_test",
        supplied_email="test@example.com",
        status=ResolutionStatus.RESOLVED,
        user_id="user_001",
        primary_link=None,
    )

    builder = TimelineBuilder(activity_data, index)
    timeline = builder.build_timeline(resolution, audit_at="2026-09-05T00:00:00Z")

    # Only act_before and ev_before should be present
    evidence_ids = {e.evidence_id for e in timeline.events}
    assert "act_before" in evidence_ids
    assert "ev_before" in evidence_ids
    assert "act_after" not in evidence_ids
    assert "ev_after" not in evidence_ids

    index.close()


# ---------------------------------------------------------------------------
# 5. Live integration test with sample case_05
# ---------------------------------------------------------------------------


def test_live_case_05_recreation_contradiction():
    """Live integration test: verify case_05 (user_05) detects sync recreation contradiction.

    In the real dataset:
    - Leila Haddad requested deletion (case_05).
    - Reported status: COMPLETE.
    - Activity activity_3482: DELETE_RECORD on account_005 at 2026-08-28 09:11:01Z.
    - Activity activity_3483: SYNC_RECORD on account_005 at 2026-08-28 09:11:09Z.
    - Snapshot evidence_54: account_005 still PRESENT at 2026-09-02 22:45:00Z.
    """
    pipeline = IngestionPipeline()
    pipeline.run()
    index = pipeline.index

    resolver = IdentityResolver.from_config()
    res = resolver.resolve("case_05", "leila.haddad@yahoo.com")
    assert res.is_resolved
    assert res.user_id == "user_05"

    activity_df = pd.read_csv("dataset/system_activity.csv")
    builder = TimelineBuilder(activity_df, index, resolver)
    timeline = builder.build_timeline(res, audit_at="2026-09-08T13:40:00Z")

    assert len(timeline.events) >= 3

    # Check the presence of key events
    act_ids = {e.evidence_id for e in timeline.events}
    assert "activity_3482" in act_ids
    assert "activity_3483" in act_ids
    assert "evidence_54" in act_ids

    detector = ContradictionDetector()
    contradictions = detector.detect(timeline)

    # Must detect both residual and sync contradictions
    ctypes = {c.contradiction_type for c in contradictions}
    assert ContradictionType.RESIDUAL_AFTER_REPORTED_DELETION in ctypes
    assert ContradictionType.SYNC_AFTER_DELETION in ctypes

    # Residual contradiction must be confirmed and cite evidence_54
    res_c = [c for c in contradictions if c.contradiction_type == ContradictionType.RESIDUAL_AFTER_REPORTED_DELETION][0]
    assert res_c.is_confirmed
    assert res_c.supporting_evidence_id == "evidence_54"
    assert res_c.resource_id == "account_005"
