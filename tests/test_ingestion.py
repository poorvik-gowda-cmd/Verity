"""Unit tests for Stage 2: Evidence ingestion, parsing, and SQLite FTS5 search."""

import json
from pathlib import Path
import pytest

from tracepoint.ingestion.indexer import EvidenceIndex
from tracepoint.ingestion.models import (
    ExtractionStatus,
    ExtractedItem,
    SourceType,
)
from tracepoint.ingestion.parsers import (
    parse_image_file,
    parse_json_file,
    parse_log_file,
    parse_pdf_file,
    parse_sql_file,
    parse_txt_file,
)
from tracepoint.ingestion.pipeline import IngestionPipeline


@pytest.fixture
def temp_fixtures_dir(tmp_path: Path) -> Path:
    """Create directory structure for isolated synthetic testing."""
    fixtures = tmp_path / "synthetic fixtures with spaces"
    fixtures.mkdir(parents=True)
    return fixtures


def test_sql_parser_synthetic(temp_fixtures_dir: Path):
    """Test SQL snapshot parsing into structured ExtractedItems."""
    sql_file = temp_fixtures_dir / "test_snapshot.sql"
    sql_file.write_text(
        """
        BEGIN TRANSACTION;
        CREATE TABLE test_users (resource_id TEXT PRIMARY KEY, email TEXT, updated_at TEXT);
        INSERT INTO test_users VALUES ('account_9999', 'test.user@example.com', '2026-09-01T10:00:00Z');
        COMMIT;
        """,
        encoding="utf-8",
    )

    items = list(parse_sql_file(sql_file, evidence_id="ev_sql_01"))
    assert len(items) == 1
    item = items[0]
    assert item.evidence_id == "ev_sql_01"
    assert item.resource_id == "account_9999"
    assert item.timestamp == "2026-09-01T10:00:00Z"
    assert item.source_type == SourceType.DATABASE_SQL
    assert "test.user@example.com" in item.identifiers
    assert "table:test_users/row:1" in item.locator


def test_json_parser_synthetic(temp_fixtures_dir: Path):
    """Test JSON snapshot bundle parsing."""
    json_file = temp_fixtures_dir / "test_bundle.json"
    data = {
        "exported_at": "2026-09-15T12:00:00Z",
        "snapshots": [
            {
                "captured_at": "2026-09-10T08:00:00Z",
                "data_store": "analytics_platform",
                "collection": "customer_reports",
                "records": [
                    {
                        "resource_id": "analytics_8888",
                        "customer_reference": "user_77",
                        "email": "analytics.user@domain.com",
                    }
                ],
            }
        ],
    }
    json_file.write_text(json.dumps(data), encoding="utf-8")

    items = list(parse_json_file(json_file, evidence_id="ev_json_01"))
    assert len(items) == 1
    item = items[0]
    assert item.resource_id == "analytics_8888"
    assert item.system_id == "analytics_platform"
    assert item.timestamp == "2026-09-10T08:00:00Z"
    assert item.source_type == SourceType.JSON_RECORD
    assert "analytics.user@domain.com" in item.identifiers
    assert "user_77" in item.identifiers


def test_log_parser_synthetic(temp_fixtures_dir: Path):
    """Test structured log parsing with key-value extractions."""
    log_file = temp_fixtures_dir / "service_trace.log"
    log_file.write_text(
        "2026-09-12T07:15:00Z level=INFO service=search_service resource_id=search_5555 event=index_hit matched_value=\"search.user@test.org\"\n"
        "2026-09-12T07:16:00Z level=WARN service=support_platform resource_id=support_7777 event=pii_detected subject_ref=\"user_99\"\n",
        encoding="utf-8",
    )

    items = list(parse_log_file(log_file, evidence_id="ev_log_01"))
    assert len(items) == 2
    assert items[0].system_id == "search_service"
    assert items[0].resource_id == "search_5555"
    assert items[0].timestamp == "2026-09-12T07:15:00Z"
    assert "search.user@test.org" in items[0].identifiers

    assert items[1].system_id == "support_platform"
    assert items[1].resource_id == "support_7777"
    assert "user_99" in items[1].identifiers


