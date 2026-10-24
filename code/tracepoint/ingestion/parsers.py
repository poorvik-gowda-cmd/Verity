"""Specialized parsers for all dataset evidence formats."""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import zlib
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .models import (
    ExtractionStatus,
    ExtractedItem,
    SourceType,
)

# Common regex patterns for grounded identifier discovery
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
USER_ID_REGEX = re.compile(r"\buser_[0-9a-zA-Z_-]+\b")
RESOURCE_ID_REGEX = re.compile(
    r"\b(?:account|audit_record|analytics|search|support|data_flow|backup|email_delivery|export|tombstone)_[0-9a-zA-Z_-]+\b"
)
ISO_TIMESTAMP_REGEX = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b")


def extract_identifiers(text: str) -> List[str]:
    """Find all candidate emails, user IDs, and resource IDs in text."""
    found: List[str] = []
    for match in EMAIL_REGEX.findall(text):
        if match not in found:
            found.append(match)
    for match in USER_ID_REGEX.findall(text):
        if match not in found:
            found.append(match)
    return found


def parse_sql_file(
    file_path: Path, evidence_id: str, default_captured_at: Optional[str] = None
) -> Iterator[ExtractedItem]:
    """Parse SQLite-compatible SQL snapshots into granular record items."""
    content = file_path.read_text(encoding="utf-8", errors="replace")

    # Extract capture time from header comment if present
    captured_at = default_captured_at
    for line in content.splitlines()[:10]:
        if "Captured at:" in line:
            m = ISO_TIMESTAMP_REGEX.search(line)
            if m:
                captured_at = m.group(0)

    try:
        conn = sqlite3.connect(":memory:")
        # Enable json functions in sqlite3
        conn.executescript(content)
        cursor = conn.cursor()

        # Query all user tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall()]

        for table in tables:
            cursor.execute(f"PRAGMA table_info({table});")
            col_info = cursor.fetchall()
            col_names = [c[1] for c in col_info]

            cursor.execute(f"SELECT * FROM {table};")
            rows = cursor.fetchall()

            for row_idx, row in enumerate(rows):
                row_dict = dict(zip(col_names, row))
                row_str = json.dumps(row_dict, default=str)

                resource_id = row_dict.get("resource_id")
                origin_system = row_dict.get("origin_system") or row_dict.get("system_id")
                timestamp = row_dict.get("updated_at") or row_dict.get("indexed_at") or captured_at

                # Inspect embedded JSON if column exists
                embedded_json: Dict[str, Any] = {}
                for col, val in row_dict.items():
                    if isinstance(val, str) and (val.startswith("{") or val.startswith("[")):
                        try:
                            parsed_json = json.loads(val)
                            if isinstance(parsed_json, dict):
                                embedded_json.update(parsed_json)
                        except Exception:
                            pass

                if not resource_id and "resource_id" in embedded_json:
                    resource_id = embedded_json["resource_id"]
                if not origin_system and "origin_system" in embedded_json:
                    origin_system = embedded_json["origin_system"]

                ids = extract_identifiers(row_str)

                yield ExtractedItem(
                    source_path=str(file_path),
                    evidence_id=evidence_id,
                    source_type=SourceType.DATABASE_SQL,
                    locator=f"table:{table}/row:{row_idx + 1}",
                    content=row_str,
                    system_id=str(origin_system) if origin_system else None,
                    resource_id=str(resource_id) if resource_id else None,
                    timestamp=timestamp,
                    identifiers=ids,
                    metadata={"table": table, "row_index": row_idx + 1, "columns": col_names},
                    status=ExtractionStatus.PARSED,
                )

        conn.close()
    except Exception as e:
        yield ExtractedItem(
            source_path=str(file_path),
            evidence_id=evidence_id,
            source_type=SourceType.DATABASE_SQL,
            locator="file",
            content=content[:500],
            status=ExtractionStatus.ERROR,
            error_message=f"SQL execution error: {e}",
        )


