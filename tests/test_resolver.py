"""Test suite for Phase 3: Identity Resolution Engine.

Covers:
- Current email matches (exact and case-insensitive)
- Historical email matches (audit log chains)
- Email hash fallback matches
- Unresolved identities
- Conflicting identities (same email, different users)
- Ambiguous/multi-hop historical chains
- Temporal consistency (effective_from / effective_to)
- Provenance fields (source_file, evidence_field, audit_id)
- Batch resolution
- Sample dataset accuracy (all 32 public cases)
- Resolution summary diagnostics
"""

from __future__ import annotations

import hashlib
from io import StringIO
from pathlib import Path
from typing import Dict

import pandas as pd
import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "code"))

from tracepoint.resolver import (
    IdentityLink,
    IdentityResolver,
    MatchMethod,
    ResolutionResult,
    ResolutionStatus,
)


# ---------------------------------------------------------------------------
# Synthetic test fixtures
# ---------------------------------------------------------------------------

IDENTITIES_CSV = """user_id,name,email,phone,email_hash,updated_at
user_01,Alice Smith,alice@example.com,+1-555-0001,{hash_alice},2026-01-01T00:00:00Z
user_02,Bob Jones,bob@example.com,+1-555-0002,{hash_bob},2026-01-01T00:00:00Z
user_03,Carol White,carol@example.com,+1-555-0003,{hash_carol},2026-01-01T00:00:00Z
user_04,Dan Brown,dan@example.com,+1-555-0004,{hash_dan},2026-01-01T00:00:00Z
""".format(
    hash_alice=hashlib.sha256("alice@example.com".encode()).hexdigest(),
    hash_bob=hashlib.sha256("bob@example.com".encode()).hexdigest(),
    hash_carol=hashlib.sha256("carol@example.com".encode()).hexdigest(),
    hash_dan=hashlib.sha256("dan@example.com".encode()).hexdigest(),
)

AUDIT_LOG_CSV = """audit_id,user_id,event_time,field_name,old_value,new_value
audit_01,user_02,2025-06-01T10:00:00Z,email,bob.old@example.com,bob@example.com
audit_02,user_02,2025-06-01T10:00:00Z,email_hash,{old_hash_bob},{new_hash_bob}
audit_03,user_03,2025-03-15T08:00:00Z,email,carol.older@example.com,carol.intermediate@example.com
audit_04,user_03,2025-08-20T12:00:00Z,email,carol.intermediate@example.com,carol@example.com
audit_05,user_03,2025-03-15T08:00:00Z,email_hash,{old_hash_carol_older},{mid_hash_carol}
audit_06,user_03,2025-08-20T12:00:00Z,email_hash,{mid_hash_carol},{new_hash_carol}
audit_07,user_04,2025-01-01T00:00:00Z,phone,+1-555-9999,+1-555-0004
""".format(
    old_hash_bob=hashlib.sha256("bob.old@example.com".encode()).hexdigest(),
    new_hash_bob=hashlib.sha256("bob@example.com".encode()).hexdigest(),
    old_hash_carol_older=hashlib.sha256("carol.older@example.com".encode()).hexdigest(),
    mid_hash_carol=hashlib.sha256("carol.intermediate@example.com".encode()).hexdigest(),
    new_hash_carol=hashlib.sha256("carol@example.com".encode()).hexdigest(),
)

# Conflict scenario: two users share the same historical email (data quality issue)
CONFLICT_AUDIT_CSV = """audit_id,user_id,event_time,field_name,old_value,new_value
audit_C1,user_01,2025-01-01T00:00:00Z,email,shared@example.com,alice@example.com
audit_C2,user_02,2025-01-02T00:00:00Z,email,shared@example.com,bob@example.com
"""

CONFLICT_IDENTITIES_CSV = """user_id,name,email,phone,email_hash,updated_at
user_01,Alice Smith,alice@example.com,+1-555-0001,{h1},2026-01-01T00:00:00Z
user_02,Bob Jones,bob@example.com,+1-555-0002,{h2},2026-01-01T00:00:00Z
""".format(
    h1=hashlib.sha256("alice@example.com".encode()).hexdigest(),
    h2=hashlib.sha256("bob@example.com".encode()).hexdigest(),
)


