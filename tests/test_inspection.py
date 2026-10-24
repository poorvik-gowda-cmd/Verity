"""Tests for dataset inspection logic and privacy sanitization."""

import pytest

try:
    from config import config
    from inspect_dataset import (
        inspect_evidence_files,
        inspect_identity_and_requests,
        inspect_tabular_files,
    )
    from tracepoint.utils import mask_email, mask_name, mask_phone
except ImportError:
    from code.config import config
    from code.inspect_dataset import (
        inspect_evidence_files,
        inspect_identity_and_requests,
        inspect_tabular_files,
    )
    from code.tracepoint.utils import mask_email, mask_name, mask_phone


def test_tabular_inspection_finds_all_required_files():
    """Verify that all tabular dataset CSVs are found with valid schemas."""
    tabular_results = inspect_tabular_files(config.dataset_dir)
    assert len(tabular_results) == 9

    for filename, result in tabular_results.items():
        assert result["status"] == "OK", f"File {filename} returned error: {result}"
        assert result["schema_valid"] is True, f"Schema mismatch in {filename}"
        assert result["duplicate_rows"] == 0, f"Duplicate rows detected in {filename}"


def test_evidence_manifest_complete_match():
    """Verify that every file in evidence manifest exists on disk with 0 discrepancies."""
    evidence_results = inspect_evidence_files(config.dataset_dir)
    assert evidence_results["manifest_file_count"] == 94
    assert evidence_results["disk_file_count"] == 94
    assert len(evidence_results["missing_from_disk"]) == 0
    assert len(evidence_results["untracked_on_manifest"]) == 0


def test_identity_resolution_coverage():
    """Verify that 100% of sample and evaluation emails resolve to identities."""
    id_results = inspect_identity_and_requests(config.dataset_dir)
    assert id_results["sample_coverage"]["unresolved_emails"] == 0
    assert id_results["evaluation_coverage"]["unresolved_emails"] == 0
    assert id_results["not_applicable_candidates_count"] > 0


def test_privacy_masking_utilities():
    """Verify that personal identifiers are masked properly."""
    assert mask_email("john.doe@example.com") == "j***e@example.com"
    assert mask_email("al@domain.org") == "a***@domain.org"
    assert mask_phone("+1 (202) 555-0103") == "+1  ***-*** 03"
    assert mask_name("John Doe") == "J*** D***"