def test_txt_parser_synthetic(temp_fixtures_dir: Path):
    """Test pipe-delimited handover document parsing."""
    txt_file = temp_fixtures_dir / "handover.txt"
    txt_file.write_text(
        "- 2026-10-01T12:00:00Z | account_service | account_1111 | USER_PROFILE | NOT_FOUND | sample.user@test.com\n"
        "- 2026-10-01T12:00:00Z | backup_service | backup_2222 | BACKUP_ARCHIVE | BLOCKED | user_44\n",
        encoding="utf-8",
    )

    items = list(parse_txt_file(txt_file, evidence_id="ev_txt_01"))
    assert len(items) == 2
    assert items[0].system_id == "account_service"
    assert items[0].resource_id == "account_1111"
    assert "sample.user@test.com" in items[0].identifiers

    assert items[1].system_id == "backup_service"
    assert items[1].resource_id == "backup_2222"
    assert "user_44" in items[1].identifiers


def test_image_parser_declares_unsupported(temp_fixtures_dir: Path):
    """Test image file parser records status UNSUPPORTED with clear error."""
    img_file = temp_fixtures_dir / "console.png"
    img_file.write_bytes(b"\x89PNG\r\n\x1a\nfakeimagebytes")

    items = list(parse_image_file(img_file, evidence_id="ev_img_01"))
    assert len(items) == 1
    assert items[0].status == ExtractionStatus.UNSUPPORTED
    assert "OCR" in items[0].error_message


def test_malformed_and_missing_files(temp_fixtures_dir: Path):
    """Test graceful handling of corrupt and missing files without crashing."""
    corrupt_json = temp_fixtures_dir / "corrupt.json"
    corrupt_json.write_text("{ unclosed invalid json ...", encoding="utf-8")

    items = list(parse_json_file(corrupt_json, evidence_id="ev_bad_json"))
    assert len(items) == 1
    assert items[0].status == ExtractionStatus.ERROR
    assert "decoding error" in items[0].error_message


def test_evidence_index_search_and_idempotency():
    """Test FTS5 indexing, duplicate prevention, and search query precision."""
    index = EvidenceIndex()

    item1 = ExtractedItem(
        source_path="evidence/databases/replica.sql",
        evidence_id="evidence_57",
        source_type=SourceType.DATABASE_SQL,
        locator="table:search/row:1",
        content='{"resource_id":"search_001","email":"jonas.berg@outlook.com"}',
        system_id="search_service",
        resource_id="search_001",
        timestamp="2026-09-10T08:45:00Z",
        identifiers=["jonas.berg@outlook.com"],
    )

    # First insertion succeeds
    idx_id1 = index.add_item(item1)
    assert idx_id1 is not None

    # Duplicate insertion returns None (idempotent, no uncontrolled duplicates)
    idx_id2 = index.add_item(item1)
    assert idx_id2 is None

    # Search by email query
    results = index.search(query="jonas.berg@outlook.com")
    assert len(results) == 1
    match = results[0]
    assert match.evidence_id == "evidence_57"
    assert match.resource_id == "search_001"
    assert match.system_id == "search_service"
    assert match.locator == "table:search/row:1"
    assert "jonas.berg@outlook.com" in match.snippet

    # Search with filter
    filtered = index.search(query="", system_id="search_service", resource_id="search_001")
    assert len(filtered) == 1
    assert filtered[0].evidence_id == "evidence_57"

    index.close()


def test_paths_with_spaces(temp_fixtures_dir: Path):
    """Test that file paths containing spaces are parsed and indexed cleanly."""
    nested = temp_fixtures_dir / "folder with space"
    nested.mkdir()
    log_file = nested / "trace with space.log"
    log_file.write_text(
        "2026-09-20T12:00:00Z service=account_service resource_id=account_1234 event=test_event\n",
        encoding="utf-8",
    )

    items = list(parse_log_file(log_file, evidence_id="ev_space_01"))
    assert len(items) == 1
    assert items[0].system_id == "account_service"
    assert items[0].resource_id == "account_1234"
