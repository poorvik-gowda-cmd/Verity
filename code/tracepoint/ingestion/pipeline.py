"""End-to-end ingestion pipeline orchestrating file discovery, parsing, and indexing."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

try:
    from config import config
except ImportError:
    from code.config import config

from .indexer import EvidenceIndex
from .models import (
    ExtractionStatus,
    ExtractedItem,
    SourceType,
)
from .parsers import (
    parse_image_file,
    parse_json_file,
    parse_log_file,
    parse_pdf_file,
    parse_sql_file,
    parse_txt_file,
)


class IngestionPipeline:
    """Manages file discovery, format dispatching, and evidence indexing."""

    def __init__(
        self,
        dataset_dir: Optional[Path] = None,
        index: Optional[EvidenceIndex] = None,
    ):
        self.dataset_dir = dataset_dir or config.dataset_dir
        self.index = index or EvidenceIndex()
        self.manifest_map: Dict[str, Dict[str, str]] = {}
        self._load_manifest()

    def _load_manifest(self) -> None:
        """Load evidence_files.csv manifest into lookup map."""
        manifest_path = self.dataset_dir / "evidence_files.csv"
        if manifest_path.exists():
            df = pd.read_csv(manifest_path)
            for _, row in df.iterrows():
                norm_path = str(row["file_path"]).replace("\\", "/")
                self.manifest_map[norm_path] = {
                    "file_id": str(row["file_id"]),
                    "origin": str(row["origin"]),
                    "captured_at": str(row["captured_at"]),
                }

    def discover_files(self) -> List[Tuple[Path, str, Optional[str]]]:
        """Discover all supported evidence files, preserving manifest IDs and capture timestamps."""
        evidence_dir = self.dataset_dir / "evidence"
        discovered: List[Tuple[Path, str, Optional[str]]] = []

        if not evidence_dir.exists():
            return discovered

        for file_path in sorted(evidence_dir.rglob("*")):
            if file_path.is_file():
                rel_path = file_path.relative_to(self.dataset_dir).as_posix()
                manifest_meta = self.manifest_map.get(rel_path, {})
                evidence_id = manifest_meta.get("file_id", f"file_{file_path.stem}")
                captured_at = manifest_meta.get("captured_at")
                discovered.append((file_path, evidence_id, captured_at))

        return discovered

    def ingest_file(
        self, file_path: Path, evidence_id: str, default_captured_at: Optional[str] = None
    ) -> List[ExtractedItem]:
        """Dispatch a single file to its appropriate parser."""
        ext = file_path.suffix.lower()

        if ext == ".sql":
            return list(parse_sql_file(file_path, evidence_id, default_captured_at))
        elif ext == ".json":
            return list(parse_json_file(file_path, evidence_id, default_captured_at))
        elif ext == ".log":
            return list(parse_log_file(file_path, evidence_id, default_captured_at))
        elif ext == ".txt":
            return list(parse_txt_file(file_path, evidence_id, default_captured_at))
        elif ext == ".pdf":
            return list(parse_pdf_file(file_path, evidence_id, default_captured_at))
        elif ext in [".png", ".jpg", ".jpeg"]:
            return list(parse_image_file(file_path, evidence_id, default_captured_at))
        else:
            return [
                ExtractedItem(
                    source_path=str(file_path),
                    evidence_id=evidence_id,
                    source_type=SourceType.UNKNOWN,
                    locator="file",
                    content="",
                    timestamp=default_captured_at,
                    status=ExtractionStatus.UNSUPPORTED,
                    error_message=f"Unsupported file format: {ext}",
                )
            ]

    def run(self) -> Dict[str, Any]:
        """Execute full ingestion across all discovered files and populate index."""
        start_time = time.time()
        files = self.discover_files()

        total_files = len(files)
        parsed_files = 0
        unsupported_files = 0
        failed_files = 0
        total_items_indexed = 0

        for file_path, evidence_id, captured_at in files:
            items = self.ingest_file(file_path, evidence_id, captured_at)

            # Categorize file status
            has_error = any(it.status == ExtractionStatus.ERROR for it in items)
            has_unsupported = any(it.status == ExtractionStatus.UNSUPPORTED for it in items)
            has_parsed = any(it.status in (ExtractionStatus.PARSED, ExtractionStatus.PARTIAL) for it in items)

            if has_error and not has_parsed:
                failed_files += 1
            elif has_unsupported and not has_parsed:
                unsupported_files += 1
            else:
                parsed_files += 1

            # Index extracted items
            indexed_count = self.index.add_items(items)
            total_items_indexed += indexed_count

        elapsed = time.time() - start_time
        coverage = self.index.get_coverage_summary()

        return {
            "status": "COMPLETED",
            "elapsed_seconds": round(elapsed, 2),
            "files_discovered": total_files,
            "files_parsed_successfully": parsed_files,
            "files_unsupported": unsupported_files,
            "files_failed": failed_files,
            "total_items_indexed": total_items_indexed,
            "coverage_summary": coverage,
        }
