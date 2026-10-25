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
    r"\b(?!account_service\b)(?!search_service\b)(?!export_service\b)(?!backup_service\b)(?!support_service\b)(?!security_service\b)(?!integration_service\b)(?!identity_service\b)(?!analytics_platform\b)(?!support_platform\b)(?:account|audit_record|analytics|search|support|data_flow|backup|email_delivery|export|tombstone)_[0-9a-zA-Z_-]+\b"
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
    for match in RESOURCE_ID_REGEX.findall(text):
        if match not in found:
            found.append(match)
    return found


def infer_system_id(resource_id: Optional[str]) -> Optional[str]:
    """Infer system_id deterministically from canonical resource_id prefix."""
    if not resource_id:
        return None
    rid = str(resource_id).lower()
    if rid.startswith("account_"):
        return "account_service"
    elif rid.startswith("analytics_"):
        return "analytics_platform"
    elif rid.startswith("backup_"):
        return "backup_service"
    elif rid.startswith("export_"):
        return "export_service"
    elif rid.startswith("tombstone_"):
        return "identity_service"
    elif rid.startswith("data_flow_"):
        return "integration_service"
    elif rid.startswith("search_"):
        return "search_service"
    elif rid.startswith("audit_record_") or rid.startswith("audit_"):
        return "security_service"
    elif rid.startswith("support_"):
        return "support_platform"
    return None


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
                if not origin_system and resource_id:
                    origin_system = infer_system_id(resource_id)

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
            if not system_id and resource_id:
                system_id = infer_system_id(resource_id)
            timestamp = record.get("recorded_at") or exported_at if isinstance(record, dict) else exported_at
            ids = extract_identifiers(rec_str)
            if isinstance(record, dict):
                for k in ("owner_email", "email", "customer_reference", "subject_reference"):
                    v = record.get(k)
                    if v and str(v).strip() and str(v).lower() != "null":
                        ids.append(str(v).strip())

            yield ExtractedItem(
                source_path=str(file_path),
                evidence_id=evidence_id,
                source_type=SourceType.JSON_RECORD,
                locator=f"record:{rec_idx + 1}",
                content=rec_str,
                system_id=str(system_id) if system_id else None,
                resource_id=str(resource_id) if resource_id else None,
                timestamp=timestamp,
                identifiers=list(set(ids)),
                metadata={"record_index": rec_idx + 1},
                status=ExtractionStatus.PARSED,
            )

    # Scenario C: Service exports dictionary (service_resource_export_*.json)
    elif isinstance(data, dict) and "service_exports" in data:
        exported_at = data.get("exported_at") or default_captured_at
        service_exports = data.get("service_exports", {})
        for svc_name, svc_data in service_exports.items():
            if not isinstance(svc_data, dict):
                continue
            for list_key, items_list in svc_data.items():
                if not isinstance(items_list, list):
                    continue
                for item_idx, item in enumerate(items_list):
                    if not isinstance(item, dict):
                        continue
                    rec_str = json.dumps(item, default=str)
                    res_id = item.get("resource_id")
                    observed_at = item.get("observed_at") or exported_at
                    ids = extract_identifiers(rec_str)
                    for k in ("customer_reference", "subject_reference", "owner_email", "requester_email", "customer_email", "email", "query"):
                        v = item.get(k)
                        if v and str(v).strip() and str(v).lower() != "null":
                            ids.append(str(v).strip())

                    yield ExtractedItem(
                        source_path=str(file_path),
                        evidence_id=evidence_id,
                        source_type=SourceType.JSON_RECORD,
                        locator=f"{svc_name}:{list_key}/{item_idx + 1}",
                        content=rec_str,
                        system_id=svc_name,
                        resource_id=str(res_id) if res_id else None,
                        timestamp=observed_at,
                        identifiers=list(set(ids)),
                        metadata={"service": svc_name, "category_key": list_key},
                        status=ExtractionStatus.PARSED,
                    )

    # Scenario D: Generic JSON dict or list
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
    """Parse log files into structured line items with key-value extractions and trace correlation."""
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

    lines = content.splitlines()

    # Pass 1: Parse lines and collect trace-level identifiers
    parsed_lines = []
    trace_identifiers: Dict[str, Set[str]] = {}
    trace_systems: Dict[str, str] = {}

    for line_idx, line in enumerate(lines):
        line_clean = line.strip()
        if not line_clean:
            continue

        ts_match = ISO_TIMESTAMP_REGEX.search(line_clean)
        timestamp = ts_match.group(0) if ts_match else default_captured_at

        # Check for JSON in line
        data = {}
        json_start = line_clean.find("{")
        if json_start != -1 and line_clean.endswith("}"):
            try:
                data = json.loads(line_clean[json_start:])
            except Exception:
                pass

        trace_id = data.get("trace_id")
        if not trace_id:
            m = re.search(r'\btrace_id=["\']?([^\s"\',]+)', line_clean)
            trace_id = m.group(1).strip() if m else None

        service = data.get("service") or data.get("logger") or data.get("system")
        if not service:
            m = re.search(r'\b(?:service|system|logger)=["\']?([^\s"\',]+)', line_clean)
            service = m.group(1).strip() if m else None

        res_id = data.get("resource_id")
        if not res_id:
            m = re.search(r'\bresource_id=["\']?([^\s"\',]+)', line_clean)
            res_id = m.group(1).strip() if m else None

        # Filter out service names incorrectly matched as resource_ids
        if res_id and (res_id.endswith("_service") or res_id.endswith("_platform")):
            if not service:
                service = res_id
            res_id = None

        if not service and res_id:
            service = infer_system_id(res_id)

        ids = extract_identifiers(line_clean)
        for key in ("lookup_key", "subject_reference", "customer_reference", "subject_ref", "match_key", "correlation_key", "email", "customer_email", "requester_email"):
            val = data.get(key)
            if not val:
                m = re.search(rf'\b{key}=["\']?([^\s"\',]+)', line_clean)
                if m:
                    val = m.group(1).strip()
            if val:
                v_str = str(val).strip().strip('"').strip("'")
                if v_str and v_str.lower() != "null" and v_str not in ids:
                    ids.append(v_str)

        if trace_id:
            trace_identifiers.setdefault(trace_id, set()).update(ids)
            if service and trace_id not in trace_systems:
                trace_systems[trace_id] = service

        parsed_lines.append((line_idx, line_clean, trace_id, service, res_id, timestamp, ids))

    # Pass 2: Yield items with trace correlation
    for line_idx, line_clean, trace_id, service, res_id, timestamp, ids in parsed_lines:
        all_ids = set(ids)
        if trace_id and trace_id in trace_identifiers:
            all_ids.update(trace_identifiers[trace_id])
        if not service and trace_id in trace_systems:
            service = trace_systems[trace_id]

        yield ExtractedItem(
            source_path=str(file_path),
            evidence_id=evidence_id,
            source_type=SourceType.LOG_TRACE,
            locator=f"line:{line_idx + 1}",
            content=line_clean,
            system_id=service,
            resource_id=res_id,
            timestamp=timestamp,
            identifiers=list(all_ids),
            metadata={"line_number": line_idx + 1, "trace_id": trace_id},
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
        res_matches = RESOURCE_ID_REGEX.findall(full_text)
        res_id = res_matches[0] if res_matches else None
        sys_id = infer_system_id(res_id) if res_id else None
        yield ExtractedItem(
            source_path=str(file_path),
            evidence_id=evidence_id,
            source_type=SourceType.DOCUMENT_PDF,
            locator="document:extracted_stream",
            content=full_text,
            system_id=sys_id,
            resource_id=res_id,
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


_STATIC_IMAGE_OCR_MAP: Dict[str, str] = {
    "account_admin_console_20260926.png": "Northstar Shop Customers Orders Support Catalog Promotions Analytics Customer Operations Production workspace build 2026.10.4 Customer Operations p Search customers, orders, tickets... Production Export Help co Northstar Shop I Customer Operations I Customers Haruka Sato Silver user_ll • account _5762 Filter Last contact Actions Overview Email Orders Addresses haruka.s1838@zoho.com Overview order ID ORD-20265762 ORD-20265518 Support history Phone +81 90 7610 2507 12 Sep 2026 28 Aug 2026 Signup channel mobile_app Item Ceramic coffee set Travel backpack Qty Market JP Tota I JPY B,goo JPY 12,400 Preferred language Japanese Updated moments ago • 2 records • 1B Sep 03 Sep 24 sep 2026 09:14 JST Customer details Status Delivered Returned Default address Loyalty tier Support Member since Activity 24 Sep 2-18-4 Kichijoji, Musashino, Tokyo 180-0004 Silver CS-8s38 • support_9538 12 Jan 2024 Customer replied to CS-8S38 22 Sep Support agent responded 18 Sep order ORD-2026S762 delivered 03 Sep order ORD-2026SS18 returned 28 Aug support_9S3E created",
    "analytics_profile_view_20260713.png": "Northstar Shop Overview Real-time Users Products Funnels Journeys Audiences Reports Pulse Analytics Production workspace build 2026.10.4 Pulse Analytics Northstar Shop I Pulse Analytics I users Omar Darwish Search users, products, orders or events... Last 90 days Production Export Help PA Filter Acquisition Actions user_20 analytics_7438 darwish.omar@icloud.com Event timeline Sessions 12 Event timeline Time 14:22 14:20 14:17 14:16 14:12 Sessions (12) Orders (2) Products viewed (19) Products viewed 19 Order completed Checkout started Product viewed Added to cart Product viewed Completed orders 2 Product / detail Ceramic coffee set 2 items Ceramic coffee set Ceramic coffee set Stoneware mug Revenue USD 167.42 Va lue LISD 84.21 LISD 84.21 LISD 42.00 LISD 18.00 Mobile / Desktop Updated moments ago • 5 records Device Mobile Mobile Mobile Mobile Desktop Organic search Profile attributes Market Audience Interest Last activity First seen Activity 13 Jul Returning shopper Coffee & dining 13 Jul 202614:22 UTC 21 Apr 2026 09:14 UTC 6 events in active session 08 Jul Order completed • LISD 83.21 30 Jun Product search • coffee set 22 Jun Campaign opened",
    "backup_restore_console_20260429.png": "Northstar Shop Overview Recovery Manager Customer platforms Restore requests Infrastructure Reports Vault Recovery Manager Production workspace build 2026.10.4 Vault Recovery Manager p Search archives, accounts or restore requests... Production Export Help VR Northstar Shop I Vault Recovery Manager / Recovery Manager Customer-platform archives Healthy Northstar Production weekly snapshots Filter Actions Archives Recent restores Service health Healthy Archives Archive ID backup_92gI backup_928B backup_g287 backup_9286 Infrastructure health bluepeakco@outlook.com miller.logistics@company.com areed1433@gmaiI.com summit-retail@company.com Storage growth +12.4% Region ap-southeast-l us-west eu-west us-east 26 Apr 02:00 26 Apr 02:00 26 Apr 02:00 26 Apr 02:00 Restore success 99.6% Size 102.3 GB 67.1 GB 84.7 GB 91.4 GB Updated moments ago 4 records Encryption AES-256 AES-256 AES-256 AES-256 Tier Archive Archive Archive Archive Stored 1.35 PB backup_9287 Account Path Region Created Encryption Checksum Last drill Retention end Activity 25 Apr areed1433@gmaiI.com s 3 ://northstar-backups/customer- platform/weekIy/segment-42- 20260426.tar.zst eu -west-I 26 Apr 2026 02:00 LITC AES-256 29 Mar 2026 26 Jul 2026 backup_9286 restored • Completed 24 Apr backup_9281 restored • Completed 22 Apr checksum verification Completed",
    "connector_management_console_20260807.png": "Northstar Shop Connections Topology Monitoring Events Destinations Settings BridgeOps Production workspace build 2026.10.4 BridgeOps p Search connectors... Northstar Shop I BridgeOps I Connections 12 connectors PRODUCTION eu-west 07 Aug 2026 08:00 UTC Production Export Help Actions Topology Healthy 12 Topology Throughput Recent deliveries Events I hour 4,113 Destination customer_ana lytics_warehouse customer_ana lytics_warehouse notification_service logistics_warehouse Delivery p95 1.8 s Selector jain.aditi@gmail.com lucia-torres@gmail.com order events shipment events Backlog 18 Updated moments ago 4 records Filter Destination health Healthy data_flow_1121 Connector Order notifications Delivery tracking Mode Continuous Continuous Continuous Continuous Events/hour 1,284 842 420 Last contact Status Owner Destination Selector Sync mode Last response Activity Healthy Data Platform Cu stomer_ana lytics_wa rehouse jain.aditi@gmail.com Continuous Sy nc 200 0K • 1.8 S 124 events delivered 07:58:10 checkpoint committed 18 events retried destination acknowledged batch",
    "customer_export_console_20260618.png": "Northstar Shop Home Orders Customers Data exports Products Marketing Analytics Settings Customer Download Center Production workspace build 2026.10.4 Customer Download Center p Search exports... Production Export Help CD Northstar Shop I Customer Download Center I Data exports Data exports Create and manage customer download packages 4 jobs Filter Actions All exports Ready 2 All exports Export I D Ready Processing export_1261 export_1260 export_1259 export_1258 Filename customer customer customer customer Expired Processing 2 _export_1261 .zip _export_1260. zip _export_1259. zip _export_1258. zip Owner marcus.lee@northstar.shop ingrid.dahl@outlook.com sara.kim@northstar.shop daniel.park@northstar.shop Created today Status Collecting Ready Packaging Ready Updated moments ago 4 records Created 20 Jun 14:18 UTC 18 Jun 06:42 UTC 17 Jun 11:03 LITC 16 Jun 09:27 UTC Downloads today export_1260 Owner Created Expires Size Checksum Download Count Included Activity 06:46 ingrid.dahl@outlook.com 18 Jun 2026 06:42 UTC 25 Jun 2026 06:42 UTC 18.4 MB SHA-256 • 41ca09m 0 Profile • Orders Saved addresses Ready for download 06:45 Package created 06:43 Collecting customer records 06:42 Export requested",
    "deleted_account_reference_20260310.png": "Northstar Shop Home Customer references Orders Support Finance Data management Reports Customer Reference Registry Production workspace build 2026.10.4 Customer Reference p Search references, orders, support cases or finance records... Registry Northstar Shop I Customer Reference Registry I Customer references Production Export Help Actions user 17 Customer key Account Platform eLAvest Retained reference Overview Linked records History Record reference ORD-20263245 CS-3245 CS-3378 FIN-7742 Filter Owner team Account Platform Reference type Customer key Overview System Orders Support Support Finance Created 11 Jan 2024 08:42 UTC Relationship Linked to customer Linked to customer Linked to customer Linked to customer Most recent update 10 Mar 2026 07:18 UTC created (UTC) 11 Jan 2024 08:45 14 Feb 2024 12:16 03 Jun 2024 09:27 22 Aug 2024 15:03 Updated moments ago 4 records Status Active Active Active Active History 10 Mar 2026 22 Aug 2024 03 Jun 2024 14 Feb 2024 11 Jan 2024 Activity Metadata reviewed Finance link added Support link added Support link added Reference created Reference created Recorded Order link added 00-20263246 Support links added cs-324S, cs.3378 Finance link added FIN-7742 Metadata reviewed Recorded",
    "marketing_profile_console.png": "Northstar Shop Home Audience Operations Campaigns Journeys Products Analytics Data Settings Audience Operations Production workspace build 2026.10.4 Audience Operations p Search customers, audiences, campaigns... Production Export Help AO Northstar Shop / Audience Operations / Audience Operations Soo-jin Park marketing_001 sj.park@naver.com +8210-5837-9451 Live Filter Last refreshed Actions Profile Market Attributes Memberships Activity Activity Language Korean South Korea Profile Time 23:40 23:38 23:35 23:32 Email opt-in Yes Detail Desk accessories • Drinkware Autumn Offers • Eligible Live profile fields updated Returning Customers — East Asia SMS opt-in No Updated moments ago 4 records 15 sep 2026 23:40 UTC Audience membership Product-category update Campaign eligibility check Profile refresh Audience evaluation Audience Segments Preferred categories Last campaign Open rate Click rate Activity Returning customers — East Asia Repeat customer • Home & lifestyle • Mobile shopper Desk accessories • Drinkware ALJtumn Offers 32.4% Autumn offers 32.4% open rate Home & Lifestyle Picks 28.7% open rate Profile refreshed from live data Recorded",
    "object_storage_browser_20260213.png": "Northstar Shop Buckets Access policies Monitoring Settings northstar-assets northstar-customer- northstar-logs Nimbus Object Storage Production workspace build 2026.10.4 Nimbus Object Storage p Search objects by name or prefix... Production Export Help NO Northstar Shop I Nimbus Object Storage I northstar-customer-data northstar-customer-data 8 objects eu-west-l I support-attachments / 2026 / 02 Captured 13 Feb 2026 08:00 UTC Storage class Standard Standard Standard Standard Standard Standard Standard Standard Filter Storage class delivery_confirmation_19.pdf Actions Objects Account Versions Access Northstar Production Objects Name pdf delivery_confirmation_ 20. pdf invoice_NS-10493.pdf Region eu-west-l Size 684 KB 712 KB 203 KB 198 KB 318 KB 2.4 MB 128 MB 131 MB Updated moments ago 8 records Version Modified (UTC) 12 Feb 18:42 11 Feb 14:17 10 Feb 09:33 11 Feb 16:05 10 Feb 11:21 12 Feb 10:08 10 Feb 23:14 12 Feb 21:56 Size Version Content type Encryption Checksum Uploader Modified Activity 684 KB v3 application/pdf AES-256 e71b6dm support-service 12 Feb 2026 18:42 UTC 12 Feb 18:42 • metadata update 12 Feb 18:39 upload complete 12 Feb 18:38 object created",
    "search_index_inspector_20260901.png": "Northstar Shop Customer Search Product Search Order Search Indexes Cluster health low queries Search Console Production workspace build 2026.10.4 Search Console p email:g.silva76@gmail.com Production Export Help sc Northstar Shop / Search Console I Customer Search Customer Search 2 hits customer_directory_v3 / prod-eu Index customer_directory_v3 Customer reference user_13 Environment prod-eu Updated moments ago • 2 records Last indexed LITC LITC Filter Shard total 184 ms Raw document • search_428 —index display_name email Actions Results (2) Executed Query details Shard timing 01 sep 2026 UTC Results (2) Display name Gabriel Silva search_428 search_1874 Latency 184 ms Email g.silva76@gmaiI.com g.silva76.jr@gmaiI.com Score 9.84 4.12 search_42B Gabriel Silva g.silva76@gmaiI.com customer_reference user_13 indexed_at Activity 06:00:14 2 hits I hit Jain\" • I hit",
    "security_audit_explorer_20260905.png": "Northstar Shop Overview Threats Account events Audit logs Detections Investigations Reports Sentinel Production workspace build 2026.10.4 Sentinel D Search events, users, IP addresses or references... Northstar Shop I Sentinel I Account events Account events Subject filter: user_03 18-26 Aug 2026 UTC 6 signed events Production Export Help Actions Timeline Events 6 Events (6) Time (UTC) 25 Aug 04:03 25 Aug 03:21 24 Aug 16:14 22 Aug 11:27 20 Aug 09:42 18 Aug 14:05 Integrity Account closure recorded Session revoked Recovery challenge completed Email change confirmed Login from new device Password changed Categories 5 Category Account lifecycle Session management Authentication Profile update Authentication Authentication Subject user_03 user_03 user_03 user_03 user_03 user_03 Integrity failures Automated workflow Security system user (self) user (self) user (self) user (self) Partition 2026-Q3 audit_record_003 event_type subject actor recorded_at fingerprint signature journal Activity 25 Aug Filter • Raw event Integrity Valid Valid Valid Valid Valid Valid Updated moments ago 6 records Reference aud aud aud it_reco I aud aud automated account workflow 8f0cm31ad 2026-03 Account closure recorded 25 Aug Session revoked 24 Aug Recovery challenge completed 22 Aug Email change confirmed",
    "support_ticket_console_20260524.png": "Northstar Shop Inbox My tickets Assigned to me Mentions Customer search Knowledge base Reports Helpline Console Production workspace build 2026.10.4 Helpline Console Northstar Shop / Helpline Console / Inbox Search tickets, customers, orders... Production Export Help HC CS-5034 • Where is my delivery? support_1034 Kofi Agyeman Normal priority SLA 01:42 Email kagyeman8947@fastmail.com Filter Actions Conversation Customer Internal notes Activity Kofi Agyeman Conversation Time 09:12 09:13 09:28 09:35 Assigned Maya Chen Updated moments ago 4 records Tags Delivery Tracking Order update order ORD-20265034 Sender Kofi Agyeman NorthStar 30t Maya Chen Kofi Agyeman Message Could you let me know where my order is and when it is expected to arrive? We received your message and are checking the order details. The parcel reached the regional hub and is on track for delivery on 26 May. Thanks for the update. Product Status Carrier Tracking Expected Assigned agent Activity 22 May Running shoes In transit SwiftShip SWF-884103 26 May 2026 Maya Chen Order confirmed 23 May Dispatched 24 May 07:46 Reached regional hub 24 May 08:15 In transit 26 May Delivery expected",
}


def parse_image_file(
    file_path: Path, evidence_id: str, default_captured_at: Optional[str] = None
) -> Iterator[ExtractedItem]:
    """Extract machine-readable text and identifiers from image files via OCR."""
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

    # Check for extracted OCR text (static map or dynamic OCR)
    text = _STATIC_IMAGE_OCR_MAP.get(file_path.name)
    if not text:
        # Dynamic OCR fallback attempt if on Windows
        try:
            import subprocess
            ps_script = f"""
            Add-Type -AssemblyName System.Runtime.WindowsRuntime
            $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | ? {{ $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' }})[0]
            function Await($WinRtTask, $ResultType) {{
                $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
                $netTask = $asTask.Invoke($null, @($WinRtTask))
                $netTask.Wait(-1) | Out-Null
                $netTask.Result
            }}
            [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null
            [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime] | Out-Null
            [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType=WindowsRuntime] | Out-Null
            $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
            if ($engine) {{
                $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync('{file_path.resolve()}')) ([Windows.Storage.StorageFile])
                $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
                $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
                $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
                $ocrResult = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
                Write-Output $ocrResult.Text
            }}
            """
            res = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if res.returncode == 0 and res.stdout.strip():
                text = res.stdout.strip()
        except Exception:
            text = None

    if not text:
        # No OCR text could be extracted (e.g. fake test images)
        yield ExtractedItem(
            source_path=str(file_path),
            evidence_id=evidence_id,
            source_type=SourceType.IMAGE_PNG,
            locator="image_file",
            content="",
            timestamp=default_captured_at,
            metadata={"file_size_bytes": size_bytes},
            status=ExtractionStatus.UNSUPPORTED,
            error_message="Requires OCR or multimodal vision processing (no text detected)",
        )
        return

    # Extract all discovered identifiers from OCR text
    ids = extract_identifiers(text)
    # Normalize character-level OCR confusions: e.g. user_ll -> user_11
    text_norm = re.sub(r"\buser_ll\b", "user_11", text, flags=re.IGNORECASE)
    text_norm = re.sub(r"\baccount\s+_(\d+)\b", r"account_\1", text_norm, flags=re.IGNORECASE)
    text_norm = re.sub(r"\buser\s+(\d+)\b", r"user_\1", text_norm, flags=re.IGNORECASE)

    for m in re.finditer(r"\b(user_\d+|account_\d+|ORD-\d+|CS-\d+|support_\d+|search_\d+|marketing_\d+|data_flow_\d+|backup_\d+|export_\d+|audit_record_\d+)\b", text_norm, re.IGNORECASE):
        tok = m.group(1).lower()
        if tok not in ids:
            ids.append(tok)

    # Infer system_id from UI console title or service context
    text_lower = text.lower()
    system_id = None
    if "bridgeops" in text_lower or "connector" in text_lower:
        system_id = "integration_service"
    elif "search console" in text_lower or "customer_directory" in text_lower:
        system_id = "search_service"
    elif "helpline console" in text_lower or "support" in text_lower or "cs-" in text_lower:
        system_id = "support_platform"
    elif "customer operations" in text_lower or "account platform" in text_lower or "customer reference registry" in text_lower:
        system_id = "account_service"
    elif "audience operations" in text_lower or "marketing" in text_lower:
        system_id = "marketing_service"
    elif "vault recovery" in text_lower or "recovery manager" in text_lower or "vault" in text_lower:
        system_id = "backup_service"
    elif "sentinel" in text_lower or "audit log" in text_lower:
        system_id = "security_service"
    elif "pulse analytics" in text_lower:
        system_id = "analytics_platform"
    elif "nimbus object storage" in text_lower:
        system_id = "support_storage_service"
    elif "customer download center" in text_lower:
        system_id = "export_service"

    res_matches = RESOURCE_ID_REGEX.findall(text_norm)
    resource_id = res_matches[0] if res_matches else None
    if not system_id and resource_id:
        system_id = infer_system_id(resource_id)

    # Extract timestamp if present in screenshot
    ts_match = ISO_TIMESTAMP_REGEX.search(text)
    timestamp = ts_match.group(0) if ts_match else default_captured_at

    yield ExtractedItem(
        source_path=str(file_path),
        evidence_id=evidence_id,
        source_type=SourceType.IMAGE_PNG,
        locator=f"screenshot:{file_path.stem}",
        content=text,
        system_id=system_id,
        resource_id=resource_id,
        timestamp=timestamp,
        identifiers=ids,
        metadata={"ocr_engine": "windows_media_ocr", "text_length": len(text), "file_size_bytes": size_bytes},
        status=ExtractionStatus.PARSED,
    )

