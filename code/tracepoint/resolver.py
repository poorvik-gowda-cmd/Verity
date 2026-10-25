"""Identity resolution engine for Tracepoint.

Connects each deletion request to the correct internal user_id, resolving
both current and historical email addresses. Returns structured resolution
results with full provenance for downstream pipeline stages.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd


# ---------------------------------------------------------------------------
# Resolution status and method enumerations
# ---------------------------------------------------------------------------


class ResolutionStatus(str, Enum):
    """Status of an identity resolution attempt."""

    RESOLVED = "RESOLVED"
    """Unique match found with high confidence."""

    UNRESOLVED = "UNRESOLVED"
    """No matching identity found in any dataset."""

    AMBIGUOUS = "AMBIGUOUS"
    """Multiple conflicting candidates found; safe merge is impossible."""

    CONFLICTING = "CONFLICTING"
    """Two different users claim the same identifier at the same time."""


class MatchMethod(str, Enum):
    """How the identity was matched."""

    CURRENT_EMAIL = "CURRENT_EMAIL"
    """Email matches the current primary address in user_identities.csv."""

    HISTORICAL_EMAIL = "HISTORICAL_EMAIL"
    """Email matches an old_value in user_audit_log.csv (email field)."""

    EMAIL_HASH_SHA256 = "EMAIL_HASH_SHA256"
    """SHA-256 hash of the supplied email matches the stored email_hash."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentityLink:
    """A single verified relationship between a supplied identifier and a user_id."""

    user_id: str
    """Stable internal user reference."""

    match_method: MatchMethod
    """How this link was established."""

    source_file: str
    """Dataset file that provided the evidence (e.g. 'user_identities.csv')."""

    evidence_field: str
    """Column/field in the source file that produced the match."""

    matched_value: str
    """The canonical identifier value as stored (may differ in case from supplied value)."""

    effective_from: Optional[str]
    """ISO-8601 timestamp from which this identifier belonged to the user (None = unknown)."""

    effective_to: Optional[str]
    """ISO-8601 timestamp after which this identifier no longer belongs to the user (None = still current)."""

    audit_id: Optional[str] = None
    """audit_id from user_audit_log that produced a HISTORICAL_EMAIL link."""


@dataclass
class ResolutionResult:
    """Full resolution outcome for one deletion request."""

    case_id: str
    """Request identifier."""

    supplied_email: str
    """Email address as supplied in the deletion request (may be uppercase, old address, etc.)."""

    status: ResolutionStatus
    """Overall resolution outcome."""

    user_id: Optional[str]
    """Resolved user_id (None if UNRESOLVED or AMBIGUOUS)."""

    primary_link: Optional[IdentityLink]
    """The single link that determined the resolution."""

    all_links: List[IdentityLink] = field(default_factory=list)
    """All candidate links considered (useful for conflict inspection)."""

    all_historical_emails: List[str] = field(default_factory=list)
    """All email addresses (current + historical) known to belong to this user."""

    all_email_hashes: List[str] = field(default_factory=list)
    """All email hashes (current + historical) known to belong to this user."""

    current_email: Optional[str] = None
    """Current canonical email at resolution time (from user_identities.csv)."""

    current_email_hash: Optional[str] = None
    """Current email hash from user_identities.csv."""

    resolution_note: str = ""
    """Human-readable explanation (no raw PII beyond what the caller needs)."""

    @property
    def is_resolved(self) -> bool:
        return self.status == ResolutionStatus.RESOLVED

    def all_known_emails(self) -> List[str]:
        """Return all known email addresses for this user (deduplicated, lowercased)."""
        seen = set()
        result = []
        for e in self.all_historical_emails:
            lc = e.lower()
            if lc not in seen:
                seen.add(lc)
                result.append(lc)
        return result


# ---------------------------------------------------------------------------
# Identity resolver
# ---------------------------------------------------------------------------


