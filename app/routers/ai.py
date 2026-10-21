from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth import AuthUser, optional_user, persona_for, require_roles
from app.services import ai_service as ai

router = APIRouter(prefix="/api/ai", tags=["ai"])


# ---------------------------------------------------------------------------
# Copilot
# ---------------------------------------------------------------------------

@router.get("/copilot/quick-analyses")
def quick_analyses():
    return ai.quick_analyses()


class AnalysisRequest(BaseModel):
    key: str


@router.post("/copilot/run-analysis")
def run_analysis(body: AnalysisRequest, user: AuthUser | None = Depends(optional_user)):
    try:
        return ai.run_analysis(
            body.key,
            persona_for(user),
            user.name if user else "UNKNOWN",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class ChatRequest(BaseModel):
    question: str
    short_memory: str | None = None
    long_memory: str | None = None


@router.post("/copilot/chat")
def chat(body: ChatRequest, user: AuthUser | None = Depends(optional_user)):
    try:
        return ai.copilot_chat(
            body.question,
            persona_for(user),
            user.name if user else "UNKNOWN",
            short_memory=body.short_memory,
            long_memory=body.long_memory,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/copilot/frequent-questions")
def frequent_questions(
    limit: int = Query(8, ge=1, le=25),
    user: AuthUser | None = Depends(optional_user),
):
    return ai.frequent_questions(user.name if user else None, limit)


@router.get("/copilot/saved-insights")
def list_saved_insights(
    mine_only: bool = False,
    limit: int = Query(25, ge=1, le=100),
    user: AuthUser | None = Depends(optional_user),
):
    owner = user.name if (mine_only and user) else None
    return ai.saved_insights(owner, limit)


class SaveInsightRequest(BaseModel):
    title: str
    question: str
    sql_text: str | None = None
    page: str = "copilot"


@router.post("/copilot/save-insight")
def save_insight(
    body: SaveInsightRequest,
    user: AuthUser = Depends(require_roles("admin", "ap_ops", "treasury", "procurement", "exec")),
):
    return ai.save_insight(
        body.title, body.question, body.sql_text, persona_for(user), user.name, body.page
    )


class DeleteInsightRequest(BaseModel):
    insight_id: int


@router.post("/copilot/delete-insight")
def delete_insight(
    body: DeleteInsightRequest,
    user: AuthUser = Depends(require_roles("admin", "ap_ops", "treasury", "procurement", "exec")),
):
    return ai.delete_insight(body.insight_id, user.name, is_admin=user.role == "admin")


# ---------------------------------------------------------------------------
# Agent Console
# ---------------------------------------------------------------------------

@router.get("/agents/catalog")
def agents_catalog():
    return ai.agent_catalog()


@router.get("/agents/snapshot")
def agents_snapshot():
    return ai.console_snapshot()


@router.get("/agents/actions")
def agent_actions(
    agent_key: str | None = None,
    limit: int = Query(25, ge=1, le=200),
):
    return ai.recent_actions(agent_key, limit)


@router.get("/agents/{agent_key}/queue")
def agent_queue(
    agent_key: str,
    limit: int = Query(25, ge=1, le=200),
    company: str | None = None,
    vendor_id: str | None = None,
    exception_code: str | None = None,
    priority_band: str | None = None,
    window_status: str | None = None,
    risk_band: str | None = None,
    vendor_tier: str | None = None,
    age_bucket: str | None = None,
    min_age_days: int | None = None,
    min_score: int | None = None,
):
    try:
        return ai.agent_queue(
            agent_key,
            limit=limit,
            company=company,
            vendor_id=vendor_id,
            exception_code=exception_code,
            priority_band=priority_band,
            window_status=window_status,
            risk_band=risk_band,
            vendor_tier=vendor_tier,
            age_bucket=age_bucket,
            min_age_days=min_age_days,
            min_score=min_score,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class RecommendRequest(BaseModel):
    entity_id: str
    company_code: str | None = None
    po_item: int | None = None
    use_live_agent: bool | None = None


@router.post("/agents/{agent_key}/recommend")
def agent_recommend(
    agent_key: str,
    body: RecommendRequest,
    _user: AuthUser | None = Depends(optional_user),
):
    try:
        return ai.agent_recommend(
            agent_key,
            body.entity_id,
            {"company_code": body.company_code, "po_item": body.po_item},
            body.use_live_agent,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc


class ActRequest(BaseModel):
    entity_id: str
    entity_type: str = "INVOICE"
    action: str
    company_code: str | None = None
    exposure_amount: float | None = None
    recommendation: str | None = None
    notes: str | None = None
    source: str | None = None


@router.post("/agents/{agent_key}/act")
def agent_act(
    agent_key: str,
    body: ActRequest,
    user: AuthUser = Depends(require_roles("admin", "ap_ops", "treasury", "procurement")),
):
    return ai.log_agent_action(
        agent_key=agent_key,
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        action_taken=body.action,
        acted_by=user.name,
        company_code=body.company_code,
        exposure_amount=body.exposure_amount,
        recommendation=body.recommendation,
        notes=body.notes,
        source=body.source,
    )


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

@router.get("/policy/catalog")
def policy_catalog():
    return ai.policy_catalog()


@router.get("/policy/search")
def policy_search(
    q: str,
    domain: str | None = None,
    limit: int = Query(5, ge=1, le=20),
):
    return ai.policy_lookup(q, domain, limit)