@pytest.fixture
def resolver() -> IdentityResolver:
    """Standard synthetic resolver."""
    ident_df = pd.read_csv(StringIO(IDENTITIES_CSV))
    audit_df = pd.read_csv(StringIO(AUDIT_LOG_CSV))
    return IdentityResolver(ident_df, audit_df)


@pytest.fixture
def conflict_resolver() -> IdentityResolver:
    """Resolver with conflicting historical email (two users, same old email)."""
    ident_df = pd.read_csv(StringIO(CONFLICT_IDENTITIES_CSV))
    audit_df = pd.read_csv(StringIO(CONFLICT_AUDIT_CSV))
    return IdentityResolver(ident_df, audit_df)


@pytest.fixture
def live_resolver() -> IdentityResolver:
    """Resolver loaded from the actual dataset (requires dataset present)."""
    return IdentityResolver.from_config()


# ---------------------------------------------------------------------------
# 1. Current email matches
# ---------------------------------------------------------------------------


class TestCurrentEmailMatch:
    def test_exact_match(self, resolver):
        r = resolver.resolve("case_t01", "alice@example.com")
        assert r.status == ResolutionStatus.RESOLVED
        assert r.user_id == "user_01"
        assert r.primary_link.match_method == MatchMethod.CURRENT_EMAIL

    def test_case_insensitive_uppercase(self, resolver):
        r = resolver.resolve("case_t02", "ALICE@EXAMPLE.COM")
        assert r.status == ResolutionStatus.RESOLVED
        assert r.user_id == "user_01"

    def test_case_insensitive_mixed(self, resolver):
        r = resolver.resolve("case_t03", "Alice@Example.COM")
        assert r.status == ResolutionStatus.RESOLVED
        assert r.user_id == "user_01"

    def test_leading_trailing_whitespace(self, resolver):
        r = resolver.resolve("case_t04", "  alice@example.com  ")
        assert r.status == ResolutionStatus.RESOLVED
        assert r.user_id == "user_01"

    def test_provenance_fields_current(self, resolver):
        r = resolver.resolve("case_t05", "alice@example.com")
        assert r.primary_link is not None
        assert r.primary_link.source_file == "user_identities.csv"
        assert r.primary_link.evidence_field == "email"
        assert r.primary_link.matched_value == "alice@example.com"
        assert r.primary_link.audit_id is None


# ---------------------------------------------------------------------------
# 2. Historical email matches
# ---------------------------------------------------------------------------


class TestHistoricalEmailMatch:
    def test_single_hop_historical(self, resolver):
        r = resolver.resolve("case_t06", "bob.old@example.com")
        assert r.status == ResolutionStatus.RESOLVED
        assert r.user_id == "user_02"
        assert r.primary_link.match_method == MatchMethod.HISTORICAL_EMAIL

    def test_historical_provenance_has_audit_id(self, resolver):
        r = resolver.resolve("case_t07", "bob.old@example.com")
        assert r.primary_link.audit_id == "audit_01"
        assert r.primary_link.source_file == "user_audit_log.csv"
        assert r.primary_link.effective_to == "2025-06-01T10:00:00Z"

    def test_multi_hop_chain_oldest(self, resolver):
        """carol.older was email before two changes — must still resolve."""
        r = resolver.resolve("case_t08", "carol.older@example.com")
        assert r.status == ResolutionStatus.RESOLVED
        assert r.user_id == "user_03"
        assert r.primary_link.match_method == MatchMethod.HISTORICAL_EMAIL

    def test_multi_hop_chain_intermediate(self, resolver):
        """carol.intermediate was the email between two changes."""
        r = resolver.resolve("case_t09", "carol.intermediate@example.com")
        assert r.status == ResolutionStatus.RESOLVED
        assert r.user_id == "user_03"
        assert r.primary_link.match_method == MatchMethod.HISTORICAL_EMAIL

    def test_historical_is_case_insensitive(self, resolver):
        r = resolver.resolve("case_t10", "BOB.OLD@EXAMPLE.COM")
        assert r.status == ResolutionStatus.RESOLVED
        assert r.user_id == "user_02"

    def test_all_historical_emails_populated(self, resolver):
        r = resolver.resolve("case_t11", "carol@example.com")
        assert r.status == ResolutionStatus.RESOLVED
        # Should include current + 2 historical emails
        emails_lc = r.all_known_emails()
        assert "carol.older@example.com" in emails_lc
        assert "carol.intermediate@example.com" in emails_lc
        assert "carol@example.com" in emails_lc


