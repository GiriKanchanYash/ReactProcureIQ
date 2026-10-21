from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

from app.db import (
    IM,
    cache_get,
    cache_set,
    esc,
    run_query,
    run_query_on_conn,
    get_connection,
)

INVOICE_VW = f"{IM}.INVOICE_360_VW"
EXCEPTION_VW = f"{IM}.INVOICE_EXCEPTION_WORKLIST_VW"
MATCH_VW = f"{IM}.THREE_WAY_MATCH_EXCEPTION_VW"
DISCOUNT_VW = f"{IM}.DISCOUNT_CAPTURE_WORKLIST_VW"
PAYRUN_VW = f"{IM}.PAYMENT_RUN_WORKLIST_VW"
CASH_VW = f"{IM}.CASH_COMMITMENT_SCHEDULE_VW"
DUP_VW = f"{IM}.DUPLICATE_INVOICE_RISK_VW"
GRIR_VW = f"{IM}.GRIR_RECONCILIATION_WORKLIST_VW"
VENDOR_VW = f"{IM}.VENDOR_RISK_PROFILE_VW"
CONCENTRATION_VW = f"{IM}.VENDOR_SPEND_CONCENTRATION_VW"
COMPLIANCE_VW = f"{IM}.SPEND_COMPLIANCE_WORKLIST_VW"
HISTORY_VW = f"{IM}.INVOICE_STATUS_HISTORY_VW"
DPO_VW = f"{IM}.WEIGHTED_DAYS_PAYABLE_OUTSTANDING_VW"


def _cached(key: str, value: Any) -> Any:
    return cache_set(key, value)


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------

def _eq(column: str, value: str | None) -> str:
    if not value or value in ("all", "All"):
        return ""
    return f" AND {column} = '{esc(value)}'"


def _date_range(column: str, date_from: str | None, date_to: str | None) -> str:
    parts = ""
    if date_from:
        parts += f" AND {column} >= '{esc(date_from[:10])}'"
    if date_to:
        parts += f" AND {column} <= '{esc(date_to[:10])}'"
    return parts


def _invoice_filters(
    company: str | None = None,
    vendor_id: str | None = None,
    category: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> str:
    return (
        _eq("COMPANY_CODE", company)
        + _eq("VENDOR_ID", vendor_id)
        + _eq("SPEND_CATEGORY", category)
        + _date_range("POSTING_DATE", date_from, date_to)
    )


def data_bounds() -> dict:
    cached = cache_get("bounds")
    if cached is not None:
        return cached
    rows = run_query(
        f"""
        SELECT MIN(POSTING_DATE) AS posting_date_min,
               MAX(POSTING_DATE) AS posting_date_max
        FROM {INVOICE_VW}
        """
    )
    row = rows[0] if rows else {}
    out = {
        "posting_date_min": row.get("posting_date_min"),
        "posting_date_max": row.get("posting_date_max"),
    }
    return cache_set("bounds", out)


def _resolve_window(
    date_from: str | None, date_to: str | None, default_days: int = 90
) -> tuple[str, str]:
    """Fill in a sensible window anchored on the latest posting date in the mart."""
    bounds = data_bounds()
    latest = bounds.get("posting_date_max") or date.today()
    if isinstance(latest, str):
        latest = date.fromisoformat(latest[:10])
    end = date.fromisoformat(date_to[:10]) if date_to else latest
    start = (
        date.fromisoformat(date_from[:10])
        if date_from
        else end - timedelta(days=default_days - 1)
    )
    return start.isoformat(), end.isoformat()


def _prior_window(start: str, end: str) -> tuple[str, str]:
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    span = (e - s).days + 1
    prior_end = s - timedelta(days=1)
    return (prior_end - timedelta(days=span - 1)).isoformat(), prior_end.isoformat()


def _run_parallel(specs: dict[str, tuple[str, tuple | None]]) -> dict[str, list[dict]]:
    """Run independent queries on one connection, concurrently."""

    def _one(item):
        key, (sql, params) = item
        with get_connection() as conn:
            return key, run_query_on_conn(conn, sql, params)

    out: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(specs)))) as pool:
        for key, rows in pool.map(_one, specs.items()):
            out[key] = rows
    return out


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def get_filters() -> dict:
    cached = cache_get("filters")
    if cached is not None:
        return cached

    specs = {
        "companies": (
            f"""
            SELECT DISTINCT COMPANY_CODE AS value, COMPANY_NAME AS label
            FROM {INVOICE_VW}
            WHERE COMPANY_CODE IS NOT NULL
            ORDER BY 2
            """,
            None,
        ),
        "vendors": (
            f"""
            SELECT VENDOR_ID AS value, VENDOR_NAME AS label,
                   ROUND(TOTAL_SPEND, 2) AS total_spend
            FROM {VENDOR_VW}
            ORDER BY TOTAL_SPEND DESC
            LIMIT 500
            """,
            None,
        ),
        "categories": (
            f"""
            SELECT DISTINCT SPEND_CATEGORY AS value
            FROM {INVOICE_VW}
            WHERE SPEND_CATEGORY IS NOT NULL
            ORDER BY 1
            """,
            None,
        ),
        "statuses": (
            f"""
            SELECT DISTINCT INVOICE_STATUS AS value
            FROM {INVOICE_VW}
            WHERE INVOICE_STATUS IS NOT NULL
            ORDER BY 1
            """,
            None,
        ),
        "exception_codes": (
            f"""
            SELECT EXCEPTION_CODE AS value, COUNT(*) AS invoice_count
            FROM {EXCEPTION_VW}
            GROUP BY EXCEPTION_CODE
            ORDER BY 2 DESC
            """,
            None,
        ),
    }
    res = _run_parallel(specs)
    bounds = data_bounds()
    out = {
        "companies": res["companies"],
        "vendors": res["vendors"],
        "categories": [r["value"] for r in res["categories"]],
        "statuses": [r["value"] for r in res["statuses"]],
        "exception_codes": res["exception_codes"],
        "posting_date_min": bounds.get("posting_date_min"),
        "posting_date_max": bounds.get("posting_date_max"),
    }
    return cache_set("filters", out)


# ---------------------------------------------------------------------------
# Control Tower summary
# ---------------------------------------------------------------------------

