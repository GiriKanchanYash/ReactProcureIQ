"""Dynamic allow-list and schema prompt for P2P text-to-SQL.

The allow-list is intentionally restricted to curated analytical views in the
Fabric mart. Column metadata is read from INFORMATION_SCHEMA so the prompt
stays aligned with the deployed Fabric item without shipping a legacy warehouse YAML
file.
"""
from __future__ import annotations

import threading
from app import db
from app.db import run_query

_ALLOWED = frozenset({
    "invoice_360_vw",
    "invoice_exception_worklist_vw",
    "three_way_match_exception_vw",
    "discount_capture_worklist_vw",
    "payment_run_worklist_vw",
    "cash_commitment_schedule_vw",
    "duplicate_invoice_risk_vw",
    "grir_reconciliation_worklist_vw",
    "vendor_risk_profile_vw",
    "vendor_spend_concentration_vw",
    "spend_compliance_worklist_vw",
    "invoice_status_history_vw",
    "weighted_days_payable_outstanding_vw",
})
_lock = threading.Lock()
_catalog: dict | None = None


class SchemaCatalogError(RuntimeError):
    pass


def _load() -> dict:
    global _catalog
    with _lock:
        if _catalog is not None:
            return _catalog
        placeholders = ",".join("?" for _ in _ALLOWED)
        rows = run_query(
            f"""SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = ? AND LOWER(TABLE_NAME) IN ({placeholders})
                ORDER BY TABLE_NAME, ORDINAL_POSITION""",
            (db.SCHEMA, *sorted(_ALLOWED)),
        )
        tables: dict[str, dict] = {}
        for r in rows:
            t = str(r["table_name"]).lower()
            tables.setdefault(t, {"description": "Curated P2P analytical view.", "columns": []})
            tables[t]["columns"].append({
                "name": str(r["column_name"]).lower(),
                "data_type": r.get("data_type") or "",
            })
        if not tables:
            raise SchemaCatalogError("No curated P2P analytical views were found in Fabric.")
        _catalog = {
            "tables": tables,
            "allowed_tables": frozenset(tables),
            "allowed_columns": {t: frozenset(c["name"] for c in m["columns"]) for t, m in tables.items()},
        }
        return _catalog


def allowed_tables() -> frozenset[str]:
    return _load()["allowed_tables"]


def allowed_columns(table: str) -> frozenset[str]:
    return _load()["allowed_columns"].get(table.strip().lower(), frozenset())


def table_meta(table: str) -> dict | None:
    return _load()["tables"].get(table.strip().lower())


def schema_prompt_text(schema_prefix: str) -> str:
    cat = _load()
    lines = [
        "You are generating Microsoft Fabric Warehouse T-SQL.",
        f"Only query these curated views in schema {schema_prefix}:",
    ]
    for t in sorted(cat["tables"]):
        lines.append(f"TABLE {schema_prefix}.{t}")
        for c in cat["tables"][t]["columns"]:
            lines.append(f"  - {c['name']} ({c['data_type']})")
    return "\n".join(lines)