def parse_json_file(
    file_path: Path, evidence_id: str, default_captured_at: Optional[str] = None
) -> Iterator[ExtractedItem]:
    """Parse structured JSON evidence (snapshot bundles, exports, configurations)."""
    try:
        raw_text = file_path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw_text)
    except Exception as e:
        yield ExtractedItem(
            source_path=str(file_path),
            evidence_id=evidence_id,
            source_type=SourceType.JSON_RECORD,
            locator="file",
            content="",
            status=ExtractionStatus.ERROR,
            error_message=f"JSON decoding error: {e}",
        )
        return

    # Scenario A: Snapshot bundles (e.g. snapshots: [{captured_at, data_store, collection, records: [...] }])
    if isinstance(data, dict) and "snapshots" in data:
        exported_at = data.get("exported_at") or default_captured_at
        source = data.get("source")

        for snap_idx, snapshot in enumerate(data.get("snapshots", [])):
            captured_at = snapshot.get("captured_at") or exported_at
            data_store = snapshot.get("data_store") or source
            collection = snapshot.get("collection")

            for rec_idx, record in enumerate(snapshot.get("records", [])):
                rec_str = json.dumps(record, default=str)
                resource_id = record.get("resource_id") if isinstance(record, dict) else None
                ids = extract_identifiers(rec_str)

                yield ExtractedItem(
                    source_path=str(file_path),
                    evidence_id=evidence_id,
                    source_type=SourceType.JSON_RECORD,
                    locator=f"snapshot:{snap_idx + 1}/record:{rec_idx + 1}",
                    content=rec_str,
                    system_id=str(data_store) if data_store else None,
                    resource_id=str(resource_id) if resource_id else None,
                    timestamp=captured_at,
                    identifiers=ids,
                    metadata={"collection": collection, "snapshot_index": snap_idx + 1},
                    status=ExtractionStatus.PARSED,
                )

    # Scenario B: Flat records list (e.g. records: [{recorded_at, system, resource_id, ...}])
    elif isinstance(data, dict) and "records" in data:
        origin = data.get("origin") or data.get("source")
        exported_at = data.get("exported_at") or default_captured_at

        for rec_idx, record in enumerate(data.get("records", [])):
            rec_str = json.dumps(record, default=str)
            resource_id = record.get("resource_id") if isinstance(record, dict) else None
            system_id = record.get("system") or origin if isinstance(record, dict) else origin
            timestamp = record.get("recorded_at") or exported_at if isinstance(record, dict) else exported_at
            ids = extract_identifiers(rec_str)

            yield ExtractedItem(
                source_path=str(file_path),
                evidence_id=evidence_id,
                source_type=SourceType.JSON_RECORD,
                locator=f"record:{rec_idx + 1}",
                content=rec_str,
                system_id=str(system_id) if system_id else None,
                resource_id=str(resource_id) if resource_id else None,
                timestamp=timestamp,
                identifiers=ids,
                metadata={"record_index": rec_idx + 1},
                status=ExtractionStatus.PARSED,
            )

    # Scenario C: Generic JSON dict or list
    else:
        content_str = json.dumps(data, default=str)
        ids = extract_identifiers(content_str)
        yield ExtractedItem(
            source_path=str(file_path),
            evidence_id=evidence_id,
            source_type=SourceType.JSON_RECORD,
            locator="root",
            content=content_str[:4000],
            timestamp=default_captured_at,
            identifiers=ids,
            status=ExtractionStatus.PARSED,
        )


def parse_log_file(
    file_path: Path, evidence_id: str, default_captured_at: Optional[str] = None
) -> Iterator[ExtractedItem]:
    """Parse log files into structured line items with key-value extractions."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        yield ExtractedItem(
            source_path=str(file_path),
            evidence_id=evidence_id,
            source_type=SourceType.LOG_TRACE,
            locator="file",
            content="",
            status=ExtractionStatus.ERROR,
            error_message=f"Log read error: {e}",
        )
        return

    for line_idx, line in enumerate(content.splitlines()):
        line_clean = line.strip()
        if not line_clean:
            continue

        # Extract timestamp
        ts_match = ISO_TIMESTAMP_REGEX.search(line_clean)
        timestamp = ts_match.group(0) if ts_match else default_captured_at

        # Extract system / service
        service_match = re.search(r"\bservice=([^\s]+)", line_clean)
        system_id = service_match.group(1) if service_match else None

        # Extract resource_id
        res_match = re.search(r"\bresource_id=([^\s]+)", line_clean)
        resource_id = res_match.group(1) if res_match else None

        ids = extract_identifiers(line_clean)

        yield ExtractedItem(
            source_path=str(file_path),
            evidence_id=evidence_id,
            source_type=SourceType.LOG_TRACE,
            locator=f"line:{line_idx + 1}",
            content=line_clean,
            system_id=system_id,
            resource_id=resource_id,
            timestamp=timestamp,
            identifiers=ids,
            metadata={"line_number": line_idx + 1},
            status=ExtractionStatus.PARSED,
        )


def parse_txt_file(
    file_path: Path, evidence_id: str, default_captured_at: Optional[str] = None
) -> Iterator[ExtractedItem]:
    """Parse text handover and audit documents."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        yield ExtractedItem(
            source_path=str(file_path),
            evidence_id=evidence_id,
            source_type=SourceType.DOCUMENT_TXT,
            locator="file",
            content="",
            status=ExtractionStatus.ERROR,
            error_message=f"Text read error: {e}",
        )
        return

    for line_idx, line in enumerate(content.splitlines()):
        line_clean = line.strip()
        if not line_clean or line_clean.startswith("#"):
            continue

        # Check for pipe-delimited handover format:
        # - 2026-10-01T12:00:00Z | account_service | account_146 | USER_PROFILE | NOT_FOUND | e.lindberg18@yahoo.com
        if "|" in line_clean:
            parts = [p.strip() for p in line_clean.lstrip("- ").split("|")]
            timestamp = parts[0] if len(parts) > 0 and ISO_TIMESTAMP_REGEX.match(parts[0]) else default_captured_at
            system_id = parts[1] if len(parts) > 1 else None
            resource_id = parts[2] if len(parts) > 2 else None
        else:
            ts_match = ISO_TIMESTAMP_REGEX.search(line_clean)
            timestamp = ts_match.group(0) if ts_match else default_captured_at
            system_id = None
            resource_id = None

        ids = extract_identifiers(line_clean)

        yield ExtractedItem(
            source_path=str(file_path),
            evidence_id=evidence_id,
            source_type=SourceType.DOCUMENT_TXT,
            locator=f"line:{line_idx + 1}",
            content=line_clean,
            system_id=system_id,
            resource_id=resource_id,
            timestamp=timestamp,
            identifiers=ids,
            metadata={"line_number": line_idx + 1},
            status=ExtractionStatus.PARSED,
        )