_KPI_SELECT = """
    SELECT
        COUNT(*)                                                                      AS invoice_count,
        COUNT(DISTINCT VENDOR_ID)                                                     AS vendor_count,
        COUNT(DISTINCT PURCHASE_ORDER_REFERENCE)                                      AS po_count,
        ROUND(SUM(CASE WHEN IS_CANCELLED = 0 THEN COALESCE(INVOICE_AMOUNT_LOCAL, 0) ELSE 0 END), 2) AS total_spend,
        ROUND(SUM(CASE WHEN IS_OPEN = 1 THEN COALESCE(INVOICE_AMOUNT_LOCAL, 0) ELSE 0 END), 2)      AS open_exposure,
        SUM(CASE WHEN IS_OPEN = 1 THEN 1 ELSE 0 END)                                  AS open_invoices,
        ROUND(SUM(CASE WHEN IS_OPEN = 1 AND DAYS_UNTIL_DUE < 0
                       THEN COALESCE(INVOICE_AMOUNT_LOCAL, 0) ELSE 0 END), 2)         AS overdue_exposure,
        SUM(CASE WHEN IS_OPEN = 1 AND DAYS_UNTIL_DUE < 0 THEN 1 ELSE 0 END)           AS overdue_invoices,
        SUM(CASE WHEN IS_DISPUTED = 1 THEN 1 ELSE 0 END)                              AS disputed_invoices,
        ROUND(AVG(DAYS_TO_PAY), 1)                                                    AS avg_days_to_pay,
        ROUND(AVG(PAID_ON_TIME) * 100, 1)                                             AS on_time_payment_pct,
        ROUND(100.0 * SUM(CASE WHEN EXCEPTION_CODE = 'NONE' AND EVER_DISPUTED = 0
                                    AND EVER_OVERDUE = 0 THEN 1 ELSE 0 END)
              / NULLIF(COUNT(*), 0), 1)                                               AS first_pass_pct,
        ROUND(100.0 * SUM(AUTO_PROCESSED) / NULLIF(COUNT(*), 0), 1)                   AS touchless_pct,
        ROUND(100.0 * SUM(CASE WHEN PURCHASE_ORDER_REFERENCE IS NOT NULL THEN 1 ELSE 0 END)
              / NULLIF(COUNT(*), 0), 1)                                               AS po_coverage_pct,
        SUM(CASE WHEN IS_OPEN = 1 AND EXCEPTION_CODE <> 'NONE' THEN 1 ELSE 0 END)     AS exception_invoices,
        ROUND(SUM(CASE WHEN IS_OPEN = 1 AND EXCEPTION_CODE <> 'NONE'
                       THEN COALESCE(INVOICE_AMOUNT_LOCAL, 0) ELSE 0 END), 2)         AS exception_exposure,
        ROUND(SUM(CASE WHEN IS_OPEN = 1 AND DISCOUNT_PERCENT > 0
                            AND DISCOUNT_DEADLINE >= CAST(GETDATE() AS date)
                       THEN COALESCE(DISCOUNT_VALUE, 0) ELSE 0 END), 2)               AS discount_available,
        ROUND(SUM(CASE WHEN DISCOUNT_PERCENT > 0 AND DISCOUNT_DEADLINE < CAST(GETDATE() AS date)
                            AND IS_OPEN = 1
                       THEN COALESCE(DISCOUNT_VALUE, 0) ELSE 0 END), 2)               AS discount_missed,
        ROUND(SUM(COALESCE(DUPLICATE_PAYMENT_AMOUNT, 0)), 2)                          AS duplicate_payment_amount
"""


