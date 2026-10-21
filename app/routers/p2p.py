from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth import AuthUser, get_current_user, optional_user, require_roles
from app.db import cache_clear
from app.services import p2p_service as svc

router = APIRouter(prefix="/api/p2p", tags=["p2p"])


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

@router.get("/filters")
def filters():
    return svc.get_filters()


@router.get("/data-version")
def data_version():
    return svc.get_data_version()


@router.post("/refresh-cache")
def refresh_cache():
    cleared = cache_clear()
    return {"ok": True, "cleared": cleared, **svc.get_data_version()}


@router.post("/refresh-pipeline")
def refresh_pipeline(_user: AuthUser = Depends(require_roles("admin"))):
    return svc.refresh_pipeline()


# ---------------------------------------------------------------------------
# Control Tower
# ---------------------------------------------------------------------------

@router.get("/summary")
def summary(
    company: str | None = None,
    vendor_id: str | None = None,
    category: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    return svc.get_summary(company, vendor_id, category, date_from, date_to)


# ---------------------------------------------------------------------------
# Invoice Desk
# ---------------------------------------------------------------------------

@router.get("/invoices")
def invoices(
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
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return svc.get_invoices(
        company=company,
        vendor_id=vendor_id,
        status=status,
        exception_code=exception_code,
        priority_band=priority_band,
        open_only=open_only,
        search=search,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@router.get("/invoices/{invoice_number}")
def invoice_detail(invoice_number: str, company_code: str | None = None):
    detail = svc.get_invoice_detail(invoice_number, company_code)
    if not detail.get("header"):
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_number} not found")
    return detail


class PayRequest(BaseModel):
    company_code: str
    fiscal_year: str


@router.post("/invoices/{invoice_number}/pay")
def pay_invoice(
    invoice_number: str,
    body: PayRequest,
    _user: AuthUser = Depends(require_roles("admin", "ap_ops", "treasury")),
):
    return svc.pay_invoice(invoice_number, body.company_code, body.fiscal_year)


class NoteRequest(BaseModel):
    company_code: str | None = None
    fiscal_year: str | None = None
    exception_code: str | None = None
    resolution: str | None = None
    notes: str | None = None


@router.post("/invoices/{invoice_number}/note")
def invoice_note(
    invoice_number: str,
    body: NoteRequest,
    user: AuthUser = Depends(require_roles("admin", "ap_ops", "treasury", "procurement")),
):
    return svc.add_invoice_note(
        invoice_number,
        body.company_code,
        body.fiscal_year,
        body.exception_code,
        body.resolution,
        body.notes,
        user.name,
    )


# ---------------------------------------------------------------------------
# Cash & Discounts
# ---------------------------------------------------------------------------

@router.get("/cash")
def cash_overview(
    company: str | None = None,
    horizon_days: int = Query(14, ge=1, le=120),
    weeks: int = Query(13, ge=1, le=26),
):
    return svc.get_cash_overview(company, horizon_days, weeks)


@router.get("/cash/discounts")
def discounts(
    company: str | None = None,
    window_status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
):
    return svc.get_discount_worklist(company, window_status, limit)


@router.get("/cash/grir")
def grir(
    vendor_id: str | None = None,
    age_bucket: str | None = None,
    min_age_days: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    return svc.get_grir_worklist(vendor_id, age_bucket, min_age_days, limit)


# ---------------------------------------------------------------------------
# Vendor 360
# ---------------------------------------------------------------------------

@router.get("/vendors")
def vendors(
    risk_band: str | None = None,
    vendor_tier: str | None = None,
    search: str | None = None,
    sort: str = "risk",
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return svc.get_vendors(risk_band, vendor_tier, search, sort, limit, offset)


@router.get("/vendors/{vendor_id}")
def vendor_detail(vendor_id: str):
    detail = svc.get_vendor_detail(vendor_id)
    if not detail.get("profile"):
        raise HTTPException(status_code=404, detail=f"Vendor {vendor_id} not found")
    return detail


class WatchlistRequest(BaseModel):
    reason_code: str | None = None
    notes: str | None = None
    risk_score: float | None = None


@router.post("/vendors/{vendor_id}/watchlist")
def vendor_watchlist(
    vendor_id: str,
    body: WatchlistRequest,
    user: AuthUser = Depends(require_roles("admin", "procurement", "ap_ops")),
):
    return svc.add_vendor_watchlist(
        vendor_id, body.reason_code, body.notes, body.risk_score, user.name
    )


# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------

@router.get("/compliance")
def compliance(
    company: str | None = None,
    finding: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    _user: AuthUser | None = Depends(optional_user),
):
    return svc.get_compliance_worklist(company, finding, limit)


@router.get("/me/context")
def my_context(user: AuthUser = Depends(get_current_user)):
    """Role plus the default landing filters the UI should apply for that role."""
    defaults = {
        "admin": {"page": "/", "priority_band": None},
        "ap_ops": {"page": "/invoices", "priority_band": "ACT_NOW"},
        "treasury": {"page": "/cash", "priority_band": None},
        "procurement": {"page": "/vendors", "priority_band": None},
        "exec": {"page": "/", "priority_band": None},
    }
    return {"name": user.name, "role": user.role, "defaults": defaults[user.role]}
