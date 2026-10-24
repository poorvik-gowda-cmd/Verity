"""Full-text and structured evidence indexing engine using SQLite FTS5."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import (
    ExtractionStatus,
    ExtractedItem,
    SearchResult,
    SourceType,
)


def sanitize_fts_query(query: str) -> str:
    """Sanitize user query for SQLite FTS5 syntax, wrapping tokens with quotes."""
    cleaned = query.strip()
    if not cleaned:
        return '""'
    # If the user query has special punctuation (like @, -, .), wrap terms in quotes
    # Replace quotes inside the query
    escaped = cleaned.replace('"', '""')
    # If it's a single email or token, wrap in quotes
    return f'"{escaped}"'


class EvidenceIndex:
    """Lightweight SQLite-backed searchable evidence index with full-text search."""

    def __init__(self, db_path: Optional[Path | str] = None):
        """Initialize index in-memory or at specified file path."""
        self.db_path = str(db_path) if db_path else ":memory:"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        """Create structured items table and FTS5 search index."""
        cursor = self.conn.cursor()

        # Master items table with uniqueness constraint to prevent duplicate indexing
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_items (
                index_id INTEGER PRIMARY KEY AUTOINCREMENT,
                evidence_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_type TEXT NOT NULL,
                locator TEXT NOT NULL,
                content TEXT NOT NULL,
                system_id TEXT,
                resource_id TEXT,
                timestamp TEXT,
                identifiers TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                UNIQUE(evidence_id, source_path, locator)
            );
            """
        )

        # FTS5 virtual table for high-performance substring and token matching
        cursor.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(
                content,
                system_id,
                resource_id,
                identifiers,
                evidence_id,
                content='evidence_items',
                content_rowid='index_id'
            );
            """
        )

        # Triggers to keep FTS index synchronized with items table
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON evidence_items BEGIN
                INSERT INTO evidence_fts(rowid, content, system_id, resource_id, identifiers, evidence_id)
                VALUES (new.index_id, new.content, new.system_id, new.resource_id, new.identifiers, new.evidence_id);
            END;
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON evidence_items BEGIN
                INSERT INTO evidence_fts(evidence_fts, rowid, content, system_id, resource_id, identifiers, evidence_id)
                VALUES('delete', old.index_id, old.content, old.system_id, old.resource_id, old.identifiers, old.evidence_id);
            END;
            """
        )

        self.conn.commit()

    def add_item(self, item: ExtractedItem) -> Optional[int]:
        """Insert single extracted item idempotently, returning index_id or None if duplicate."""
        cursor = self.conn.cursor()
        identifiers_json = json.dumps(item.identifiers)
        metadata_json = json.dumps(item.metadata)

        try:
            cursor.execute(
                """
                INSERT OR IGNORE INTO evidence_items (
                    evidence_id, source_path, source_type, locator, content,
                    system_id, resource_id, timestamp, identifiers, metadata_json,
                    status, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.evidence_id,
                    item.source_path,
                    item.source_type.value,
                    item.locator,
                    item.content,
                    item.system_id,
                    item.resource_id,
                    item.timestamp,
                    identifiers_json,
                    metadata_json,
                    item.status.value,
                    item.error_message,
                ),
            )
            self.conn.commit()
            if cursor.rowcount > 0:
                return cursor.lastrowid
            return None
        except Exception as e:
            self.conn.rollback()
            raise e

    def add_items(self, items: Iterable[ExtractedItem]) -> int:
        """Batch insert multiple items within a single transaction."""
        cursor = self.conn.cursor()
        inserted_count = 0

        for item in items:
            identifiers_json = json.dumps(item.identifiers)
            metadata_json = json.dumps(item.metadata)
            cursor.execute(
                """
                INSERT OR IGNORE INTO evidence_items (
                    evidence_id, source_path, source_type, locator, content,
                    system_id, resource_id, timestamp, identifiers, metadata_json,
                    status, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.evidence_id,
                    item.source_path,
                    item.source_type.value,
                    item.locator,
                    item.content,
                    item.system_id,
                    item.resource_id,
                    item.timestamp,
                    identifiers_json,
                    metadata_json,
                    item.status.value,
                    item.error_message,
                ),
            )
            if cursor.rowcount > 0:
                inserted_count += 1

        self.conn.commit()
        return inserted_count

    def search(
        self,
        query: str,
        system_id: Optional[str] = None,
        resource_id: Optional[str] = None,
        identifier: Optional[str] = None,
        limit: int = 50,
    ) -> List[SearchResult]:
        """Search evidence items using FTS5 with optional structured filters."""
        cursor = self.conn.cursor()

        # Build query
        fts_query = sanitize_fts_query(query) if query else None

        sql = """
            SELECT
                e.index_id,
                e.evidence_id,
                e.source_path,
                e.source_type,
                e.locator,
                e.system_id,
                e.resource_id,
                e.timestamp,
                e.content,
                e.identifiers,
                e.metadata_json,
                e.status,
                fts.rank
            FROM evidence_items e
            JOIN evidence_fts fts ON e.index_id = fts.rowid
            WHERE 1=1
        """
        params: List[Any] = []

        if fts_query:
            sql += " AND evidence_fts MATCH ?"
            params.append(fts_query)

        if system_id:
            sql += " AND e.system_id = ?"
            params.append(system_id)

        if resource_id:
            sql += " AND e.resource_id = ?"
            params.append(resource_id)

        if identifier:
            sql += " AND e.identifiers LIKE ?"
            params.append(f"%{identifier}%")

        sql += " ORDER BY fts.rank LIMIT ?"
        params.append(limit)

        try:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            # Fallback to direct LIKE search if FTS syntax error
            sql_fallback = """
                SELECT
                    index_id, evidence_id, source_path, source_type, locator,
                    system_id, resource_id, timestamp, content, identifiers,
                    metadata_json, status, 0.0 as rank
                FROM evidence_items
                WHERE content LIKE ?
            """
            fallback_params = [f"%{query}%"]
            if system_id:
                sql_fallback += " AND system_id = ?"
                fallback_params.append(system_id)
            if resource_id:
                sql_fallback += " AND resource_id = ?"
                fallback_params.append(resource_id)
            if identifier:
                sql_fallback += " AND identifiers LIKE ?"
                fallback_params.append(f"%{identifier}%")
            sql_fallback += " LIMIT ?"
            fallback_params.append(limit)

            cursor.execute(sql_fallback, fallback_params)
            rows = cursor.fetchall()

        results: List[SearchResult] = []
        for r in rows:
            # Generate snippet from content
            content = r["content"]
            snippet = content[:200] + "..." if len(content) > 200 else content

            identifiers = json.loads(r["identifiers"])
            metadata = json.loads(r["metadata_json"])

            results.append(
                SearchResult(
                    index_id=r["index_id"],
                    evidence_id=r["evidence_id"],
                    source_path=r["source_path"],
                    source_type=SourceType(r["source_type"]),
                    locator=r["locator"],
                    system_id=r["system_id"],
                    resource_id=r["resource_id"],
                    timestamp=r["timestamp"],
                    snippet=snippet,
                    identifiers=identifiers,
                    metadata=metadata,
                    rank=float(r["rank"]) if r["rank"] is not None else 0.0,
                )
            )

        return results

    def get_coverage_summary(self) -> Dict[str, Any]:
        """Compute comprehensive ingestion coverage report."""
        cursor = self.conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM evidence_items;")
        total_items = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(DISTINCT evidence_id) FROM evidence_items;")
        distinct_evidence = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(DISTINCT source_path) FROM evidence_items;")
        distinct_files = cursor.fetchone()[0]

        cursor.execute("SELECT source_type, COUNT(*) FROM evidence_items GROUP BY source_type;")
        by_source_type = {row[0]: row[1] for row in cursor.fetchall()}

        cursor.execute("SELECT status, COUNT(*) FROM evidence_items GROUP BY status;")
        by_status = {row[0]: row[1] for row in cursor.fetchall()}

        cursor.execute(
            """
            SELECT system_id, COUNT(*) FROM evidence_items
            WHERE system_id IS NOT NULL GROUP BY system_id ORDER BY COUNT(*) DESC LIMIT 10;
            """
        )
        top_systems = {row[0]: row[1] for row in cursor.fetchall()}

        cursor.execute(
            """
            SELECT source_path, error_message FROM evidence_items
            WHERE status != 'PARSED';
            """
        )
        unsupported_or_errors = [
            {"path": row[0], "message": row[1]} for row in cursor.fetchall()
        ]

        return {
            "total_indexed_items": total_items,
            "distinct_evidence_ids": distinct_evidence,
            "distinct_files_indexed": distinct_files,
            "breakdown_by_source_type": by_source_type,
            "breakdown_by_status": by_status,
            "top_systems": top_systems,
            "unsupported_or_errors_count": len(unsupported_or_errors),
            "unsupported_or_errors_sample": unsupported_or_errors[:10],
        }

    def close(self) -> None:
        """Close SQLite database connection."""
        self.conn.close()