# ---------------------------------------------------------------------------
# 3. Email hash fallback
# ---------------------------------------------------------------------------


class TestEmailHashFallback:
    def test_hash_fallback_not_triggered_when_direct_match_exists(self, resolver):
        """For an email that matches directly, hash lookup should not be used."""
        r = resolver.resolve("case_t12", "alice@example.com")
        assert r.primary_link.match_method == MatchMethod.CURRENT_EMAIL

    def test_historical_hash_not_current_hash(self, resolver):
        """Historical hash (bob.old) should resolve bob via hash map fallback
        if we didn't have an explicit historical email record."""
        old_hash = hashlib.sha256("bob.old@example.com".encode()).hexdigest()
        # The old hash is stored in the audit log — resolver must index it
        ident_df = pd.read_csv(StringIO(IDENTITIES_CSV))
        audit_df = pd.read_csv(StringIO(AUDIT_LOG_CSV))
        r2 = IdentityResolver(ident_df, audit_df)
        # Verify hash map includes the old bob hash
        assert old_hash in r2._hash_map
        assert r2._hash_map[old_hash].user_id == "user_02"


# ---------------------------------------------------------------------------
# 4. Unresolved identities
# ---------------------------------------------------------------------------


class TestUnresolved:
    def test_unknown_email_returns_unresolved(self, resolver):
        r = resolver.resolve("case_t13", "nobody@nowhere.com")
        assert r.status == ResolutionStatus.UNRESOLVED
        assert r.user_id is None
        assert r.primary_link is None

    def test_empty_links_on_unresolved(self, resolver):
        r = resolver.resolve("case_t14", "nobody@nowhere.com")
        assert r.all_links == []

    def test_resolution_note_present(self, resolver):
        r = resolver.resolve("case_t15", "nobody@nowhere.com")
        assert r.resolution_note != ""

    def test_phone_only_audit_entry_does_not_resolve(self, resolver):
        """Audit log entries for phone changes must not create email links."""
        r = resolver.resolve("case_t16", "+1-555-9999")
        assert r.status == ResolutionStatus.UNRESOLVED


# ---------------------------------------------------------------------------
# 5. Conflicting identities
# ---------------------------------------------------------------------------


class TestConflicting:
    def test_same_historical_email_two_users_is_conflicting(self, conflict_resolver):
        r = conflict_resolver.resolve("case_t17", "shared@example.com")
        assert r.status == ResolutionStatus.CONFLICTING
        assert r.user_id is None
        # Resolution note must indicate a multi-user conflict
        assert "multiple" in r.resolution_note.lower() or "conflict" in r.resolution_note.lower()

    def test_current_email_still_works_when_other_conflict_exists(self, conflict_resolver):
        """Conflict on historical shared@ should not affect clean current-email matches."""
        r = conflict_resolver.resolve("case_t18", "alice@example.com")
        assert r.status == ResolutionStatus.RESOLVED
        assert r.user_id == "user_01"


# ---------------------------------------------------------------------------
# 6. Temporal consistency
# ---------------------------------------------------------------------------


class TestTemporalConsistency:
    def test_effective_to_set_on_historical_link(self, resolver):
        r = resolver.resolve("case_t19", "bob.old@example.com")
        assert r.primary_link.effective_to is not None
        assert r.primary_link.effective_to == "2025-06-01T10:00:00Z"

    def test_current_email_effective_to_is_none(self, resolver):
        """Current email has no expiry — still valid."""
        r = resolver.resolve("case_t20", "alice@example.com")
        assert r.primary_link.effective_to is None

    def test_multi_hop_effective_to_matches_change_time(self, resolver):
        r = resolver.resolve("case_t21", "carol.older@example.com")
        # carol.older was changed at 2025-03-15
        assert r.primary_link.effective_to == "2025-03-15T08:00:00Z"

    def test_intermediate_email_effective_to_correct(self, resolver):
        r = resolver.resolve("case_t22", "carol.intermediate@example.com")
        # carol.intermediate was changed at 2025-08-20
        assert r.primary_link.effective_to == "2025-08-20T12:00:00Z"