def get_summary(
    company: str | None = None,
    vendor_id: str | None = None,
    category: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    start, end = _resolve_window(date_from, date_to)
    prior_start, prior_end = _prior_window(start, end)
    cache_key = f"summary:{company}:{vendor_id}:{category}:{start}:{end}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    current_f = _invoice_filters(company, vendor_id, category, start, end)
    prior_f = _invoice_filters(company, vendor_id, category, prior_start, prior_end)
    scope_f = _invoice_filters(company, vendor_id, category)
    trend_start = (date.fromisoformat(end) - timedelta(days=365)).isoformat()

    specs = {
        "current": (f"{_KPI_SELECT} FROM {INVOICE_VW} WHERE 1=1 {current_f}", None),
        "prior": (f"{_KPI_SELECT} FROM {INVOICE_VW} WHERE 1=1 {prior_f}", None),
        "status_mix": (
            f"""
            SELECT
                CASE
                    WHEN IS_PAID = 1 THEN 'Paid'
                    WHEN IS_DISPUTED = 1 THEN 'Disputed'
                    WHEN IS_OPEN = 1 AND DAYS_UNTIL_DUE < 0 THEN 'Overdue'
                    WHEN IS_OPEN = 1 THEN 'Open'
                    ELSE 'Other'
                END AS status,
                COUNT(*) AS invoice_count,
                ROUND(SUM(COALESCE(INVOICE_AMOUNT_LOCAL, 0)), 2) AS amount
            FROM {INVOICE_VW}
            WHERE 1=1 {current_f}
            GROUP BY CASE
                    WHEN IS_PAID = 1 THEN 'Paid'
                    WHEN IS_DISPUTED = 1 THEN 'Disputed'
                    WHEN IS_OPEN = 1 AND DAYS_UNTIL_DUE < 0 THEN 'Overdue'
                    WHEN IS_OPEN = 1 THEN 'Open'
                    ELSE 'Other'
                END
            ORDER BY 3 DESC
            """,
            None,
        ),
        "spend_trend": (
            f"""
            SELECT
                CONVERT(char(7), POSTING_DATE, 126) AS month,
                ROUND(SUM(CASE WHEN IS_CANCELLED = 0 THEN COALESCE(INVOICE_AMOUNT_LOCAL, 0) ELSE 0 END), 2) AS spend,
                COUNT(*) AS invoice_count,
                ROUND(AVG(DAYS_TO_PAY), 1) AS avg_days_to_pay
            FROM {INVOICE_VW}
            WHERE POSTING_DATE >= '{trend_start}'
              AND POSTING_DATE <= '{end}'
              {_eq("COMPANY_CODE", company)}{_eq("VENDOR_ID", vendor_id)}{_eq("SPEND_CATEGORY", category)}
            GROUP BY CONVERT(char(7), POSTING_DATE, 126)
            ORDER BY 1
            """,
            None,
        ),
        "vendor_concentration": (
            f"""
            WITH s AS (
                SELECT VENDOR_ID, VENDOR_NAME,
                       SUM(COALESCE(INVOICE_AMOUNT_LOCAL, 0)) AS spend,
                       COUNT(*) AS invoice_count
                FROM {INVOICE_VW}
                WHERE IS_CANCELLED = 0 {current_f}
                GROUP BY VENDOR_ID, VENDOR_NAME
            )
            SELECT VENDOR_ID AS vendor_id, VENDOR_NAME AS vendor_name,
                   ROUND(spend, 2) AS spend,
                   invoice_count,
                   ROUND(100.0 * spend / NULLIF(SUM(spend) OVER (), 0), 2) AS share_pct
            FROM s
            ORDER BY spend DESC
            LIMIT 12
            """,
            None,
        ),
        "aging": (
            f"""
            SELECT AGING_BUCKET AS bucket,
                   COUNT(*) AS invoice_count,
                   ROUND(SUM(COALESCE(INVOICE_AMOUNT_LOCAL, 0)), 2) AS amount
            FROM {INVOICE_VW}
            WHERE IS_OPEN = 1 {scope_f}
            GROUP BY AGING_BUCKET
            ORDER BY
                CASE AGING_BUCKET
                    WHEN 'NOT_YET_DUE' THEN 1
                    WHEN '1_30_PAST_DUE' THEN 2
                    WHEN '31_60_PAST_DUE' THEN 3
                    WHEN '61_90_PAST_DUE' THEN 4
                    WHEN 'OVER_90_PAST_DUE' THEN 5
                    ELSE 6
                END
            """,
            None,
        ),
        "exception_mix": (
            f"""
            SELECT EXCEPTION_CODE AS exception_code,
                   PRIORITY_BAND AS priority_band,
                   COUNT(*) AS invoice_count,
                   ROUND(SUM(COALESCE(INVOICE_AMOUNT_LOCAL, 0)), 2) AS amount
            FROM {EXCEPTION_VW}
            WHERE 1=1 {_eq("COMPANY_CODE", company)}{_eq("VENDOR_ID", vendor_id)}{_eq("SPEND_CATEGORY", category)}
            GROUP BY EXCEPTION_CODE, PRIORITY_BAND
            ORDER BY 4 DESC
            """,
            None,
        ),
        "cash_buckets": (
            f"""
            SELECT FORECAST_BUCKET AS bucket,
                   COUNT(*) AS invoice_count,
                   ROUND(SUM(COALESCE(INVOICE_AMOUNT_LOCAL, 0)), 2) AS amount
            FROM {PAYRUN_VW}
            WHERE 1=1 {_eq("COMPANY_CODE", company)}{_eq("VENDOR_ID", vendor_id)}
            GROUP BY FORECAST_BUCKET
            ORDER BY
                CASE FORECAST_BUCKET
                    WHEN 'OVERDUE_NOW' THEN 1
                    WHEN 'DUE_7_DAYS' THEN 2
                    WHEN 'DUE_14_DAYS' THEN 3
                    WHEN 'DUE_30_DAYS' THEN 4
                    WHEN 'DUE_60_DAYS' THEN 5
                    WHEN 'DUE_90_DAYS' THEN 6
                    ELSE 7
                END
            """,
            None,
        ),
        "dpo": (
            f"""
            SELECT CONCAT(YEAR, '-', RIGHT('0' + CAST(MONTH AS varchar(2)), 2)) AS month,
                   ROUND(WEIGHTED_DPO, 1) AS weighted_dpo
            FROM {DPO_VW}
            ORDER BY YEAR DESC, MONTH DESC
            LIMIT 12
            """,
            None,
        ),
    }

    res = _run_parallel(specs)
    current = res["current"][0] if res["current"] else {}
    prior = res["prior"][0] if res["prior"] else {}

    out = {
        "kpis": current,
        "prior": prior,
        "deltas": _deltas(current, prior),
        "charts": {
            "status_mix": res["status_mix"],
            "spend_trend": res["spend_trend"],
            "vendor_concentration": res["vendor_concentration"],
            "aging": res["aging"],
            "exception_mix": res["exception_mix"],
            "cash_buckets": res["cash_buckets"],
            "dpo_trend": list(reversed(res["dpo"])),
        },
        "scope": {
            "date_from": start,
            "date_to": end,
            "prior_date_from": prior_start,
            "prior_date_to": prior_end,
            "company": company,
            "vendor_id": vendor_id,
            "category": category,
        },
    }
    return cache_set(cache_key, out)


_DELTA_KEYS = (
    "total_spend",
    "invoice_count",
    "vendor_count",
    "po_count",
    "open_exposure",
    "overdue_exposure",
    "avg_days_to_pay",
    "on_time_payment_pct",
    "first_pass_pct",
    "touchless_pct",
    "po_coverage_pct",
    "exception_exposure",
    "discount_available",
)


def _deltas(current: dict, prior: dict) -> dict:
    out: dict[str, Any] = {}
    for key in _DELTA_KEYS:
        now = current.get(key)
        was = prior.get(key)
        if now is None or was in (None, 0):
            out[key] = None
            continue
        try:
            out[key] = round(100.0 * (float(now) - float(was)) / abs(float(was)), 1)
        except (TypeError, ValueError, ZeroDivisionError):
            out[key] = None
    return out


# ---------------------------------------------------------------------------
# Invoice Desk
# ---------------------------------------------------------------------------

_INVOICE_COLUMNS = """
    INVOICE_NUMBER        AS invoice_number,
    COMPANY_CODE          AS company_code,
    COMPANY_NAME          AS company_name,
    FISCAL_YEAR           AS fiscal_year,
    VENDOR_ID             AS vendor_id,
    VENDOR_NAME           AS vendor_name,
    PURCHASE_ORDER_REFERENCE AS purchase_order,
    SPEND_CATEGORY        AS spend_category,
    CURRENCY              AS currency,
    INVOICE_AMOUNT_LOCAL  AS invoice_amount,
    PO_AMOUNT             AS po_amount,
    ROUND(PO_VARIANCE_PCT, 2) AS po_variance_pct,
    POSTING_DATE          AS posting_date,
    DUE_DATE              AS due_date,
    PAYMENT_DATE          AS payment_date,
    DAYS_UNTIL_DUE        AS days_until_due,
    AGING_BUCKET          AS aging_bucket,
    INVOICE_STATUS        AS invoice_status,
    MATCH_STATUS          AS match_status,
    EXCEPTION_CODE        AS exception_code,
    PRIORITY_BAND         AS priority_band,
    PRIORITY_SCORE        AS priority_score,
    DISCOUNT_PERCENT      AS discount_percent,
    DISCOUNT_DEADLINE     AS discount_deadline,
    ROUND(DISCOUNT_VALUE, 2) AS discount_value,
    IS_OPEN               AS is_open,
    IS_PAID               AS is_paid,
    DUPLICATE_PAYMENT_AMOUNT AS duplicate_payment_amount
"""


def get_invoices(
    company: str | None = None,
    vendor_id: str | None = None,
    status: str | None = None,
    exception_code: str | None = None,
    priority_band: str | None = None,
    open_only: bool = False,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "priority",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    cache_key = (
        f"invoices:{company}:{vendor_id}:{status}:{exception_code}:{priority_band}:"
        f"{int(open_only)}:{search}:{date_from}:{date_to}:{sort}:{limit}:{offset}"
    )
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    where = "WHERE 1=1" + _invoice_filters(company, vendor_id, None, date_from, date_to)
    where += _eq("INVOICE_STATUS", status)
    where += _eq("EXCEPTION_CODE", exception_code)
    where += _eq("PRIORITY_BAND", priority_band)
    if open_only:
        where += " AND IS_OPEN = 1"
    if search:
        term = esc(search.strip())
        where += (
            " AND (UPPER(INVOICE_NUMBER) LIKE UPPER('%" + term + "%')"
            " OR UPPER(COALESCE(PURCHASE_ORDER_REFERENCE, '')) LIKE UPPER('%" + term + "%')"
            " OR UPPER(COALESCE(VENDOR_NAME, '')) LIKE UPPER('%" + term + "%')"
            " OR UPPER(COALESCE(VENDOR_ID, '')) LIKE UPPER('%" + term + "%'))"
        )

    order = {
        "priority": "PRIORITY_SCORE DESC",
        "amount": "INVOICE_AMOUNT_LOCAL DESC",
        "due_date": "DUE_DATE ASC",
        "posting_date": "POSTING_DATE DESC",
        "vendor": "VENDOR_NAME ASC",
    }.get(sort, "PRIORITY_SCORE DESC")

    specs = {
        "rows": (
            f"SELECT {_INVOICE_COLUMNS} FROM {INVOICE_VW} {where} "
            f"ORDER BY {order} LIMIT {int(limit)} OFFSET {int(offset)}",
            None,
        ),
        "total": (
            f"""
            SELECT COUNT(*) AS total,
                   ROUND(SUM(COALESCE(INVOICE_AMOUNT_LOCAL, 0)), 2) AS total_amount,
                   ROUND(SUM(CASE WHEN IS_OPEN = 1 THEN COALESCE(INVOICE_AMOUNT_LOCAL, 0) ELSE 0 END), 2) AS open_amount
            FROM {INVOICE_VW} {where}
            """,
            None,
        ),
    }
    res = _run_parallel(specs)
    totals = res["total"][0] if res["total"] else {}
    return _cached(cache_key, {
        "rows": res["rows"],
        "total": totals.get("total", 0),
        "total_amount": totals.get("total_amount", 0),
        "open_amount": totals.get("open_amount", 0),
        "limit": limit,
        "offset": offset,
    })


def get_invoice_detail(invoice_number: str, company_code: str | None = None) -> dict:
    cache_key = f"invoice_detail:{invoice_number}:{company_code}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    inv = esc(invoice_number)
    company_f = _eq("COMPANY_CODE", company_code)

    specs = {
        "header": (
            f"""
            SELECT {_INVOICE_COLUMNS},
                   VENDOR_COUNTRY        AS vendor_country,
                   VENDOR_INDUSTRY       AS vendor_industry,
                   VENDOR_POSTING_BLOCK  AS vendor_posting_block,
                   PLANT_CODE            AS plant_code,
                   PLANT_NAME            AS plant_name,
                   REGION                AS region,
                   PURCHASING_ORG        AS purchasing_org,
                   PURCHASING_GROUP      AS purchasing_group,
                   PAYMENT_TERMS         AS payment_terms,
                   DISCOUNT_DAYS         AS discount_days,
                   NET_DUE_DAYS          AS net_due_days,
                   DOCUMENT_DATE         AS document_date,
                   INVOICE_RECEIPT_DATE  AS invoice_receipt_date,
                   BASELINE_DATE         AS baseline_date,
                   CLEARING_DOCUMENT     AS clearing_document,
                   GL_ACCOUNT            AS gl_account,
                   AGING_DAYS            AS aging_days,
                   PO_TOTAL_AMOUNT       AS po_total_amount,
                   PO_DELIVERY_DATE      AS po_delivery_date,
                   PO_QTY_REMAINING      AS po_qty_remaining,
                   PO_VARIANCE_AMOUNT    AS po_variance_amount,
                   STATUS_NOTES          AS status_notes,
                   STATUS_EVENT_COUNT    AS status_event_count,
                   EVER_DISPUTED         AS ever_disputed,
                   EVER_OVERDUE          AS ever_overdue,
                   AUTO_PROCESSED        AS auto_processed
            FROM {INVOICE_VW}
            WHERE INVOICE_NUMBER = '{inv}' {company_f}
            LIMIT 1
            """,
            None,
        ),
        "status_history": (
            f"""
            SELECT SEQUENCE_NBR AS sequence_nbr,
                   STATUS       AS status,
                   EFFECTIVE_DATE AS effective_date,
                   STATUS_NOTES AS notes,
                   INVOICE_AMOUNT_LOCAL AS invoice_amount
            FROM {HISTORY_VW}
            WHERE INVOICE_NUMBER = '{inv}' {company_f}
            ORDER BY SEQUENCE_NBR
            """,
            None,
        ),
        "match": (
            f"""
            SELECT INVOICE_NUMBER AS invoice_number,
                   PURCHASE_ORDER_REFERENCE AS purchase_order,
                   INVOICE_AMOUNT_LOCAL AS invoice_amount,
                   PO_AMOUNT AS po_amount,
                   PO_TOTAL_AMOUNT AS po_total_amount,
                   PO_VARIANCE_AMOUNT AS po_variance_amount,
                   ROUND(PO_VARIANCE_PCT, 2) AS po_variance_pct,
                   PO_QTY_REMAINING AS qty_remaining,
                   PO_DELIVERY_DATE AS scheduled_delivery,
                   FIRST_GR_DATE AS first_gr_date,
                   ROUND(GR_AMOUNT, 2) AS gr_amount,
                   ROUND(IR_AMOUNT, 2) AS ir_amount,
                   ROUND(GRIR_BALANCE, 2) AS grir_balance,
                   MATCH_STATUS AS match_status,
                   MATCH_FINDING AS match_finding
            FROM {MATCH_VW}
            WHERE INVOICE_NUMBER = '{inv}' {company_f}
            LIMIT 1
            """,
            None,
        ),
        "duplicates": (
            f"""
            SELECT INVOICE_NUMBER AS invoice_number,
                   MATCHED_INVOICE_NUMBER AS matched_invoice_number,
                   MATCHED_AMOUNT AS matched_amount,
                   MATCHED_POSTING_DATE AS matched_posting_date,
                   MATCHED_STATUS AS matched_status,
                   ROUND(AMOUNT_DELTA, 2) AS amount_delta,
                   POSTING_DATE_GAP_DAYS AS posting_date_gap_days,
                   SAME_PO AS same_po,
                   RISK_SCORE AS risk_score,
                   RISK_BAND AS risk_band,
                   RISK_REASON AS risk_reason
            FROM {DUP_VW}
            WHERE INVOICE_NUMBER = '{inv}' OR MATCHED_INVOICE_NUMBER = '{inv}'
            ORDER BY RISK_SCORE DESC
            LIMIT 10
            """,
            None,
        ),
        "notes": (
            f"""
            SELECT NOTE_ID AS note_id, EXCEPTION_CODE AS exception_code,
                   RESOLUTION AS resolution, NOTES AS notes,
                   CREATED_BY AS created_by, CREATED_AT AS created_at
            FROM {IM}.INVOICE_EXCEPTION_NOTE
            WHERE INVOICE_NUMBER = '{inv}'
            ORDER BY CREATED_AT DESC
            LIMIT 20
            """,
            None,
        ),
    }
    res = _run_parallel(specs)
    header = res["header"][0] if res["header"] else None
    return _cached(cache_key, {
        "header": header,
        "status_history": res["status_history"],
        "match": res["match"][0] if res["match"] else None,
        "duplicates": res["duplicates"],
        "notes": res["notes"],
    })


def pay_invoice(invoice_number: str, company_code: str, fiscal_year: str) -> dict:
    rows = run_query(
        f"EXEC {IM}.P_PROCESS_INVOICE_PAYMENT ?, ?, ?",
        (invoice_number, company_code, fiscal_year),
    )
    message = ""
    if rows:
        message = str(list(rows[0].values())[0])
    cache_clear_summary()
    ok = not message.upper().startswith("ERROR")
    return {"ok": ok, "message": message or "Payment processed."}


def add_invoice_note(
    invoice_number: str,
    company_code: str | None,
    fiscal_year: str | None,
    exception_code: str | None,
    resolution: str | None,
    notes: str | None,
    created_by: str,
) -> dict:
    run_query(
        f"""
        INSERT INTO {IM}.INVOICE_EXCEPTION_NOTE
            (INVOICE_NUMBER, COMPANY_CODE, FISCAL_YEAR, EXCEPTION_CODE, RESOLUTION, NOTES, CREATED_BY)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            invoice_number,
            company_code,
            fiscal_year,
            exception_code,
            resolution,
            notes,
            created_by,
        ),
    )
    cache_clear_summary()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Cash & Discounts
# ---------------------------------------------------------------------------

def get_cash_overview(
    company: str | None = None,
    horizon_days: int = 14,
    weeks: int = 13,
) -> dict:
    cache_key = f"cash:{company}:{horizon_days}:{weeks}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    company_f = _eq("COMPANY_CODE", company)
    specs = {
        "commitment": (
            f"""
            SELECT WEEK_START AS week_start,
                   IS_OVERDUE_CARRYOVER AS is_overdue_carryover,
                   INVOICE_COUNT AS invoice_count,
                   VENDOR_COUNT AS vendor_count,
                   COMMITTED_AMOUNT AS committed_amount,
                   DISCOUNT_CAPTURABLE AS discount_capturable,
                   BLOCKED_AMOUNT AS blocked_amount
            FROM {CASH_VW}
            WHERE WEEK_START <= DATEADD(week, {int(weeks)}, DATEADD(week, DATEDIFF(week, 0, GETDATE()), 0))
            ORDER BY WEEK_START, IS_OVERDUE_CARRYOVER DESC
            """,
            None,
        ),
        "actions": (
            f"""
            SELECT RECOMMENDED_ACTION AS recommended_action,
                   COUNT(*) AS invoice_count,
                   ROUND(SUM(COALESCE(INVOICE_AMOUNT_LOCAL, 0)), 2) AS amount,
                   ROUND(SUM(COALESCE(DISCOUNT_VALUE, 0)), 2) AS discount_value
            FROM {PAYRUN_VW}
            WHERE 1=1 {company_f}
            GROUP BY RECOMMENDED_ACTION
            ORDER BY 3 DESC
            """,
            None,
        ),
        "buckets": (
            f"""
            SELECT FORECAST_BUCKET AS bucket,
                   COUNT(*) AS invoice_count,
                   ROUND(SUM(COALESCE(INVOICE_AMOUNT_LOCAL, 0)), 2) AS amount
            FROM {PAYRUN_VW}
            WHERE 1=1 {company_f}
            GROUP BY FORECAST_BUCKET
            """,
            None,
        ),
        "discount_summary": (
            f"""
            SELECT DISCOUNT_WINDOW_STATUS AS window_status,
                   COUNT(*) AS invoice_count,
                   ROUND(SUM(COALESCE(DISCOUNT_VALUE, 0)), 2) AS discount_value,
                   ROUND(AVG(ANNUALISED_RETURN_PCT), 1) AS avg_annualised_return_pct
            FROM {DISCOUNT_VW}
            WHERE 1=1 {company_f}
            GROUP BY DISCOUNT_WINDOW_STATUS
            """,
            None,
        ),
        "grir_summary": (
            f"""
            SELECT AGE_BUCKET AS age_bucket,
                   IMBALANCE_TYPE AS imbalance_type,
                   COUNT(*) AS item_count,
                   ROUND(SUM(GRIR_EXPOSURE), 2) AS exposure
            FROM {GRIR_VW}
            GROUP BY AGE_BUCKET, IMBALANCE_TYPE
            ORDER BY 4 DESC
            """,
            None,
        ),
        "dpo": (
            f"""
            WITH series AS (
                SELECT YEAR, MONTH, WEIGHTED_DPO, TOTAL_PAYABLES,
                       LAG(WEIGHTED_DPO) OVER (ORDER BY YEAR, MONTH) AS PRIOR_DPO
                FROM {DPO_VW}
            )
            SELECT CONCAT(YEAR, '-', RIGHT('0' + CAST(MONTH AS varchar(2)), 2)) AS month,
                   ROUND(WEIGHTED_DPO, 1) AS weighted_dpo,
                   ROUND(PRIOR_DPO, 1) AS prior_dpo,
                   ROUND(WEIGHTED_DPO - PRIOR_DPO, 1) AS dpo_change,
                   ROUND(TOTAL_PAYABLES, 2) AS total_payables
            FROM series
            ORDER BY YEAR DESC, MONTH DESC
            LIMIT 12
            """,
            None,
        ),
        "payment_run": (
            f"""
            SELECT INVOICE_NUMBER AS invoice_number,
                   COMPANY_CODE AS company_code,
                   VENDOR_ID AS vendor_id,
                   VENDOR_NAME AS vendor_name,
                   INVOICE_AMOUNT_LOCAL AS invoice_amount,
                   ROUND(DISCOUNT_VALUE, 2) AS discount_value,
                   DUE_DATE AS due_date,
                   DAYS_UNTIL_DUE AS days_until_due,
                   TARGET_PAYMENT_DATE AS target_payment_date,
                   RECOMMENDED_ACTION AS recommended_action,
                   FORECAST_BUCKET AS forecast_bucket,
                   EXCEPTION_CODE AS exception_code,
                   MATCH_STATUS AS match_status,
                   PRIORITY_SCORE AS priority_score
            FROM {PAYRUN_VW}
            WHERE TARGET_PAYMENT_DATE IS NOT NULL
              AND TARGET_PAYMENT_DATE <= DATEADD(day, {int(horizon_days)}, CAST(GETDATE() AS date))
              {company_f}
            ORDER BY
                CASE RECOMMENDED_ACTION
                    WHEN 'PAY_IMMEDIATELY' THEN 1
                    WHEN 'PAY_EARLY_FOR_DISCOUNT' THEN 2
                    WHEN 'PAY_THIS_WEEK' THEN 3
                    WHEN 'SCHEDULE_NEXT_RUN' THEN 4
                    ELSE 5
                END,
                PRIORITY_SCORE DESC
            LIMIT 200
            """,
            None,
        ),
    }
    res = _run_parallel(specs)
    return _cached(cache_key, {
        "commitment": res["commitment"],
        "actions": res["actions"],
        "buckets": res["buckets"],
        "discount_summary": res["discount_summary"],
        "grir_summary": res["grir_summary"],
        "dpo_trend": list(reversed(res["dpo"])),
        "payment_run": res["payment_run"],
        "horizon_days": horizon_days,
    })


def get_discount_worklist(
    company: str | None = None,
    window_status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    cache_key = f"discounts:{company}:{window_status}:{limit}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    where = "WHERE 1=1" + _eq("COMPANY_CODE", company) + _eq(
        "DISCOUNT_WINDOW_STATUS", window_status
    )
    return _cached(cache_key, run_query(
        f"""
        SELECT INVOICE_NUMBER AS invoice_number,
               COMPANY_CODE AS company_code,
               VENDOR_ID AS vendor_id,
               VENDOR_NAME AS vendor_name,
               INVOICE_AMOUNT_LOCAL AS invoice_amount,
               PAYMENT_TERMS AS payment_terms,
               DISCOUNT_PERCENT AS discount_percent,
               DISCOUNT_DAYS AS discount_days,
               NET_DUE_DAYS AS net_due_days,
               DISCOUNT_DEADLINE AS discount_deadline,
               DAYS_TO_DISCOUNT_DEADLINE AS days_to_deadline,
               ROUND(DISCOUNT_VALUE, 2) AS discount_value,
               ANNUALISED_RETURN_PCT AS annualised_return_pct,
               DISCOUNT_WINDOW_STATUS AS window_status,
               DUE_DATE AS due_date,
               PRIORITY_BAND AS priority_band
        FROM {DISCOUNT_VW}
        {where}
        ORDER BY DISCOUNT_VALUE DESC
        LIMIT {int(limit)}
        """
    ))


def get_grir_worklist(
    vendor_id: str | None = None,
    age_bucket: str | None = None,
    min_age_days: int = 0,
    limit: int = 100,
) -> list[dict]:
    cache_key = f"grir:{vendor_id}:{age_bucket}:{min_age_days}:{limit}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    where = (
        f"WHERE COALESCE(GRIR_AGE_DAYS, 0) >= {int(min_age_days)}"
        + _eq("VENDOR_ID", vendor_id)
        + _eq("AGE_BUCKET", age_bucket)
    )
    return _cached(cache_key, run_query(
        f"""
        SELECT PURCHASE_ORDER_NUMBER AS purchase_order,
               PO_ITEM AS po_item,
               VENDOR_ID AS vendor_id,
               VENDOR_NAME AS vendor_name,
               COMPANY_CODE AS company_code,
               PURCHASING_ORG AS purchasing_org,
               PO_DATE AS po_date,
               SCHEDULED_DELIVERY_DATE AS scheduled_delivery_date,
               FIRST_GR_DATE AS first_gr_date,
               ROUND(PO_AMOUNT, 2) AS po_amount,
               ROUND(GR_AMOUNT, 2) AS gr_amount,
               ROUND(IR_AMOUNT, 2) AS ir_amount,
               ROUND(GRIR_BALANCE, 2) AS grir_balance,
               ROUND(GRIR_EXPOSURE, 2) AS grir_exposure,
               GRIR_AGE_DAYS AS grir_age_days,
               AGE_BUCKET AS age_bucket,
               IMBALANCE_TYPE AS imbalance_type,
               PRIORITY_BAND AS priority_band,
               PRIORITY_SCORE AS priority_score,
               FINDING AS finding
        FROM {GRIR_VW}
        {where}
        ORDER BY PRIORITY_SCORE DESC
        LIMIT {int(limit)}
        """
    ))


# ---------------------------------------------------------------------------
# Vendor 360
# ---------------------------------------------------------------------------

_VENDOR_COLUMNS = """
    VENDOR_ID                   AS vendor_id,
    VENDOR_NAME                 AS vendor_name,
    COUNTRY_CODE                AS country_code,
    REGION_CODE                 AS region_code,
    CITY                        AS city,
    INDUSTRY_SECTOR             AS industry_sector,
    VENDOR_ACCOUNT_GROUP        AS vendor_account_group,
    VENDOR_TIER                 AS vendor_tier,
    POSTING_BLOCK               AS posting_block,
    DELETION_FLAG               AS deletion_flag,
    INVOICE_COUNT               AS invoice_count,
    PAID_INVOICES               AS paid_invoices,
    OPEN_INVOICES               AS open_invoices,
    ROUND(TOTAL_SPEND, 2)       AS total_spend,
    SPEND_RANK                  AS spend_rank,
    SPEND_SHARE_PCT             AS spend_share_pct,
    ROUND(OPEN_EXPOSURE, 2)     AS open_exposure,
    ROUND(OVERDUE_EXPOSURE, 2)  AS overdue_exposure,
    OVERDUE_INVOICES            AS overdue_invoices,
    OPEN_DISPUTES               AS open_disputes,
    TOTAL_DISPUTES              AS total_disputes,
    NON_PO_INVOICES             AS non_po_invoices,
    ROUND(NON_PO_SPEND, 2)      AS non_po_spend,
    PRICE_VARIANCE_INVOICES     AS price_variance_invoices,
    DUPLICATE_PAYMENT_INVOICES  AS duplicate_payment_invoices,
    ROUND(DUPLICATE_PAYMENT_AMOUNT, 2) AS duplicate_payment_amount,
    AVG_DAYS_TO_PAY             AS avg_days_to_pay,
    ON_TIME_PAYMENT_PCT         AS on_time_payment_pct,
    DISCOUNT_AVAILABLE          AS discount_available,
    DISCOUNT_MISSED             AS discount_missed,
    PO_LINES                    AS po_lines,
    PO_VALUE                    AS po_value,
    DELIVERY_ACCURACY_PCT       AS delivery_accuracy_pct,
    AVG_DELIVERY_DELAY_DAYS     AS avg_delivery_delay_days,
    OPEN_GRIR_ITEMS             AS open_grir_items,
    OPEN_GRIR_EXPOSURE          AS open_grir_exposure,
    FIRST_INVOICE_DATE          AS first_invoice_date,
    LAST_INVOICE_DATE           AS last_invoice_date,
    DAYS_SINCE_LAST_INVOICE     AS days_since_last_invoice,
    ROUND(RISK_SCORE, 1)        AS risk_score,
    RISK_BAND                   AS risk_band
"""


def get_vendors(
    risk_band: str | None = None,
    vendor_tier: str | None = None,
    search: str | None = None,
    sort: str = "risk",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    cache_key = f"vendors:{risk_band}:{vendor_tier}:{search}:{sort}:{limit}:{offset}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    where = "WHERE 1=1" + _eq("RISK_BAND", risk_band) + _eq("VENDOR_TIER", vendor_tier)
    if search:
        term = esc(search.strip())
        where += (
            " AND (UPPER(COALESCE(VENDOR_NAME, '')) LIKE UPPER('%" + term + "%')"
            " OR UPPER(COALESCE(VENDOR_ID, '')) LIKE UPPER('%" + term + "%'))"
        )
    order = {
        "risk": "RISK_SCORE DESC, TOTAL_SPEND DESC",
        "spend": "TOTAL_SPEND DESC",
        "exposure": "OPEN_EXPOSURE DESC",
        "delivery": "DELIVERY_ACCURACY_PCT ASC",
        "name": "VENDOR_NAME ASC",
    }.get(sort, "RISK_SCORE DESC")

    specs = {
        "rows": (
            f"SELECT {_VENDOR_COLUMNS} FROM {VENDOR_VW} {where} "
            f"ORDER BY {order} LIMIT {int(limit)} OFFSET {int(offset)}",
            None,
        ),
        "total": (
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN RISK_BAND = 'HIGH_RISK' THEN 1 ELSE 0 END) AS high_risk,
                   SUM(CASE WHEN RISK_BAND = 'ELEVATED' THEN 1 ELSE 0 END) AS elevated,
                   ROUND(SUM(TOTAL_SPEND), 2) AS total_spend,
                   ROUND(SUM(OPEN_EXPOSURE), 2) AS open_exposure
            FROM {VENDOR_VW} {where}
            """,
            None,
        ),
        "bands": (
            f"""
            SELECT RISK_BAND AS risk_band, COUNT(*) AS vendor_count,
                   ROUND(SUM(TOTAL_SPEND), 2) AS total_spend
            FROM {VENDOR_VW}
            GROUP BY RISK_BAND
            """,
            None,
        ),
        "concentration": (
            f"""
            SELECT VENDOR_ID AS vendor_id, VENDOR_NAME AS vendor_name,
                   ROUND(TOTAL_SPEND, 2) AS total_spend,
                   SPEND_RANK AS spend_rank,
                   SPEND_SHARE_PCT AS share_pct,
                   CUMULATIVE_SHARE_PCT AS cumulative_share_pct
            FROM {CONCENTRATION_VW}
            WHERE SPEND_RANK <= 20
            ORDER BY SPEND_RANK
            """,
            None,
        ),
    }
    res = _run_parallel(specs)
    totals = res["total"][0] if res["total"] else {}
    return _cached(cache_key, {
        "rows": res["rows"],
        "total": totals.get("total", 0),
        "high_risk": totals.get("high_risk", 0),
        "elevated": totals.get("elevated", 0),
        "total_spend": totals.get("total_spend", 0),
        "open_exposure": totals.get("open_exposure", 0),
        "bands": res["bands"],
        "concentration": res["concentration"],
        "limit": limit,
        "offset": offset,
    })


def get_vendor_detail(vendor_id: str) -> dict:
    cache_key = f"vendor_detail:{vendor_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    vid = esc(vendor_id)
    specs = {
        "profile": (
            f"SELECT {_VENDOR_COLUMNS} FROM {VENDOR_VW} WHERE VENDOR_ID = '{vid}' LIMIT 1",
            None,
        ),
        "spend_trend": (
            f"""
            SELECT CONVERT(char(7), POSTING_DATE, 126) AS month,
                   ROUND(SUM(COALESCE(INVOICE_AMOUNT_LOCAL, 0)), 2) AS spend,
                   COUNT(*) AS invoice_count,
                   ROUND(AVG(DAYS_TO_PAY), 1) AS avg_days_to_pay
            FROM {INVOICE_VW}
            WHERE VENDOR_ID = '{vid}' AND IS_CANCELLED = 0
            GROUP BY CONVERT(char(7), POSTING_DATE, 126)
            ORDER BY 1
            """,
            None,
        ),
        "open_invoices": (
            f"""
            SELECT {_INVOICE_COLUMNS}
            FROM {INVOICE_VW}
            WHERE VENDOR_ID = '{vid}' AND IS_OPEN = 1
            ORDER BY PRIORITY_SCORE DESC
            LIMIT 50
            """,
            None,
        ),
        "grir": (
            f"""
            SELECT PURCHASE_ORDER_NUMBER AS purchase_order, PO_ITEM AS po_item,
                   ROUND(GRIR_BALANCE, 2) AS grir_balance,
                   GRIR_AGE_DAYS AS grir_age_days,
                   AGE_BUCKET AS age_bucket,
                   IMBALANCE_TYPE AS imbalance_type,
                   FINDING AS finding
            FROM {GRIR_VW}
            WHERE VENDOR_ID = '{vid}'
            ORDER BY PRIORITY_SCORE DESC
            LIMIT 25
            """,
            None,
        ),
        "compliance": (
            f"""
            SELECT INVOICE_NUMBER AS invoice_number,
                   INVOICE_AMOUNT_LOCAL AS invoice_amount,
                   PURCHASE_ORDER_REFERENCE AS purchase_order,
                   ROUND(PO_VARIANCE_PCT, 2) AS po_variance_pct,
                   POSTING_DATE AS posting_date,
                   COMPLIANCE_FINDING AS compliance_finding,
                   PRIORITY_BAND AS priority_band
            FROM {COMPLIANCE_VW}
            WHERE VENDOR_ID = '{vid}'
            ORDER BY PRIORITY_SCORE DESC
            LIMIT 25
            """,
            None,
        ),
        "duplicates": (
            f"""
            SELECT INVOICE_NUMBER AS invoice_number,
                   MATCHED_INVOICE_NUMBER AS matched_invoice_number,
                   INVOICE_AMOUNT_LOCAL AS invoice_amount,
                   POSTING_DATE_GAP_DAYS AS posting_date_gap_days,
                   RISK_SCORE AS risk_score,
                   RISK_BAND AS risk_band,
                   RISK_REASON AS risk_reason
            FROM {DUP_VW}
            WHERE VENDOR_ID = '{vid}'
            ORDER BY RISK_SCORE DESC
            LIMIT 25
            """,
            None,
        ),
        "delivery": (
            f"""
            SELECT PURCHASE_ORDER_NUMBER AS purchase_order,
                   PO_ITEM AS po_item,
                   SCHEDULED_DELIVERY_DATE AS scheduled_delivery_date,
                   FIRST_GR_DATE AS first_gr_date,
                   DELIVERY_DELAY_DAYS AS delivery_delay_days,
                   DELIVERED_ON_TIME AS delivered_on_time,
                   ROUND(PO_AMOUNT, 2) AS po_amount
            FROM {IM}.PO_RECEIPT_PERFORMANCE_VW
            WHERE VENDOR_ID = '{vid}' AND FIRST_GR_DATE IS NOT NULL
            ORDER BY FIRST_GR_DATE DESC
            LIMIT 50
            """,
            None,
        ),
        "watchlist": (
            f"""
            SELECT WATCH_ID AS watch_id, REASON_CODE AS reason_code,
                   RISK_SCORE AS risk_score, NOTES AS notes, STATUS AS status,
                   ADDED_BY AS added_by, ADDED_AT AS added_at
            FROM {IM}.VENDOR_WATCHLIST
            WHERE VENDOR_ID = '{vid}'
            ORDER BY ADDED_AT DESC
            LIMIT 10
            """,
            None,
        ),
    }
    res = _run_parallel(specs)
    return _cached(cache_key, {
        "profile": res["profile"][0] if res["profile"] else None,
        "spend_trend": res["spend_trend"],
        "open_invoices": res["open_invoices"],
        "grir": res["grir"],
        "compliance": res["compliance"],
        "duplicates": res["duplicates"],
        "delivery": res["delivery"],
        "watchlist": res["watchlist"],
    })


def add_vendor_watchlist(
    vendor_id: str,
    reason_code: str | None,
    notes: str | None,
    risk_score: float | None,
    added_by: str,
) -> dict:
    run_query(
        f"""
        INSERT INTO {IM}.VENDOR_WATCHLIST (VENDOR_ID, REASON_CODE, RISK_SCORE, NOTES, ADDED_BY)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (vendor_id, reason_code, risk_score, notes, added_by),
    )
    cache_clear_summary()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------

def get_compliance_worklist(
    company: str | None = None,
    finding: str | None = None,
    limit: int = 100,
) -> dict:
    cache_key = f"compliance:{company}:{finding}:{limit}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    where = "WHERE 1=1" + _eq("COMPANY_CODE", company) + _eq(
        "COMPLIANCE_FINDING", finding
    )
    specs = {
        "rows": (
            f"""
            SELECT INVOICE_NUMBER AS invoice_number,
                   COMPANY_CODE AS company_code,
                   COMPANY_NAME AS company_name,
                   VENDOR_ID AS vendor_id,
                   VENDOR_NAME AS vendor_name,
                   PURCHASE_ORDER_REFERENCE AS purchase_order,
                   PURCHASING_ORG AS purchasing_org,
                   SPEND_CATEGORY AS spend_category,
                   PLANT_NAME AS plant_name,
                   INVOICE_AMOUNT_LOCAL AS invoice_amount,
                   PO_AMOUNT AS po_amount,
                   ROUND(PO_VARIANCE_PCT, 2) AS po_variance_pct,
                   POSTING_DATE AS posting_date,
                   INVOICE_STATUS AS invoice_status,
                   COMPLIANCE_FINDING AS compliance_finding,
                   PRIORITY_BAND AS priority_band,
                   PRIORITY_SCORE AS priority_score
            FROM {COMPLIANCE_VW}
            {where}
            ORDER BY PRIORITY_SCORE DESC
            LIMIT {int(limit)}
            """,
            None,
        ),
        "summary": (
            f"""
            SELECT COMPLIANCE_FINDING AS compliance_finding,
                   COUNT(*) AS invoice_count,
                   ROUND(SUM(COALESCE(INVOICE_AMOUNT_LOCAL, 0)), 2) AS amount
            FROM {COMPLIANCE_VW}
            {where}
            GROUP BY COMPLIANCE_FINDING
            ORDER BY 3 DESC
            """,
            None,
        ),
    }
    res = _run_parallel(specs)
    return _cached(cache_key, {"rows": res["rows"], "summary": res["summary"]})


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

def get_data_version() -> dict:
    rows = run_query(
        f"""
        SELECT MAX(LAST_STATUS_DATE) AS last_status_date,
               COUNT(*) AS invoice_count
        FROM {INVOICE_VW}
        """
    )
    row = rows[0] if rows else {}
    return {
        "version": f"{row.get('last_status_date')}:{row.get('invoice_count')}",
        "last_status_date": row.get("last_status_date"),
        "invoice_count": row.get("invoice_count"),
    }


def cache_clear_summary() -> None:
    from app.db import cache_clear

    cache_clear()


def refresh_pipeline() -> dict:
    """Re-run the stage -> vault -> dynamic table pipeline, then drop caches."""
    rows = run_query(f"EXEC {IM}.P_LOAD_FULL_DATA_PIPELINE")
    cache_clear_summary()
    message = str(list(rows[0].values())[0]) if rows else "Pipeline triggered."
    return {"ok": True, "message": message, **get_data_version()}