class IdentityResolver:
    """Deterministic, evidence-backed identity resolver.

    Loads user_identities.csv and user_audit_log.csv once at construction
    and provides O(1) lookup for per-case resolution.

    Usage
    -----
    resolver = IdentityResolver.from_config()
    result = resolver.resolve("case_01", "amina.elmasri@gmail.com")
    if result.is_resolved:
        print(result.user_id)
    """

    # Source file labels used in IdentityLink.source_file
    _IDENTITY_FILE = "user_identities.csv"
    _AUDIT_FILE = "user_audit_log.csv"

    def __init__(
        self,
        identities_df: pd.DataFrame,
        audit_log_df: pd.DataFrame,
    ) -> None:
        """Initialise from already-loaded DataFrames (prefer from_paths / from_config)."""
        self._identities = identities_df.copy()
        self._audit_log = audit_log_df.copy()

        # Primary lookup tables (keyed by lowercased email)
        self._current_email_map: Dict[str, IdentityLink] = {}
        self._historical_email_map: Dict[str, IdentityLink] = {}
        self._hash_map: Dict[str, IdentityLink] = {}

        # Emails that map to two different user_ids (data quality conflict)
        self._conflicted_historical_emails: set = set()

        # Per-user accumulations (for building ResolutionResult extras)
        self._user_emails: Dict[str, List[str]] = {}  # user_id -> all known emails
        self._user_hashes: Dict[str, List[str]] = {}  # user_id -> all known hashes
        self._user_current_email: Dict[str, str] = {}
        self._user_current_hash: Dict[str, str] = {}

        self._build_indexes()

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_paths(
        cls,
        identities_path: Path,
        audit_log_path: Path,
    ) -> "IdentityResolver":
        """Load datasets from explicit file paths."""
        identities_df = pd.read_csv(identities_path)
        audit_log_df = pd.read_csv(audit_log_path)
        return cls(identities_df, audit_log_df)

    @classmethod
    def from_config(cls, cfg=None) -> "IdentityResolver":
        """Load datasets using the project config singleton."""
        if cfg is None:
            try:
                from config import config as _cfg  # type: ignore[import]
            except ImportError:
                from code.config import config as _cfg  # type: ignore[import]
            cfg = _cfg
        return cls.from_paths(cfg.user_identities_csv, cfg.user_audit_log_csv)

    # ------------------------------------------------------------------
    # Index construction
    # ------------------------------------------------------------------

    def _build_indexes(self) -> None:
        """Build all lookup structures from the loaded DataFrames."""
        # --- current identities ---
        for _, row in self._identities.iterrows():
            uid = str(row["user_id"])
            email = str(row["email"])
            email_lc = email.lower()
            email_hash = str(row["email_hash"])
            updated_at = str(row.get("updated_at", "")) or None

            link = IdentityLink(
                user_id=uid,
                match_method=MatchMethod.CURRENT_EMAIL,
                source_file=self._IDENTITY_FILE,
                evidence_field="email",
                matched_value=email,
                effective_from=None,
                effective_to=None,
            )
            # Prefer first occurrence if duplicates exist (shouldn't happen)
            if email_lc not in self._current_email_map:
                self._current_email_map[email_lc] = link

            # Email hash link
            hash_link = IdentityLink(
                user_id=uid,
                match_method=MatchMethod.EMAIL_HASH_SHA256,
                source_file=self._IDENTITY_FILE,
                evidence_field="email_hash",
                matched_value=email_hash,
                effective_from=None,
                effective_to=None,
            )
            if email_hash not in self._hash_map:
                self._hash_map[email_hash] = hash_link

            # Accumulate per-user collections
            self._user_emails.setdefault(uid, []).append(email)
            self._user_hashes.setdefault(uid, []).append(email_hash)
            self._user_current_email[uid] = email
            self._user_current_hash[uid] = email_hash

        # --- audit log: email changes ---
        email_events = self._audit_log[
            self._audit_log["field_name"] == "email"
        ].copy()

        # Sort by event_time so effective_to is set correctly for chains
        email_events = email_events.sort_values("event_time")

        # Build: for each user, list of (old_email, new_email, audit_id, event_time)
        user_email_history: Dict[str, List[Tuple[str, str, str, str]]] = {}
        for _, row in email_events.iterrows():
            uid = str(row["user_id"])
            old_val = str(row["old_value"])
            new_val = str(row["new_value"])
            audit_id = str(row["audit_id"])
            event_time = str(row["event_time"])
            user_email_history.setdefault(uid, []).append(
                (old_val, new_val, audit_id, event_time)
            )

        for uid, history in user_email_history.items():
            for idx, (old_email, new_email, audit_id, event_time) in enumerate(history):
                old_lc = old_email.lower()
                # effective_to is when this email was changed away
                effective_to = event_time
                # effective_from: end of previous change, or None
                effective_from = history[idx - 1][3] if idx > 0 else None

                link = IdentityLink(
                    user_id=uid,
                    match_method=MatchMethod.HISTORICAL_EMAIL,
                    source_file=self._AUDIT_FILE,
                    evidence_field="old_value",
                    matched_value=old_email,
                    effective_from=effective_from,
                    effective_to=effective_to,
                    audit_id=audit_id,
                )
                # Only store if not already a current-email match (current wins)
                if old_lc not in self._current_email_map and old_lc not in self._historical_email_map:
                    self._historical_email_map[old_lc] = link
                elif old_lc in self._historical_email_map:
                    # Check if this is a different user — genuine historical conflict
                    existing = self._historical_email_map[old_lc]
                    if existing.user_id != uid:
                        self._conflicted_historical_emails.add(old_lc)

                # Add historical email to per-user email accumulation
                if old_email not in self._user_emails.get(uid, []):
                    self._user_emails.setdefault(uid, []).append(old_email)

                # Compute SHA-256 of historical email and register in user hashes and hash map
                old_h = hashlib.sha256(old_email.strip().lower().encode()).hexdigest()
                if old_h not in self._user_hashes.get(uid, []):
                    self._user_hashes.setdefault(uid, []).append(old_h)
                if old_h not in self._hash_map:
                    self._hash_map[old_h] = link

        # --- audit log: email_hash changes ---
        hash_events = self._audit_log[
            self._audit_log["field_name"] == "email_hash"
        ].copy()
        for _, row in hash_events.iterrows():
            uid = str(row["user_id"])
            old_hash = str(row["old_value"])
            if old_hash not in self._hash_map:
                link = IdentityLink(
                    user_id=uid,
                    match_method=MatchMethod.EMAIL_HASH_SHA256,
                    source_file=self._AUDIT_FILE,
                    evidence_field="old_value",
                    matched_value=old_hash,
                    effective_from=None,
                    effective_to=str(row["event_time"]),
                    audit_id=str(row["audit_id"]),
                )
                self._hash_map[old_hash] = link

    # ------------------------------------------------------------------
    # Public resolution API
    # ------------------------------------------------------------------

    def resolve(self, case_id: str, supplied_email: str) -> ResolutionResult:
        """Resolve a supplied email to a unique internal user_id.

        Resolution order (first match wins):
        1. Case-insensitive current email match.
        2. Case-insensitive historical email match (user_audit_log old_value).
        3. SHA-256 hash of the lowercased supplied email against stored hashes.

        Parameters
        ----------
        case_id:
            The deletion request case identifier (used only for result labelling).
        supplied_email:
            Email as provided in the deletion request.

        Returns
        -------
        ResolutionResult with status RESOLVED, UNRESOLVED, AMBIGUOUS, or CONFLICTING.
        """
        supplied_lc = supplied_email.strip().lower()
        candidates: List[IdentityLink] = []

        # Step 0: check for known historical conflicts
        if supplied_lc in self._conflicted_historical_emails:
            return ResolutionResult(
                case_id=case_id,
                supplied_email=supplied_email,
                status=ResolutionStatus.CONFLICTING,
                user_id=None,
                primary_link=None,
                all_links=[],
                resolution_note=(
                    f"Historical email '{supplied_lc}' is claimed by multiple "
                    f"different users in user_audit_log. Cannot safely resolve."
                ),
            )

        # Step 1: current email match
        if supplied_lc in self._current_email_map:
            candidates.append(self._current_email_map[supplied_lc])

        # Step 2: historical email match
        if supplied_lc in self._historical_email_map:
            link = self._historical_email_map[supplied_lc]
            # Only add if not already found via current (same user via different path)
            if not any(c.user_id == link.user_id for c in candidates):
                candidates.append(link)

        # Step 3: email hash fallback
        if not candidates:
            sha256 = hashlib.sha256(supplied_lc.encode()).hexdigest()
            if sha256 in self._hash_map:
                candidates.append(self._hash_map[sha256])

        # --- Evaluate candidates ---
        if not candidates:
            return ResolutionResult(
                case_id=case_id,
                supplied_email=supplied_email,
                status=ResolutionStatus.UNRESOLVED,
                user_id=None,
                primary_link=None,
                all_links=candidates,
                resolution_note="No identity match found in current or historical records.",
            )

        # Check for conflicting users (same email mapped to two different user_ids)
        unique_users = {c.user_id for c in candidates}
        if len(unique_users) > 1:
            return ResolutionResult(
                case_id=case_id,
                supplied_email=supplied_email,
                status=ResolutionStatus.CONFLICTING,
                user_id=None,
                primary_link=None,
                all_links=candidates,
                resolution_note=(
                    f"Email resolves to {len(unique_users)} different users: "
                    f"{sorted(unique_users)}. Cannot safely merge."
                ),
            )

        # Unique resolution
        primary = candidates[0]
        uid = primary.user_id

        return ResolutionResult(
            case_id=case_id,
            supplied_email=supplied_email,
            status=ResolutionStatus.RESOLVED,
            user_id=uid,
            primary_link=primary,
            all_links=candidates,
            all_historical_emails=list(self._user_emails.get(uid, [])),
            all_email_hashes=list(self._user_hashes.get(uid, [])),
            current_email=self._user_current_email.get(uid),
            current_email_hash=self._user_current_hash.get(uid),
            resolution_note=(
                f"Resolved via {primary.match_method.value}"
                + (f" (audit_id={primary.audit_id})" if primary.audit_id else "")
            ),
        )

    def resolve_batch(
        self, cases: pd.DataFrame
    ) -> Dict[str, ResolutionResult]:
        """Resolve all cases in a DataFrame with columns case_id and email.

        Returns
        -------
        dict mapping case_id -> ResolutionResult
        """
        results: Dict[str, ResolutionResult] = {}
        for _, row in cases.iterrows():
            case_id = str(row["case_id"])
            email = str(row["email"])
            results[case_id] = self.resolve(case_id, email)
        return results

    def get_user_identity(self, user_id: str) -> Optional[pd.Series]:
        """Return the current identity row for a user_id, or None if not found."""
        rows = self._identities[self._identities["user_id"] == user_id]
        return rows.iloc[0] if not rows.empty else None

    def get_all_identifiers(self, user_id: str) -> Dict[str, List[str]]:
        """Return all known identifiers for a user (current + historical).

        Returns a dict with keys: 'emails', 'email_hashes'.
        """
        return {
            "emails": list(self._user_emails.get(user_id, [])),
            "email_hashes": list(self._user_hashes.get(user_id, [])),
        }

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def resolution_summary(self, results: Dict[str, ResolutionResult]) -> Dict:
        """Compute a summary of resolution outcomes across a batch."""
        by_status: Dict[str, int] = {}
        by_method: Dict[str, int] = {}
        for r in results.values():
            by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
            if r.primary_link:
                m = r.primary_link.match_method.value
                by_method[m] = by_method.get(m, 0) + 1
        return {
            "total": len(results),
            "by_status": by_status,
            "by_method": by_method,
            "resolution_rate": (
                by_status.get("RESOLVED", 0) / len(results) if results else 0.0
            ),
        }
