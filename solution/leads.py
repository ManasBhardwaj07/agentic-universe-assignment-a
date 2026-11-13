"""
Lead ingestion pipeline.
Handles CSV parsing, header mapping, validation in schema order, rejections,
field warnings, PII encryption, and duplicate lead merging.
"""
from __future__ import annotations

import csv
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from solution.crypto import CryptoManager
from solution.database import (
    check_file_status,
    increment_metric,
    record_file_ingestion,
)
from solution.normalization import (
    normalize_header,
    normalize_phone,
    parse_date,
    parse_decimal,
    parse_enum,
)
from solution.schemas import FieldDefinition, SchemaRegistry, WorkflowSchema


def compute_file_hash(path: Path) -> str:
    """Compute SHA-256 hash of file contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class LeadIngestor:
    """Ingests lead CSV files for a workflow into the SQLite database."""

    def __init__(self, conn, crypto: CryptoManager, registry: SchemaRegistry):
        self.conn = conn
        self.crypto = crypto
        self.registry = registry

    def ingest_csv(
        self,
        file_path: Path,
        workflow_id: str,
        drop_name: str,
    ) -> bool:
        """
        Ingest a single lead CSV file.
        Returns True if processed, False if skipped due to idempotency.
        """
        batch_id = file_path.stem  # e.g. "batch_001"
        relative_path = f"{drop_name}/leads/{workflow_id}/{file_path.name}"
        content_hash = compute_file_hash(file_path)

        # Check idempotency
        already_ingested, content_changed = check_file_status(
            self.conn, relative_path, content_hash
        )
        if already_ingested:
            return False
        if content_changed:
            raise ValueError(
                f"File {relative_path} has changed since previous ingestion. Cannot safely re-ingest."
            )

        schema = self.registry.get_workflow(workflow_id)
        if not schema:
            raise ValueError(f"Unknown workflow_id: {workflow_id}")

        with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            try:
                raw_headers = next(reader)
            except StopIteration:
                # Empty file
                record_file_ingestion(
                    self.conn, drop_name, "leads", workflow_id, batch_id, relative_path, content_hash
                )
                return True

            # Map CSV headers to schema fields
            header_map = self._map_headers(raw_headers, schema)

            row_number = 0
            for raw_row in reader:
                # Blank rows (every cell empty or whitespace) are skipped and NOT counted
                if not raw_row or all(cell.strip() == "" for cell in raw_row):
                    continue

                row_number += 1
                increment_metric(self.conn, "lead_rows_total", 1)

                # Extract raw values by field name
                row_dict: Dict[str, str] = {}
                for col_idx, cell_value in enumerate(raw_row):
                    if col_idx in header_map:
                        field_name = header_map[col_idx]
                        row_dict[field_name] = cell_value.strip()

                self._process_row(schema, row_dict, workflow_id, batch_id, row_number, relative_path)

        record_file_ingestion(
            self.conn, drop_name, "leads", workflow_id, batch_id, relative_path, content_hash
        )
        return True

    def _map_headers(self, raw_headers: List[str], schema: WorkflowSchema) -> Dict[int, str]:
        """
        Maps column index to schema field name based on normalized matching of name or aliases.
        """
        mapping: Dict[int, str] = {}
        target_fields: Dict[str, str] = {}

        for f in schema.fields:
            norm_name = normalize_header(f.name)
            target_fields[norm_name] = f.name
            for alias in f.aliases:
                norm_alias = normalize_header(alias)
                target_fields[norm_alias] = f.name

        for idx, h in enumerate(raw_headers):
            norm_h = normalize_header(h)
            if norm_h in target_fields:
                mapping[idx] = target_fields[norm_h]

        return mapping

    def _process_row(
        self,
        schema: WorkflowSchema,
        row_dict: Dict[str, str],
        workflow_id: str,
        batch_id: str,
        row_number: int,
        file_path: str,
    ) -> None:
        """
        Validates row in schema order, records rejections or field warnings,
        and merges/inserts valid leads.
        """
        # Step 1: Rejection validation in schema order (all rows must be valid)
        parsed_values: Dict[str, Optional[str]] = {}
        canonical_phone: Optional[str] = None

        for field_def in schema.fields:
            val = row_dict.get(field_def.name, "")
            is_empty = val == ""

            if field_def.type == "phone":
                if is_empty:
                    if field_def.required:
                        self._reject_row(workflow_id, batch_id, row_number, f"missing_required:{field_def.name}", file_path)
                        return
                    parsed_values[field_def.name] = None
                else:
                    canon, valid = normalize_phone(val)
                    if not valid:
                        # Present but invalid phone
                        self._reject_row(workflow_id, batch_id, row_number, "invalid_phone", file_path)
                        return
                    canonical_phone = canon
                    parsed_values[field_def.name] = canon

            elif field_def.required and is_empty:
                self._reject_row(workflow_id, batch_id, row_number, f"missing_required:{field_def.name}", file_path)
                return

        if not canonical_phone:
            # Defensive check
            self._reject_row(workflow_id, batch_id, row_number, "missing_required:phone", file_path)
            return

        # Step 2: Validate optional fields and record warnings for accepted rows
        for field_def in schema.fields:
            if field_def.type == "phone" or field_def.required:
                continue

            val = row_dict.get(field_def.name, "")
            if val == "":
                parsed_values[field_def.name] = None
                continue

            if field_def.type == "date":
                p_date, valid = parse_date(val)
                if not valid:
                    parsed_values[field_def.name] = None
                    increment_metric(self.conn, "field_warnings", 1)
                else:
                    parsed_values[field_def.name] = p_date

            elif field_def.type == "decimal":
                p_dec, valid = parse_decimal(val)
                if not valid:
                    parsed_values[field_def.name] = None
                    increment_metric(self.conn, "field_warnings", 1)
                else:
                    parsed_values[field_def.name] = p_dec

            elif field_def.type == "enum":
                p_enum, valid = parse_enum(val, field_def.values)
                if not valid:
                    parsed_values[field_def.name] = None
                    increment_metric(self.conn, "field_warnings", 1)
                else:
                    parsed_values[field_def.name] = p_enum

            elif field_def.type == "string":
                parsed_values[field_def.name] = val

        # For required string fields, store trimmed value
        for field_def in schema.fields:
            if field_def.required and field_def.type == "string":
                parsed_values[field_def.name] = row_dict.get(field_def.name, "").strip()

        # Step 3: Insert or Merge Lead
        self._upsert_lead(schema, parsed_values, canonical_phone, workflow_id, batch_id)

    def _reject_row(
        self,
        workflow_id: str,
        batch_id: str,
        row_number: int,
        reason: str,
        file_path: str,
    ) -> None:
        """Record row rejection and update quality metrics."""
        increment_metric(self.conn, "lead_rows_rejected", 1)
        increment_metric(self.conn, f"rej_{reason}", 1)

        self.conn.execute(
            """
            INSERT INTO lead_rejections (workflow_id, batch_id, row_number, reason, file_path)
            VALUES (?, ?, ?, ?, ?)
            """,
            (workflow_id, batch_id, row_number, reason, file_path)
        )

    def _upsert_lead(
        self,
        schema: WorkflowSchema,
        values: Dict[str, Optional[str]],
        canonical_phone: str,
        workflow_id: str,
        batch_id: str,
    ) -> None:
        """
        Merge duplicate leads or insert fresh lead.
        Identity is (workflow_id, canonical_phone).
        """
        phone_hmac = self.crypto.phone_lookup_hmac(canonical_phone)

        # Check if lead already exists
        existing = self.conn.execute(
            "SELECT * FROM leads WHERE workflow_id = ? AND phone_lookup_hmac = ?",
            (workflow_id, phone_hmac)
        ).fetchone()

        now_iso = datetime.now(timezone.utc).isoformat()
        ref_field = schema.reference_field.name if schema.reference_field else "reference"
        external_ref = values.get(ref_field) or ""

        if existing is None:
            lead_uuid = str(uuid.uuid4())
            # First valid appearance: apply defaults to fields that are still null
            for f in schema.fields:
                if f.default and values.get(f.name) is None:
                    values[f.name] = f.default

            # Encrypt PII bound to immutable internal lead_uuid
            customer_name = values.get("customer_name") or ""
            enc_name = self.crypto.encrypt_field(
                customer_name,
                aad_context=f"{workflow_id}:customer_name:{lead_uuid}"
            )
            enc_phone = self.crypto.encrypt_field(
                canonical_phone,
                aad_context=f"{workflow_id}:phone:{lead_uuid}"
            )

            self.conn.execute(
                """
                INSERT INTO leads (
                    lead_uuid, workflow_id, phone_lookup_hmac, encrypted_phone, encrypted_customer_name,
                    external_ref, initial_batch_id, policy_expiry, premium_amount,
                    order_date, product, preferred_language, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lead_uuid,
                    workflow_id,
                    phone_hmac,
                    enc_phone,
                    enc_name,
                    external_ref,
                    batch_id,  # permanent initial_batch_id
                    values.get("policy_expiry"),
                    values.get("premium_amount"),
                    values.get("order_date"),
                    values.get("product"),
                    values.get("preferred_language") or "en",
                    now_iso,
                    now_iso,
                )
            )
        else:
            lead_uuid = existing["lead_uuid"]
            # Duplicate lead row: increment duplicate counter
            increment_metric(self.conn, "duplicate_lead_rows_merged", 1)

            # Update only non-null fields; batch_id stays permanent
            update_clauses = ["updated_at = ?"]
            params = [now_iso]

            # If customer_name is provided in the duplicate row, update it
            if values.get("customer_name"):
                new_enc_name = self.crypto.encrypt_field(
                    values["customer_name"],
                    aad_context=f"{workflow_id}:customer_name:{lead_uuid}"
                )
                update_clauses.append("encrypted_customer_name = ?")
                params.append(new_enc_name)

            if external_ref:
                update_clauses.append("external_ref = ?")
                params.append(external_ref)

            for col in ["policy_expiry", "premium_amount", "order_date", "product", "preferred_language"]:
                if values.get(col) is not None:
                    update_clauses.append(f"{col} = ?")
                    params.append(values[col])

            params.append(existing["lead_id"])
            sql = f"UPDATE leads SET {', '.join(update_clauses)} WHERE lead_id = ?"
            self.conn.execute(sql, params)