# ---------------------------------------------------------------------------
# 7. Batch resolution and diagnostics
# ---------------------------------------------------------------------------


class TestBatchResolution:
    def test_batch_returns_all_case_ids(self, resolver):
        cases = pd.DataFrame([
            {"case_id": "c1", "email": "alice@example.com"},
            {"case_id": "c2", "email": "bob.old@example.com"},
            {"case_id": "c3", "email": "nobody@nowhere.com"},
        ])
        results = resolver.resolve_batch(cases)
        assert set(results.keys()) == {"c1", "c2", "c3"}

    def test_batch_statuses_correct(self, resolver):
        cases = pd.DataFrame([
            {"case_id": "c1", "email": "alice@example.com"},
            {"case_id": "c2", "email": "bob.old@example.com"},
            {"case_id": "c3", "email": "nobody@nowhere.com"},
        ])
        results = resolver.resolve_batch(cases)
        assert results["c1"].status == ResolutionStatus.RESOLVED
        assert results["c2"].status == ResolutionStatus.RESOLVED
        assert results["c3"].status == ResolutionStatus.UNRESOLVED

    def test_summary_counts(self, resolver):
        cases = pd.DataFrame([
            {"case_id": "c1", "email": "alice@example.com"},
            {"case_id": "c2", "email": "bob.old@example.com"},
            {"case_id": "c3", "email": "nobody@nowhere.com"},
        ])
        results = resolver.resolve_batch(cases)
        summary = resolver.resolution_summary(results)
        assert summary["total"] == 3
        assert summary["by_status"]["RESOLVED"] == 2
        assert summary["by_status"]["UNRESOLVED"] == 1
        assert abs(summary["resolution_rate"] - 2 / 3) < 0.001

    def test_get_user_identity(self, resolver):
        identity = resolver.get_user_identity("user_01")
        assert identity is not None
        assert identity["email"] == "alice@example.com"

    def test_get_user_identity_unknown_returns_none(self, resolver):
        assert resolver.get_user_identity("user_9999") is None

    def test_get_all_identifiers_includes_historical(self, resolver):
        ids = resolver.get_all_identifiers("user_03")
        emails_lc = [e.lower() for e in ids["emails"]]
        assert "carol.older@example.com" in emails_lc
        assert "carol.intermediate@example.com" in emails_lc
        assert "carol@example.com" in emails_lc


# ---------------------------------------------------------------------------
# 8. Live dataset: all 32 sample requests
# ---------------------------------------------------------------------------


