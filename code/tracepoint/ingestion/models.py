"""Ingestion models and provenance data structures for Tracepoint."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class SourceType(str, Enum):
    DATABASE_SQL = "DATABASE_SQL"
    JSON_RECORD = "JSON_RECORD"
    LOG_TRACE = "LOG_TRACE"
    DOCUMENT_PDF = "DOCUMENT_PDF"
    DOCUMENT_TXT = "DOCUMENT_TXT"
    TABULAR_CSV = "TABULAR_CSV"
    IMAGE_PNG = "IMAGE_PNG"
    UNKNOWN = "UNKNOWN"


class ExtractionStatus(str, Enum):
    PARSED = "PARSED"
    UNSUPPORTED = "UNSUPPORTED"
    PARTIAL = "PARTIAL"
    ERROR = "ERROR"


@dataclass
class ExtractedItem:
    """A granular evidence record with full provenance tracking.

    Represents a specific observation (row, event, log entry, document section)
    extracted from raw evidence files without altering the source data.
    """

    source_path: str
    evidence_id: str
    source_type: SourceType
    locator: str  # e.g., 'table:users/row:3', 'line:42', 'page:1'
    content: str  # Extracted text content for searching
    system_id: Optional[str] = None
    resource_id: Optional[str] = None
    timestamp: Optional[str] = None
    identifiers: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    status: ExtractionStatus = ExtractionStatus.PARSED
    error_message: Optional[str] = None


@dataclass
class SearchResult:
    """Search query match linked to authentic evidence provenance."""

    index_id: int
    evidence_id: str
    source_path: str
    source_type: SourceType
    locator: str
    system_id: Optional[str]
    resource_id: Optional[str]
    timestamp: Optional[str]
    snippet: str
    identifiers: List[str]
    metadata: Dict[str, Any]
    rank: float = 0.0