def parse_pdf_file(
    file_path: Path, evidence_id: str, default_captured_at: Optional[str] = None
) -> Iterator[ExtractedItem]:
    """Extract text from ReportLab/FlateDecode/ASCII85 PDF documents using Python built-ins."""
    try:
        data = file_path.read_bytes()
    except Exception as e:
        yield ExtractedItem(
            source_path=str(file_path),
            evidence_id=evidence_id,
            source_type=SourceType.DOCUMENT_PDF,
            locator="file",
            content="",
            status=ExtractionStatus.ERROR,
            error_message=f"PDF read error: {e}",
        )
        return

    text_chunks: List[str] = []

    # Parse stream blocks with /Length
    for m in re.finditer(rb"<<\s*([^>]*?)\s*>>\s*stream[\r\n]+", data):
        dict_content = m.group(1)
        stream_start = m.end()

        length_match = re.search(rb"/Length\s+(\d+)", dict_content)
        if not length_match:
            continue
        length = int(length_match.group(1))
        stream_bytes = data[stream_start : stream_start + length].strip()

        is_ascii85 = b"ASCII85Decode" in dict_content
        is_flate = b"FlateDecode" in dict_content

        raw_stream = stream_bytes
        if is_ascii85:
            if not raw_stream.startswith(b"<~"):
                raw_stream = b"<~" + raw_stream
            if not raw_stream.endswith(b"~>"):
                raw_stream = raw_stream + b"~>"
            try:
                raw_stream = base64.a85decode(raw_stream, adobe=True)
            except Exception:
                continue

        if is_flate:
            try:
                decomp = zlib.decompress(raw_stream)
            except Exception:
                continue
        else:
            decomp = raw_stream

        # Extract textual literals in parentheses
        clean_decomp = decomp.replace(rb"\(", rb"__LPAREN__").replace(rb"\)", rb"__RPAREN__")
        matches = re.findall(rb"\((.*?)\)", clean_decomp)
        for part in matches:
            decoded = (
                part.replace(rb"__LPAREN__", rb"(")
                .replace(rb"__RPAREN__", rb")")
                .decode("latin1", errors="replace")
                .strip()
            )
            if decoded and len(decoded) > 1:
                text_chunks.append(decoded)

    if text_chunks:
        full_text = " | ".join(text_chunks)
        ids = extract_identifiers(full_text)
        yield ExtractedItem(
            source_path=str(file_path),
            evidence_id=evidence_id,
            source_type=SourceType.DOCUMENT_PDF,
            locator="document:extracted_stream",
            content=full_text,
            timestamp=default_captured_at,
            identifiers=ids,
            metadata={"token_count": len(text_chunks)},
            status=ExtractionStatus.PARSED,
        )
    else:
        yield ExtractedItem(
            source_path=str(file_path),
            evidence_id=evidence_id,
            source_type=SourceType.DOCUMENT_PDF,
            locator="file",
            content="",
            status=ExtractionStatus.UNSUPPORTED,
            error_message="No decompressed text stream discovered in PDF",
        )


def parse_image_file(
    file_path: Path, evidence_id: str, default_captured_at: Optional[str] = None
) -> Iterator[ExtractedItem]:
    """Catalog image files with provenance while declaring OCR/Vision requirement."""
    try:
        size_bytes = file_path.stat().st_size
    except Exception as e:
        yield ExtractedItem(
            source_path=str(file_path),
            evidence_id=evidence_id,
            source_type=SourceType.IMAGE_PNG,
            locator="file",
            content="",
            status=ExtractionStatus.ERROR,
            error_message=f"Image stat error: {e}",
        )
        return

    # Image files cannot be parsed with pure string parsers without OCR/Vision
    yield ExtractedItem(
        source_path=str(file_path),
        evidence_id=evidence_id,
        source_type=SourceType.IMAGE_PNG,
        locator="image_file",
        content="",
        timestamp=default_captured_at,
        metadata={"file_size_bytes": size_bytes},
        status=ExtractionStatus.UNSUPPORTED,
        error_message="Requires OCR or multimodal vision processing",
    )