class TestLiveSampleDataset:
    """Evaluate resolution against the 32 public sample cases.

    Uses sample_output.csv only to check resolution success — not to
    look up deletion status answers at runtime.
    """

    def _load(self):
        try:
            from config import config
        except ImportError:
            from code.config import config  # type: ignore[import]
        return config

    def test_all_32_sample_cases_resolve(self, live_resolver):
        cfg = self._load()
        sample_req = pd.read_csv(cfg.sample_deletion_requests_csv)
        results = live_resolver.resolve_batch(sample_req)

        unresolved = [
            (cid, r.supplied_email)
            for cid, r in results.items()
            if r.status != ResolutionStatus.RESOLVED
        ]
        assert unresolved == [], (
            f"Expected all 32 sample cases to resolve, but {len(unresolved)} did not: "
            f"{unresolved}"
        )

    def test_sample_resolution_methods_breakdown(self, live_resolver):
        cfg = self._load()
        sample_req = pd.read_csv(cfg.sample_deletion_requests_csv)
        results = live_resolver.resolve_batch(sample_req)
        summary = live_resolver.resolution_summary(results)

        # All 32 should be resolved
        assert summary["by_status"].get("RESOLVED", 0) == 32, (
            f"Resolution breakdown: {summary}"
        )
        # Must use both current and historical methods
        assert "CURRENT_EMAIL" in summary["by_method"]
        assert "HISTORICAL_EMAIL" in summary["by_method"]

    def test_historical_email_cases_correct_user(self, live_resolver):
        """case_10 uses nadia.ionescu@yahoo.com (historical) -> must resolve to user_10."""
        r = live_resolver.resolve("case_10", "nadia.ionescu@yahoo.com")
        assert r.status == ResolutionStatus.RESOLVED
        assert r.user_id == "user_10"
        assert r.primary_link.match_method == MatchMethod.HISTORICAL_EMAIL

    def test_historical_email_case_29(self, live_resolver):
        """case_29: noah.f438085@outlook.com (historical) -> user_29."""
        r = live_resolver.resolve("case_29", "noah.f438085@outlook.com")
        assert r.status == ResolutionStatus.RESOLVED
        assert r.user_id == "user_29"

    def test_historical_email_case_30(self, live_resolver):
        """case_30: camille.bernard86@outlook.com (historical) -> user_30."""
        r = live_resolver.resolve("case_30", "camille.bernard86@outlook.com")
        assert r.status == ResolutionStatus.RESOLVED
        assert r.user_id == "user_30"

    def test_all_518_evaluation_cases_resolve(self, live_resolver):
        """All 518 evaluation cases must resolve (case-insensitive email matching)."""
        cfg = self._load()
        eval_req = pd.read_csv(cfg.deletion_requests_csv)
        results = live_resolver.resolve_batch(eval_req)
        summary = live_resolver.resolution_summary(results)

        unresolved = [
            (cid, r.supplied_email)
            for cid, r in results.items()
            if r.status != ResolutionStatus.RESOLVED
        ]
        assert unresolved == [], (
            f"{len(unresolved)} of 518 evaluation cases unresolved: {unresolved[:5]}"
        )
        assert summary["by_status"].get("RESOLVED", 0) == 518

    def test_uppercase_email_resolves(self, live_resolver):
        """case_38 has SIPHO.MOKOENA@GMAIL.COM (uppercase) — must still resolve."""
        r = live_resolver.resolve("case_38", "SIPHO.MOKOENA@GMAIL.COM")
        assert r.status == ResolutionStatus.RESOLVED
        assert r.user_id == "user_36"

    def test_case_insensitive_historical_resolves(self, live_resolver):
        """case_85 has NILS.L200695@ZOHO.COM (uppercase historical email)."""
        r = live_resolver.resolve("case_85", "NILS.L200695@ZOHO.COM")
        assert r.status == ResolutionStatus.RESOLVED
        assert r.primary_link.match_method == MatchMethod.HISTORICAL_EMAIL

    def test_no_conflicts_in_live_data(self, live_resolver):
        """The live dataset must have no conflicting resolution."""
        cfg = self._load()
        all_req = pd.concat([
            pd.read_csv(cfg.deletion_requests_csv),
            pd.read_csv(cfg.sample_deletion_requests_csv),
        ], ignore_index=True)
        results = live_resolver.resolve_batch(all_req)
        conflicts = [
            cid for cid, r in results.items()
            if r.status == ResolutionStatus.CONFLICTING
        ]
        assert conflicts == [], f"Unexpected conflicts: {conflicts}"

    def test_all_historical_emails_populated_for_multi_hop(self, live_resolver):
        """user_10 (Nadia) has 2 email changes — all_historical_emails must have 3 total."""
        r = live_resolver.resolve("case_10", "nadia.ionescu@yahoo.com")
        assert r.status == ResolutionStatus.RESOLVED
        all_emails = r.all_known_emails()
        # Must include the current and at least 2 historical forms
        assert len(all_emails) >= 3

    def test_get_all_identifiers_has_hashes(self, live_resolver):
        """get_all_identifiers must return email_hashes for any resolved user."""
        r = live_resolver.resolve("case_01", "amina.elmasri@gmail.com")
        assert r.is_resolved
        ids = live_resolver.get_all_identifiers(r.user_id)
        assert len(ids["email_hashes"]) >= 1
        assert len(ids["emails"]) >= 1

    def test_resolution_result_is_resolved_property(self, live_resolver):
        r = live_resolver.resolve("case_01", "amina.elmasri@gmail.com")
        assert r.is_resolved is True
        r2 = live_resolver.resolve("case_x", "nobody@missing.com")
        assert r2.is_resolved is False
