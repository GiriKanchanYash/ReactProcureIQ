from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import re

from app.db import qcol, qview, run_query
from app.services import text_to_sql
from app.services import onelake_writer

from openai import AzureOpenAI, OpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

logger = logging.getLogger("orderlens.ai_service")

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "").strip()
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview").strip()
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini").strip()

_aoai_client: AzureOpenAI | OpenAI | None = None


def _get_aoai_client() -> AzureOpenAI | OpenAI:
    global _aoai_client
    if _aoai_client is not None:
        return _aoai_client

    if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_DEPLOYMENT:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT are required")

    api_key = os.getenv("AZURE_OPENAI_API_KEY", "").strip()
    if api_key:
        _aoai_client = AzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            api_key=api_key,
            api_version=AZURE_OPENAI_API_VERSION,
        )
    else:
        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://ai.azure.com/.default"
        )
        _aoai_client = OpenAI(
            base_url=AZURE_OPENAI_ENDPOINT.rstrip("/") + "/openai/v1/",
            api_key=token_provider,
        )
    return _aoai_client


def _llm_complete(prompt: str) -> str:
    client = _get_aoai_client()
    resp = client.chat.completions.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=900,
    )
    return (resp.choices[0].message.content or "").strip()


COPILOT_OUTPUT_DIRECTIVE = (
    "Format the answer with EXACTLY these two markdown sections and nothing else before them:\n"
    "**Descriptive:** What the data shows — concrete numbers and evidence from the facts.\n"
    "**Prescriptive:** Start with 'Here are the bullet points with specific findings, concrete actions, and explanations:' "
    "then list 4-5 bullets. Each bullet MUST use this pattern:\n"
    "- **Topic:** brief finding. **Action:** specific next step. **Why it matters:** why this helps sales ops / planning.\n"
    "Do not invent metrics that are not in the facts. Start directly with **Descriptive:**.\n"
)

AGENT_OUTPUT_DIRECTIVE = (
    "WORKBENCH MODE — respond in one pass with NO tools.\n"
    "Return ONLY these HTML section headers in this exact order. "
    "Do not use inline bold except for section headers. No markdown tables, no emojis, no preamble.\n\n"
    "<strong>SITUATION ASSESSMENT</strong>\n"
    "2-3 sentences with concrete facts from the JSON (ids, customer/material, amounts, ages, reasons). "
    "State the problem clearly — do not leave this section empty.\n\n"
    "<strong>IMMEDIATE ACTIONS (ranked)</strong>\n"
    "Exactly 3 numbered lines. Each line MUST start with a real ALL-CAPS action name, then a colon, then detail + Next step.\n"
    "GOOD examples:\n"
    "1. SUPPLY CHECK: Confirm ATP and open schedule qty. Next step: Fulfillment lead diagnoses today.\n"
    "2. CUSTOMER COMMIT: Align revised delivery date. Next step: CS confirms within 48h.\n"
    "3. EXPEDITE / TRANSFER: Evaluate alternate plant or expedite PO. Next step: Planner decides today.\n"
    "NEVER write the literal words 'ACTION LABEL'. Never use Title Case action names — use ALL CAPS names.\n\n"
    "<strong>CONTEXT</strong>\n"
    "2-4 short bullets with supporting facts from the JSON. Do not leave empty.\n\n"
    "<strong>TIMELINE</strong>\n"
    "Exactly 3 bullets: Today / +48h / +7d tied to the actions above. Do not leave empty.\n"
)


def _num(row: dict, *keys: str, default: float = 0.0) -> float:
    for k in keys:
        if k in row and row[k] is not None:
            try:
                return float(row[k])
            except (TypeError, ValueError):
                continue
    return default


def _str(row: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        if k in row and row[k] is not None:
            return str(row[k])
    return default


def fulfillment_queue(limit: int = 50) -> list[dict]:
    rows = run_query(f'''
        select
            {qcol("sales_order_id")} as sales_order_id,
            {qcol("customer_name")} as customer_name,
            {qcol("sales_order_category")} as sales_order_category,
            {qcol("order_age_days")} as order_age_days,
            {qcol("days_to_requested_delivery")} as days_to_requested_delivery,
            {qcol("open_value")} as open_value,
            {qcol("total_open_quantity")} as total_open_quantity,
            {qcol("backorder_reasons")} as backorder_reasons,
            {qcol("plant_name")} as plant_name,
            {qcol("material_key")} as material_key
        from {qview("orders_backlog_analysis")}
        order by {qcol("open_value")} desc
        OFFSET 0 ROWS FETCH NEXT {int(limit)} ROWS ONLY
    ''')
    out = []
    for r in rows:
        age = _num(r, "order_age_days")
        days_left = _num(r, "days_to_requested_delivery")
        if age >= 30 or days_left < 0:
            tier = "ACT_NOW"
        elif age >= 14 or days_left <= 7:
            tier = "PLAN"
        else:
            tier = "MONITOR"
        out.append({
            **{k: r.get(k) for k in r},
            "action_group": tier,
            "open_value": _num(r, "open_value"),
            "order_age_days": age,
        })
    return out


def inventory_queue(limit: int = 50) -> list[dict]:
    rows = run_query(f'''
        select
            {qcol("material_key")} as material_key,
            {qcol("material_description")} as material_description,
            {qcol("plant_name")} as plant_name,
            {qcol("country_code")} as country_code,
            {qcol("unrestricted_stock_quantity")} as stock_qty,
            {qcol("reorder_point_quantity")} as reorder_point,
            {qcol("reorder_shortage_quantity")} as shortage_qty,
            {qcol("planned_delivery_days")} as planned_delivery_days,
            {qcol("mrp_controller_code")} as mrp_controller
        from {qview("inventory_reorder_point_breaches")}
        order by {qcol("reorder_shortage_quantity")} desc
        OFFSET 0 ROWS FETCH NEXT {int(limit)} ROWS ONLY
    ''')
    out = []
    for r in rows:
        shortage = _num(r, "shortage_qty")
        stock = _num(r, "stock_qty")
        if stock <= 0 or shortage > _num(r, "reorder_point") * 0.5:
            tier = "ACT_NOW"
        elif shortage > 0:
            tier = "PLAN"
        else:
            tier = "MONITOR"
        out.append({**r, "action_group": tier, "shortage_qty": shortage})
    return out


def forecast_queue(limit: int = 50) -> list[dict]:
    rows = run_query(f'''
        select
            {qcol("material_key")} as material_key,
            {qcol("material_description")} as material_description,
            {qcol("plant_name")} as plant_name,
            {qcol("forecast_period")} as forecast_period,
            {qcol("variance_category")} as variance_category,
            {qcol("absolute_percentage_error")} as ape,
            {qcol("forecast_quantity")} as forecast_qty,
            {qcol("actual_sales_quantity")} as actual_qty,
            {qcol("forecast_method_code")} as forecast_method
        from {qview("forecasting_outlier_analysis")}
        where {qcol("variance_category")} <> 'Normal'
        order by {qcol("absolute_percentage_error")} desc
        OFFSET 0 ROWS FETCH NEXT {int(limit)} ROWS ONLY
    ''')
    out = []
    for r in rows:
        ape = _num(r, "ape")
        cat = _str(r, "variance_category")
        if ape >= 80 or "Significant" in cat:
            tier = "ACT_NOW"
        elif ape >= 50:
            tier = "PLAN"
        else:
            tier = "MONITOR"
        out.append({**r, "action_group": tier, "ape": ape})
    return out


def recommend(agent_key: str, context: dict) -> dict:
    """legacy AI COMPLETE recommendation in O2C workbench section format."""
    names = {
        "fulfillment": "Order Lens Fulfillment Agent",
        "inventory": "Order Lens Inventory Agent",
        "forecast": "Order Lens Forecast Agent",
    }
    prompts = {
        "fulfillment": (
            f"{AGENT_OUTPUT_DIRECTIVE}\n"
            "You are the Order Lens Fulfillment Agent. Focus on backlog aging, open value, "
            "delivery risk, and backorder reasons.\n"
            f"Backlog item JSON: {json.dumps(context, default=str)[:4000]}"
        ),
        "inventory": (
            f"{AGENT_OUTPUT_DIRECTIVE}\n"
            "You are the Order Lens Inventory Agent. Focus on reorder shortage, stock vs reorder point, "
            "and replenishment lead time.\n"
            f"Inventory breach JSON: {json.dumps(context, default=str)[:4000]}"
        ),
        "forecast": (
            f"{AGENT_OUTPUT_DIRECTIVE}\n"
            "You are the Order Lens Forecast Agent. Focus on variance category, APE, and method bias.\n"
            f"Forecast outlier JSON: {json.dumps(context, default=str)[:4000]}"
        ),
    }
    prompt = prompts.get(agent_key, prompts["fulfillment"])
    fallback = _template_recommend(agent_key, context)
    fallback["agent_name"] = names.get(agent_key, "Order Lens Agent")
    try:
        text = _llm_complete(prompt)
        text = _polish_agent_response(text, agent_key, context)
        if text.strip():
            return {
                "response": text,
                "source": "azure_openai",
                "agent_used": True,
                "agent_name": names.get(agent_key, "Order Lens Agent"),
                "cached": False,
            }
    except Exception as e:
        return {
            **fallback,
            "agent_used": False,
            "agent_error": str(e),
            "source": "error",
            "response": _polish_agent_response(fallback.get("response") or "", agent_key, context),
        }
    return {
        **fallback,
        "agent_used": False,
        "response": _polish_agent_response(fallback.get("response") or "", agent_key, context),
    }


def _parse_agent_sections(text: str) -> dict[str, str]:
    """Split workbench HTML into canonical section bodies."""
    raw = _normalize_agent_response(text or "")
    parts = re.split(r"<strong>([^<]+)</strong>", raw, flags=re.I)
    sections: dict[str, str] = {}
    canon_map = {
        "SITUATION": "SITUATION ASSESSMENT",
        "SITUATION ASSESSMENT": "SITUATION ASSESSMENT",
        "IMMEDIATE ACTIONS": "IMMEDIATE ACTIONS (ranked)",
        "IMMEDIATE ACTIONS (RANKED)": "IMMEDIATE ACTIONS (ranked)",
        "ACTIONS": "IMMEDIATE ACTIONS (ranked)",
        "CONTEXT": "CONTEXT",
        "ORDER CONTEXT": "CONTEXT",
        "TIMELINE": "TIMELINE",
    }
    for i in range(1, len(parts), 2):
        title = re.sub(r"\s+", " ", (parts[i] or "").strip()).upper()
        body = (parts[i + 1] if i + 1 < len(parts) else "").strip()
        key = None
        for prefix, canon in canon_map.items():
            if title == prefix or title.startswith(prefix):
                key = canon
                break
        if key:
            # Prefer non-empty body if section appears twice
            if body or key not in sections:
                sections[key] = body
    return sections


def _fix_action_labels(actions_body: str) -> str:
    """Replace placeholder ACTION LABEL / Title Case with ALL-CAPS action names."""
    lines_out: list[str] = []
    for line in (actions_body or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        if not m:
            lines_out.append(stripped)
            continue
        num, rest = m.group(1), m.group(2).strip()
        # Strip markdown bold around labels
        rest = re.sub(r"^\*\*(.+?)\*\*:?\s*", r"\1: ", rest)
        rest = re.sub(r"^<strong>([^<]+)</strong>:?\s*", r"\1: ", rest, flags=re.I)

        labeled = re.match(r"^([A-Za-z][A-Za-z0-9\s/&-]{1,40}):\s*(.+)$", rest)
        if labeled:
            label, detail = labeled.group(1).strip(), labeled.group(2).strip()
            if re.fullmatch(r"ACTION\s*LABELS?", label, flags=re.I) or label.lower() in {
                "action", "actions", "step", "next step",
            }:
                # Promote first phrase of detail to the label
                detail_m = re.match(
                    r"^([A-Za-z][A-Za-z0-9\s/&-]{2,40}?)\s*[–—\-:]\s*(.+)$",
                    detail,
                )
                if detail_m:
                    label = detail_m.group(1).strip()
                    detail = detail_m.group(2).strip()
                else:
                    words = detail.split()
                    label = " ".join(words[:3]) if words else "FOLLOW UP"
                    detail = " ".join(words[3:]) if len(words) > 3 else detail
            label = re.sub(r"\s+", " ", label).upper()
            if "next step:" not in detail.lower():
                detail = f"{detail} Next step: Owner executes within 48h."
            lines_out.append(f"{num}. {label}: {detail}")
        else:
            words = rest.split()
            label = " ".join(words[:3]).upper() if words else "FOLLOW UP"
            detail = " ".join(words[3:]) if len(words) > 3 else rest
            if "next step:" not in detail.lower():
                detail = f"{detail} Next step: Owner executes within 48h."
            lines_out.append(f"{num}. {label}: {detail}")
    return "\n".join(lines_out)


def _polish_agent_response(text: str, agent_key: str, context: dict) -> str:
    """Ensure all workbench sections are present, filled, and action labels are real."""
    template = _template_recommend(agent_key, context)["response"]
    tmpl_sections = _parse_agent_sections(template)
    live = _parse_agent_sections(text)
    order = [
        "SITUATION ASSESSMENT",
        "IMMEDIATE ACTIONS (ranked)",
        "CONTEXT",
        "TIMELINE",
    ]
    merged: dict[str, str] = {}
    for key in order:
        body = (live.get(key) or "").strip()
        # If situation empty but context has narrative prose, promote it
        if key == "SITUATION ASSESSMENT" and len(body) < 40:
            ctx = (live.get("CONTEXT") or "").strip()
            if len(ctx) > 60 and not ctx.lstrip().startswith("-"):
                body = ctx
        if key == "IMMEDIATE ACTIONS (ranked)":
            body = _fix_action_labels(body)
            # Require at least 2 numbered actions
            if len(re.findall(r"^\d+\.", body, flags=re.M)) < 2:
                body = tmpl_sections.get(key, body)
            else:
                body = _fix_action_labels(body)
        if len(body) < 20:
            body = tmpl_sections.get(key, body)
        merged[key] = body.strip()

    # If CONTEXT still equals situation prose, use template bullets
    if merged["CONTEXT"] and merged["CONTEXT"] == merged["SITUATION ASSESSMENT"]:
        merged["CONTEXT"] = tmpl_sections.get("CONTEXT", merged["CONTEXT"])

    parts = []
    for key in order:
        parts.append(f"<strong>{key}</strong>\n{merged[key]}")
    return "\n\n".join(parts).strip()


def _normalize_agent_response(text: str) -> str:
    """Ensure ALL-CAPS HTML section headers so AgentRecommendation colors match O2C."""
    raw = (text or "").strip()
    if not raw:
        return raw

    # Canonical O2C workbench section titles (matched case-insensitively)
    section_map = [
        (r"SITUATION(?:\s+ASSESSMENT)?", "SITUATION ASSESSMENT"),
        (r"IMMEDIATE\s+ACTIONS(?:\s*\(\s*RANKED\s*\))?|ACTIONS(?:\s*\(\s*RANKED\s*\))?", "IMMEDIATE ACTIONS (ranked)"),
        (r"(?:ORDER\s+)?CONTEXT", "CONTEXT"),
        (r"TIMELINE", "TIMELINE"),
    ]

    # Convert markdown **Section Title** only when it looks like a known header
    def _md_section(m: re.Match) -> str:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        upper = title.upper()
        for pattern, canon in section_map:
            if re.fullmatch(pattern, upper):
                return f"<strong>{canon}</strong>"
        return m.group(0)  # leave inline bold alone

    raw = re.sub(r"\*\*([^*\n]+)\*\*", _md_section, raw)

    def _fix_strong(m: re.Match) -> str:
        inner = re.sub(r"\s+", " ", m.group(1)).strip()
        upper = inner.upper()
        for pattern, canon in section_map:
            if re.fullmatch(pattern, upper):
                return f"<strong>{canon}</strong>"
        return m.group(0)

    raw = re.sub(r"<strong>([^<]+)</strong>", _fix_strong, raw, flags=re.I)

    for pattern, canon in section_map:
        raw = re.sub(
            rf"(?m)^(?:#+\s*)?(?:<strong>)?({pattern})(?:</strong>)?\s*$",
            f"<strong>{canon}</strong>",
            raw,
            flags=re.I,
        )
    # Flatten remaining inline bold so it doesn't create fake sections in the UI
    raw = re.sub(r"\*\*([^*]+)\*\*", r"\1", raw)
    return raw


def _template_recommend(agent_key: str, context: dict) -> dict:
    if agent_key == "inventory":
        mat = context.get("material_description") or context.get("material_key") or "material"
        plant = context.get("plant_name") or "plant"
        shortage = context.get("shortage_qty") or context.get("reorder_shortage_quantity") or 0
        stock = context.get("stock_qty") or 0
        reorder = context.get("reorder_point") or 0
        lead = context.get("planned_delivery_days") or 0
        text = f"""<strong>SITUATION ASSESSMENT</strong>
{mat} at {plant} is below reorder point (stock {stock}, reorder point {reorder}, shortage ≈ {shortage}).

<strong>IMMEDIATE ACTIONS (ranked)</strong>
1. REPLENISH: Raise purchase / STO for shortage quantity. Next step: MRP controller confirms today.
2. LEAD TIME CHECK: Confirm planned delivery days ({lead}d) and expedite if ACT_NOW. Next step: Buyer updates ETA within 24h.
3. SUBSTITUTE / TRANSFER: Check alternate plants or substitute materials. Next step: Planner reviews options today.

<strong>CONTEXT</strong>
- Material / plant driven by inventory_reorder_point_breaches
- Shortage quantity: {shortage}

<strong>TIMELINE</strong>
- Today: raise replenishment request
- +48h: confirm supply commitment
- +7d: verify stock recovery vs reorder point"""
    elif agent_key == "forecast":
        mat = context.get("material_description") or context.get("material_key") or "material"
        cat = context.get("variance_category") or "High Variance"
        ape = context.get("ape") or context.get("absolute_percentage_error") or 0
        plant = context.get("plant_name") or "plant"
        period = context.get("forecast_period") or ""
        text = f"""<strong>SITUATION ASSESSMENT</strong>
Forecast outlier for {mat} at {plant}: {cat} (APE ≈ {ape}%, period {period}).

<strong>IMMEDIATE ACTIONS (ranked)</strong>
1. DEMAND REVIEW: Compare recent actuals vs forecast assumptions. Next step: Demand planner investigates today.
2. METHOD ADJUST: Revisit forecast method / bias if pattern persists. Next step: Planning updates model this week.
3. ALIGN S&OP: Align sales ops and planning on near-term demand. Next step: Joint review before next cycle.

<strong>CONTEXT</strong>
- Source: forecasting_outlier_analysis
- Variance category: {cat}

<strong>TIMELINE</strong>
- Today: investigate outlier drivers
- This week: revise plan / method
- Next cycle: monitor MAPE improvement"""
    else:
        order_id = context.get("sales_order_id") or "order"
        age = context.get("order_age_days") or 0
        value = context.get("open_value") or 0
        customer = context.get("customer_name") or "customer"
        reasons = context.get("backorder_reasons") or "n/a"
        text = f"""<strong>SITUATION ASSESSMENT</strong>
Open backlog on {order_id} for {customer} (age {age} days, open value {value}). Backorder reasons: {reasons}.

<strong>IMMEDIATE ACTIONS (ranked)</strong>
1. SUPPLY CHECK: Confirm plant constraints and open schedule quantity. Next step: Fulfillment lead diagnoses today.
2. CUSTOMER COMMIT: Align on revised delivery date with customer. Next step: CS confirms within 48h.
3. BLOCK REVIEW: Escalate if delivery / billing / credit blocks exist on header. Next step: Ops clears blockers this week.

<strong>CONTEXT</strong>
- Customer: {customer}
- Backorder reason: {reasons}
- Open value: {value}
- Order age: {age} days

<strong>TIMELINE</strong>
- Today: diagnose supply constraint and confirm ATP
- +48h: commit revised date with customer
- +7d: clear open quantity or escalate shortage"""
    return {
        "response": text,
        "source": "template",
        "cached": False,
        "agent_used": False,
        "agent_name": {
            "fulfillment": "Order Lens Fulfillment Agent",
            "inventory": "Order Lens Inventory Agent",
            "forecast": "Order Lens Forecast Agent",
        }.get(agent_key, "Order Lens Agent"),
    }


def _next_int_id(table_view: str, id_column: str) -> int:
    """These Delta tables have no identity/auto-increment column (confirmed
    via notebook schema dump - plain nullable 32-bit IntegerType), so the
    app computes the next id itself from the existing (working) read-only
    SQL endpoint. NOTE: a millisecond-epoch id, as an earlier version of this
    code used, overflows a 32-bit IntegerType column - must stay a small int.
    Best-effort only: under concurrent writes from multiple app instances
    this can race and collide, same as any client-side id generation without
    a DB-side sequence - acceptable at this tool's traffic level."""
    rows = run_query(f"select MAX({id_column}) as m from {table_view}")
    current_max = (rows[0].get("m") if rows else None) or 0
    return int(current_max) + 1


def _onelake_write_or_readonly(kind: str, *args, **kwargs) -> dict:
    """kind is 'append' or 'delete' -> onelake_writer.append_rows / delete_rows.
    Degrades gracefully (ok: False) instead of raising when OneLake isn't
    configured yet or the write itself fails, so the UI doesn't 503; any
    programming error elsewhere still surfaces normally."""
    try:
        if kind == "append":
            onelake_writer.append_rows(*args, **kwargs)
        else:
            onelake_writer.delete_rows(*args, **kwargs)
        return {"ok": True}
    except onelake_writer.OneLakeWriteError as e:
        logger.warning("OneLake write skipped/failed: %s", e)
        return {"ok": False, "reason": "onelake_write_failed", "message": str(e)}


def mark_reviewed(
    agent_key: str,
    entity_id: str,
    entity_type: str,
    notes: str,
    user: str,
    action_taken: str = "MARK_REVIEWED",
) -> dict:
    action = (action_taken or "MARK_REVIEWED").strip().upper()[:80] or "MARK_REVIEWED"
    review_id = _next_int_id(qview("agent_review_log"), "REVIEW_ID")
    result = _onelake_write_or_readonly(
        "append",
        "ONELAKE_AGENT_REVIEW_LOG_PATH",
        [{
            "REVIEW_ID": review_id,
            "AGENT_KEY": agent_key,
            "ENTITY_ID": entity_id,
            "ENTITY_TYPE": entity_type,
            "ACTION_TAKEN": action,
            "NOTES": notes or "",
            "REVIEWED_BY": user,
            "REVIEWED_AT": onelake_writer.utcnow(),
        }],
    )
    return {**result, "action_taken": action}



def quick_analyses() -> list[dict]:
    return [
        {"key": "control_tower", "title": "Control Tower", "desc": "Summarize payable exposure, overdue risk, exceptions and discounts.", "question": "Summarize current P2P control-tower health and the highest financial risks."},
        {"key": "invoice_exceptions", "title": "Invoice Exceptions", "desc": "Find the highest-priority open invoice exceptions.", "question": "Which open invoice exceptions should AP act on first and why?"},
        {"key": "payment_optimization", "title": "Payment Optimization", "desc": "Identify discount and payment opportunities.", "question": "Where can we improve payment timing and capture early-payment discounts?"},
        {"key": "vendor_risk", "title": "Vendor Risk", "desc": "Highlight suppliers with the highest risk and exposure.", "question": "Which vendors have the highest risk or spend concentration?"},
    ]

def run_analysis(key: str, persona: str = "ADMIN", user_name: str = "app") -> dict:
    defs = {d["key"]: d for d in quick_analyses()}
    item = defs.get(key) or {
        "key": key or "custom",
        "title": key or "Analysis",
        "question": key or "Summarize sales ops health",
    }
    result = copilot_chat(item["question"], persona=persona, user_name=user_name)
    result["key"] = item["key"]
    if not result.get("metrics"):
        result["metrics"] = {"summary": item.get("title") or item["key"]}
    return result


def copilot_chat(question: str, persona: str = "ADMIN", user_name: str = "app", short_memory: str | None = None, long_memory: str | None = None) -> dict:
    """Return O2C-shaped Descriptive (blue) + Prescriptive (green) answer."""
    q = (question or "").strip()
    if not q:
        return _finalize_copilot({
            "key": "custom",
            "metrics": {},
            "rows": [],
            "sql": "",
            "descriptive": "Ask a sales ops, inventory, or forecast question.",
            "prescriptive": (
                "- **Pick a starting point:** Use a quick analysis tile or ask about backlog, reorder, or forecast. "
                "**Action:** Click Backlog Overview. **Why it matters:** Establishes a clear baseline."
            ),
            "source": "empty",
        }, q)

    try:
        norm = " ".join(q.lower().split())
        _onelake_write_or_readonly(
            "append",
            "ONELAKE_GENIE_QUESTION_HISTORY_PATH",
            [{
                "NORMALIZED_QUERY": norm,
                "TYPE": "ANALYTICS",
                "PERSONA": persona.upper(),
                "USER_NAME": user_name,
                "FREQUENCY": 1,
                "LAST_ASKED_AT": onelake_writer.utcnow(),
            }],
        )
    except Exception:
        pass

    digest = _facts_digest()

    generated_sql = ""
    t2sql_error = ""
    top_rows = None
    try:
        result = text_to_sql.run_text_to_sql(q, _llm_complete)
        generated_sql = result["sql"]
        top_rows = result["rows"]
    except text_to_sql.SqlGenerationError as e:
        # Model declined or couldn't be reached - fall back silently to the
        # keyword-based sample rows below, same as before this feature existed.
        t2sql_error = str(e)
    except text_to_sql.SqlValidationError as e:
        # Model produced SQL that failed a safety check - never executed.
        # Logged for visibility, then fall back the same way.
        logger.warning("Text-to-SQL validation rejected generated SQL for %r: %s", q, e)
        t2sql_error = str(e)
    except Exception as e:
        logger.error("Text-to-SQL pipeline failed for %r: %s", q, e)
        t2sql_error = str(e)

    if top_rows is None:
        top_rows = _sample_rows_for_question(q)

    prompt = (
        "You are Order Lens Copilot for sales operations planning.\n"
        f"{COPILOT_OUTPUT_DIRECTIVE}\n"
        f"Facts:\n{digest}\n\n"
        f"Sample rows (JSON):\n{json.dumps(top_rows[:5], default=str)[:3000]}\n\n"
        f"Question: {q}"
    )
    try:
        text = _llm_complete(prompt)
        if text.strip():
            descriptive, prescriptive = _split_desc_pres(text)
            return _finalize_copilot({
                "key": "custom",
                "metrics": {"summary": q[:80]},
                "rows": top_rows,
                "sql": generated_sql,
                "descriptive": descriptive,
                "prescriptive": prescriptive,
                "answer": text,
                "source": "azure_openai",
                "response_mode": "analytics",
                **({"text_to_sql_error": t2sql_error} if t2sql_error else {}),
            }, q)
    except Exception as e:
        return _finalize_copilot({
            "key": "custom",
            "metrics": {"summary": "fallback"},
            "rows": top_rows,
            "sql": generated_sql,
            "descriptive": _build_descriptive_from_rows(top_rows, q) or digest or f"AI unavailable ({e}).",
            "prescriptive": _default_prescriptive(top_rows, q),
            "answer": digest,
            "source": "fallback",
            "response_mode": "analytics",
            "error": str(e),
        }, q)

    return _finalize_copilot({
        "key": "custom",
        "metrics": {"summary": "digest"},
        "rows": top_rows,
        "sql": generated_sql,
        "descriptive": _build_descriptive_from_rows(top_rows, q) or digest,
        "prescriptive": _default_prescriptive(top_rows, q),
        "answer": digest,
        "source": "digest",
        "response_mode": "analytics",
    }, q)


def _split_desc_pres(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    if not raw:
        return "", ""
    for marker in (
        "**Prescriptive:**",
        "**Prescriptive**:",
        "## Prescriptive",
        "Prescriptive —",
        "Prescriptive -",
        "Prescriptive:",
    ):
        if marker in raw:
            left, right = raw.split(marker, 1)
            return _strip_descriptive_header(left), right.strip()
    # case-insensitive fallback
    lower = raw.lower()
    for marker in ("**prescriptive**", "\nprescriptive"):
        idx = lower.find(marker)
        if idx >= 0:
            return _strip_descriptive_header(raw[:idx]), raw[idx:].split(":", 1)[-1].strip()
    return _strip_descriptive_header(raw), ""


def _strip_descriptive_header(text: str) -> str:
    desc = (text or "").strip()
    desc = re.sub(r"^\*\*Descriptive:\*\*\s*", "", desc, flags=re.I)
    desc = re.sub(r"^\*\*Descriptive\*\*:?\s*", "", desc, flags=re.I)
    desc = re.sub(r"^#+\s*Descriptive\s*[—:-]*\s*", "", desc, flags=re.I)
    desc = re.sub(r"^Descriptive\s*[—:-]+\s*", "", desc, flags=re.I)
    return desc.strip()


def _has_action_why(text: str) -> bool:
    t = text or ""
    return "**Action:**" in t and "**Why it matters:**" in t


def _numbered_to_action_bullets(text: str) -> str:
    bullets: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r"^\d+\.\s+\*\*(.+?)\*\*:?\s*(.*)$", stripped)
        if m:
            title, body = m.group(1).strip(), m.group(2).strip()
            if "**Action:**" not in body:
                body = (
                    f"{body} **Action:** Review and act on this finding. "
                    f"**Why it matters:** Improves sales ops and planning outcomes."
                )
            bullets.append(f"- **{title}:** {body}")
            continue
        m2 = re.match(r"^[-•]\s+\*\*(.+?)\*\*:?\s*(.*)$", stripped)
        if m2:
            title, body = m2.group(1).strip(), m2.group(2).strip()
            if "**Action:**" not in body:
                body = (
                    f"{body} **Action:** Follow up with the owner. "
                    f"**Why it matters:** Keeps exceptions from aging further."
                )
            bullets.append(f"- **{title}:** {body}")
            continue
        if stripped.startswith("-"):
            if "**Action:**" not in stripped:
                stripped = (
                    f"{stripped} **Action:** Assign an owner today. "
                    f"**Why it matters:** Clear ownership drives closure."
                )
            bullets.append(stripped)
        elif stripped.startswith("**") and ":" in stripped:
            bullets.append(f"- {stripped}")
    return "\n\n".join(bullets) if bullets else (text or "").strip()


def _build_descriptive_from_rows(rows: list[dict], message: str) -> str:
    if not rows:
        return ""
    msg = (message or "").lower()
    if any(w in msg for w in ("reorder", "inventory", "shortage")):
        shortages = [_num(r, "shortage_qty") for r in rows]
        total = sum(shortages)
        top = rows[0]
        return (
            f"There are **{len(rows)}** materials below reorder point in the sample, "
            f"with combined shortage quantity **{total:,.0f}**. "
            f"Largest shortage: **{_str(top, 'material_description') or _str(top, 'material_key')}** "
            f"at **{_str(top, 'plant_name')}** (shortage **{_num(top, 'shortage_qty'):,.0f}**)."
        )
    if any(w in msg for w in ("forecast", "outlier", "mape", "ape")):
        apes = [_num(r, "ape") for r in rows if _num(r, "ape") > 0]
        avg = sum(apes) / len(apes) if apes else 0
        top = rows[0]
        return (
            f"Forecast outliers show average APE **{avg:.1f}%** across **{len(rows)}** rows. "
            f"Top outlier: **{_str(top, 'material_key')}** at **{_str(top, 'plant_name')}** "
            f"({_str(top, 'variance_category')}, APE **{_num(top, 'ape'):.1f}%**)."
        )
    if "dealer" in msg:
        top = rows[0]
        return (
            f"Top dealers by order value in the sample: **{_str(top, 'dealer_name')}** "
            f"with order value **{_num(top, 'total_order_value'):,.0f}** "
            f"({_num(top, 'fulfilled_orders'):,.0f}/{_num(top, 'total_orders'):,.0f} fulfilled)."
        )
    top = rows[0]
    total = sum(_num(r, "open_value") for r in rows)
    return (
        f"Open backlog sample covers **{len(rows)}** order lines with combined open value "
        f"**{total:,.0f}**. Highest exposure: order **{_str(top, 'sales_order_id')}** "
        f"for **{_str(top, 'customer_name')}** (open **{_num(top, 'open_value'):,.0f}**, "
        f"age **{_num(top, 'order_age_days'):.0f}** days)."
    )


def _default_prescriptive(rows: list[dict], message: str) -> str:
    msg = (message or "").lower()
    if any(w in msg for w in ("reorder", "inventory", "shortage")) and rows:
        top = rows[0]
        return "\n\n".join([
            f"- **Replenish top shortage:** {_str(top, 'material_key')} at {_str(top, 'plant_name')}. "
            f"**Action:** Raise purchase/STO for shortage {_num(top, 'shortage_qty'):,.0f}. "
            f"**Why it matters:** Prevents stock-outs and protects fulfillment OTIF.",
            "- **Prioritize ACT_NOW plants:** Focus on zero/negative stock first. "
            "**Action:** Sort Agent Workbench Inventory by Act now. "
            "**Why it matters:** Concentrates scarce supply attention where risk is highest.",
            "- **Confirm lead times:** Validate planned delivery days with buyers. "
            "**Action:** Update ETA on the top 5 breaches today. "
            "**Why it matters:** Accurate ETAs improve customer commitments.",
            "- **Monitor recovery:** Track shortage qty week over week. "
            "**Action:** Add reorder breach review to the weekly planning cadence. "
            "**Why it matters:** Early drift detection prevents recurring shortages.",
        ])
    if any(w in msg for w in ("forecast", "outlier", "mape")) and rows:
        top = rows[0]
        return "\n\n".join([
            f"- **Investigate top outlier:** {_str(top, 'material_key')} ({_str(top, 'variance_category')}). "
            f"**Action:** Demand planner reviews actual vs forecast today. "
            f"**Why it matters:** High APE drives excess stock or lost sales.",
            "- **Adjust biased methods:** Revisit forecast method where error persists. "
            "**Action:** Flag methods with APE > 50% in the next S&OP. "
            "**Why it matters:** Method bias compounds across periods.",
            "- **Align sales & planning:** Confirm near-term demand signals. "
            "**Action:** Share outlier list with sales ops this week. "
            "**Why it matters:** Consensus demand reduces surprise variance.",
            "- **Track MAPE trend:** Watch accuracy after changes. "
            "**Action:** Review Forecast tab in Agent Workbench weekly. "
            "**Why it matters:** Confirms corrective actions are working.",
        ])
    if rows:
        top = rows[0]
        return "\n\n".join([
            f"- **Clear top backlog:** Order {_str(top, 'sales_order_id')} for {_str(top, 'customer_name')}. "
            f"**Action:** Confirm supply and commit a delivery date within 48h. "
            f"**Why it matters:** Highest open value drives customer service risk.",
            "- **Work Act now queue:** Age and overdue delivery first. "
            "**Action:** Open Agent Workbench → Fulfillment → Act now. "
            "**Why it matters:** Prioritization protects OTIF and backlog dollars.",
            "- **Remove blockers:** Check delivery/billing/credit blocks on open headers. "
            "**Action:** Ops clears blocks on aged open orders this week. "
            "**Why it matters:** Blocks silently stall fulfillment.",
            "- **Monitor backlog value:** Track open value trend. "
            "**Action:** Review Command Center backlog KPI daily. "
            "**Why it matters:** Early warning before aging worsens.",
        ])
    return "\n\n".join([
        "- **Start in Command Center:** Review backlog, reorder, and forecast KPIs. "
        "**Action:** Open Command Center and note red tiles. "
        "**Why it matters:** Gives a shared baseline for sales ops.",
        "- **Use Agent Workbench:** Work prioritized exceptions. "
        "**Action:** Clear Act now items in Fulfillment/Inventory/Forecast. "
        "**Why it matters:** Focuses effort where risk is highest.",
        "- **Ask a specific question:** Narrow to a plant, material, or dealer. "
        "**Action:** Rephrase with an entity name or period. "
        "**Why it matters:** Produces more actionable recommendations.",
    ])


def _finalize_copilot(result: dict, message: str) -> dict:
    """Shape answers like OrderToCash: Descriptive + Prescriptive + chart."""
    out = dict(result)
    rows = out.get("rows") or []
    desc = _strip_descriptive_header(str(out.get("descriptive") or ""))
    presc = str(out.get("prescriptive") or "").strip()

    # If descriptive still contains a Prescriptive section, split it
    if desc and not presc:
        split_desc, split_presc = _split_desc_pres(desc)
        if split_presc:
            desc, presc = split_desc, split_presc
    elif desc and "**Prescriptive" in desc:
        split_desc, split_presc = _split_desc_pres(desc)
        if split_presc:
            desc = split_desc
            presc = "\n\n".join(p for p in (presc, split_presc) if p).strip()

    presc = re.sub(r"^Here are the bullet points[^\n]*\n+", "", presc, flags=re.I).strip()
    presc = re.sub(r"^\*\*Prescriptive:\*\*\s*", "", presc, flags=re.I).strip()

    if (not desc or len(desc) < 40) and rows:
        built = _build_descriptive_from_rows(rows, message)
        if built:
            desc = built

    if not _has_action_why(presc):
        presc = _numbered_to_action_bullets(presc)
    if not _has_action_why(presc):
        presc = _default_prescriptive(rows, message)

    rows = rows[:50]
    chart_rows = _aggregate_rows_for_chart(rows, message)
    out["descriptive"] = desc
    out["prescriptive"] = presc
    out["rows"] = rows
    out["chart_rows"] = chart_rows
    out["chart"] = out.get("chart") or _build_chart_from_rows(chart_rows or rows, message)
    out["answer"] = (
        f"**Descriptive:**\n{desc}\n\n**Prescriptive:**\n{presc}" if desc or presc else out.get("answer") or ""
    )
    out["response_mode"] = out.get("response_mode") or "analytics"
    return out


def _aggregate_rows_for_chart(rows: list[dict], message: str = "") -> list[dict]:
    """Roll detail rows into a chart-friendly aggregation (top N categories)."""
    if not rows:
        return []
    msg = (message or "").lower()
    keys = set(rows[0].keys())

    def _agg(label_key: str, value_key: str, limit: int = 12) -> list[dict]:
        buckets: dict[str, float] = {}
        for r in rows:
            label = str(r.get(label_key) or "").strip() or "Unknown"
            buckets[label] = buckets.get(label, 0.0) + _num(r, value_key)
        ranked = sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        return [{label_key: k, value_key: v} for k, v in ranked if v]

    if any(w in msg for w in ("reorder", "inventory", "shortage")) and "shortage_qty" in keys:
        label = "material_description" if "material_description" in keys else "material_key"
        if label in keys:
            return _agg(label, "shortage_qty")
    if any(w in msg for w in ("forecast", "outlier", "mape", "ape")) and "ape" in keys:
        label = "material_description" if any(str(r.get("material_description") or "").strip() for r in rows[:5]) else "material_key"
        if label in keys:
            # Keep top outliers by APE (already row-level); de-dupe by material
            return _agg(label, "ape")
    if "dealer" in msg and "dealer_name" in keys and "total_order_value" in keys:
        return _agg("dealer_name", "total_order_value")
    if "customer_name" in keys and "open_value" in keys:
        return _agg("customer_name", "open_value")
    if "plant_name" in keys and "shortage_qty" in keys:
        return _agg("plant_name", "shortage_qty")
    return rows[:15]


def _build_chart_from_rows(rows: list[dict], message: str = "") -> dict | None:
    """Infer a bar chart config from tabular rows (Order Lens columns)."""
    if not rows:
        return None
    keys = list(rows[0].keys())
    msg = (message or "").lower()

    def _is_number(v: object) -> bool:
        try:
            if v is None or v == "":
                return False
            float(v)
            return True
        except Exception:
            return False

    # Prefer domain-specific axes so charts match the question
    preferred: list[tuple[tuple[str, ...], tuple[str, ...], str, str]] = []
    if any(w in msg for w in ("reorder", "inventory", "shortage")):
        preferred.append((
            ("material_description", "material_key", "plant_name"),
            ("shortage_qty", "reorder_shortage_quantity", "stock_qty"),
            "#be123c",
            "Top reorder shortages",
        ))
    if any(w in msg for w in ("forecast", "outlier", "mape", "ape")):
        preferred.append((
            ("material_key", "material_description", "plant_name"),
            ("ape", "absolute_percentage_error", "mape"),
            "#7c3aed",
            "Forecast outliers (APE %)",
        ))
    if "dealer" in msg:
        preferred.append((
            ("dealer_name", "dealer_key"),
            ("total_order_value", "fulfilled_orders", "total_orders"),
            "#0369a1",
            "Dealer order value",
        ))
    preferred.append((
        ("customer_name", "sales_order_id", "plant_name", "material_description", "material_key", "dealer_name"),
        ("open_value", "total_order_value", "shortage_qty", "ape", "order_age_days", "fulfilled_orders"),
        "#0d7d72",
        "Visualization",
    ))

    x_key = None
    y_key = None
    color = "#0d7d72"
    title = "Visualization"

    for name_prefs, value_prefs, pref_color, pref_title in preferred:
        x_cand = next((k for k in name_prefs if k in keys), None)
        y_cand = next((k for k in value_prefs if k in keys), None)
        if x_cand and y_cand:
            x_key, y_key, color, title = x_cand, y_cand, pref_color, pref_title
            break

    if not x_key:
        for k in keys:
            vals = [r.get(k) for r in rows[:20]]
            if any(v is not None and str(v).strip() for v in vals) and not all(
                _is_number(v) for v in vals if v is not None and str(v).strip()
            ):
                x_key = k
                break
    if not y_key:
        for k in keys:
            vals = [r.get(k) for r in rows[:20]]
            if any(_is_number(v) for v in vals):
                y_key = k
                break

    if not x_key or not y_key or x_key == y_key:
        return None

    if x_key == "material_key" and "material_description" in keys:
        if any(str(r.get("material_description") or "").strip() for r in rows[:10]):
            x_key = "material_description"

    if title == "Visualization":
        title_by_y = {
            "open_value": "Open backlog by customer",
            "total_order_value": "Order value by dealer",
            "shortage_qty": "Reorder shortage quantity",
            "ape": "Forecast APE %",
            "order_age_days": "Order age (days)",
        }
        title = title_by_y.get(y_key, f"{y_key.replace('_', ' ').title()} by {x_key.replace('_', ' ')}")

    return {
        "type": "bar_horizontal" if len(rows) > 8 else "bar",
        "xKey": x_key,
        "yKey": y_key,
        "color": color,
        "title": title,
    }


def _sample_rows_for_question(q: str) -> list[dict]:
    """Use the P2P mart as the deterministic fallback when text-to-SQL is unavailable."""
    from app.services import p2p_service as p2p
    ql = q.lower()
    try:
        if any(x in ql for x in ("vendor", "supplier")):
            return p2p.get_vendors(limit=15).get("rows", [])
        if any(x in ql for x in ("cash", "discount", "payment", "grir")):
            return p2p.get_cash_overview()["payment_run"][:15]
        if any(x in ql for x in ("duplicate", "fraud")):
            return p2p.get_invoices(exception_code="DUPLICATE", limit=15)["rows"]
        return p2p.get_invoices(open_only=True, limit=15)["rows"]
    except Exception:
        return []


def _facts_digest() -> str:
    from app.services import p2p_service as p2p
    try:
        summary = p2p.get_summary()
        k = summary.get("kpis", {})
        return (
            f"P2P invoices: {k.get('invoice_count', 0)}; vendors: {k.get('vendor_count', 0)}; "
            f"spend: {k.get('total_spend', 0)}; open exposure: {k.get('open_exposure', 0)}; "
            f"overdue exposure: {k.get('overdue_exposure', 0)}; overdue invoices: {k.get('overdue_invoices', 0)}; "
            f"exceptions: {k.get('exception_invoices', 0)}; exception exposure: {k.get('exception_exposure', 0)}; "
            f"discount available: {k.get('discount_available', 0)}; duplicate payment amount: {k.get('duplicate_payment_amount', 0)}."
        )
    except Exception as exc:
        logger.warning("Unable to build P2P facts digest: %s", exc)
        return "No P2P facts are currently available."

def saved_insights(persona: str | None = None, limit: int = 50) -> list[dict]:
    wh = f"where PERSONA = '{persona.replace(chr(39), chr(39)+chr(39))}'" if persona else ""
    rows = run_query(f"""
        select TOP {int(limit)} INSIGHT_ID, CREATED_AT, CREATED_BY, PERSONA, PAGE, TITLE, QUESTION, SQL_TEXT
        from {qview("saved_insights")}
        {wh}
        order by CREATED_AT desc
    """)
    # Preserve O2C-style uppercase keys expected by Copilot UI helpers
    out = []
    for r in rows:
        out.append({
            "INSIGHT_ID": r.get("insight_id"),
            "TITLE": r.get("title"),
            "QUESTION": r.get("question"),
            "SQL_TEXT": r.get("sql_text"),
            "insight_id": r.get("insight_id"),
            "title": r.get("title"),
            "question": r.get("question"),
            "sql_text": r.get("sql_text"),
        })
    return out


def save_insight(
    title: str | dict,
    question: str | None = None,
    sql_text: str | None = None,
    persona: str | None = None,
    created_by: str = "app",
    page: str = "copilot",
) -> dict:
    if isinstance(title, dict):
        payload = title
        title = payload.get("title") or "Saved insight"
        question = payload.get("question") or ""
        sql_text = payload.get("sql_text")
        persona = payload.get("persona") or persona
        created_by = payload.get("created_by") or created_by
        page = payload.get("page") or page
    insight_id = _next_int_id(qview("saved_insights"), "INSIGHT_ID")
    return _onelake_write_or_readonly(
        "append",
        "ONELAKE_SAVED_INSIGHTS_PATH",
        [{
            "INSIGHT_ID": insight_id,
            "CREATED_AT": onelake_writer.utcnow(),
            "CREATED_BY": created_by,
            "PERSONA": persona or "ADMIN",
            "PAGE": page,
            "TITLE": title,
            "QUESTION": question or "",
            "VERIFIED_QUERY_NAME": None,
            "SQL_TEXT": sql_text or "",
            "TAGS": None,
        }],
    )


def delete_insight(insight_id: int, user_name: str = "app", is_admin: bool = False) -> dict:
    iid = int(insight_id)
    if not is_admin:
        rows = run_query(
            f"SELECT TOP (1) CREATED_BY FROM {qview('saved_insights')} WHERE INSIGHT_ID = %s",
            (iid,),
        )
        if not rows:
            return {"ok": False, "message": "Insight not found"}
        if str(rows[0].get("created_by") or "").lower() != str(user_name).lower():
            return {"ok": False, "message": "You can only delete your own insights"}
    return _onelake_write_or_readonly(
        "delete", "ONELAKE_SAVED_INSIGHTS_PATH", f"INSIGHT_ID = {iid}"
    )

def frequent_questions(persona: str | None = None, limit: int = 8) -> list[dict]:
    wh = f"where PERSONA = '{persona.replace(chr(39), chr(39)+chr(39))}'" if persona else ""
    # append-only write model (see copilot_chat) means multiple rows can
    # exist per (query, persona) - aggregate them here rather than assuming
    # one counter row per question.
    rows = run_query(f"""
        select TOP {int(limit)} NORMALIZED_QUERY, TYPE, sum(FREQUENCY) as frequency, max(LAST_ASKED_AT) as last_asked_at
        from {qview("genie_question_history")}
        {wh}
        group by NORMALIZED_QUERY, TYPE
        order by frequency desc, last_asked_at desc
    """)
    return [
        {
            "NORMALIZED_QUERY": r.get("normalized_query"),
            "TYPE": r.get("type") or "ANALYTICS",
            "FREQUENCY": r.get("frequency") or 0,
            "normalized_query": r.get("normalized_query"),
            "question": r.get("normalized_query"),
        }
        for r in rows
    ]


def most_frequent() -> list[dict]:
    rows = run_query(f"""
        select TOP 20 NORMALIZED_QUERY, TYPE, sum(FREQUENCY) as total_freq
        from {qview("genie_question_history")}
        group by NORMALIZED_QUERY, TYPE
        order by total_freq desc
    """)
    return [
        {
            "NORMALIZED_QUERY": r.get("normalized_query"),
            "TYPE": r.get("type") or "ANALYTICS",
            "TOTAL_FREQ": r.get("total_freq") or 0,
            "normalized_query": r.get("normalized_query"),
            "total_freq": r.get("total_freq") or 0,
        }
        for r in rows
    ]

# ---------------------------------------------------------------------------
# P2P Agent Console (Azure OpenAI + Fabric)
# ---------------------------------------------------------------------------

# Keep this contract aligned with the Snowflake reference.  The frontend uses
# these fields directly (notably sections.RECOMMENDATION and the snapshot
# queue_size/exposure/act_now values), so the backend swap must not change them.
AGENT_CATALOG: list[dict[str, Any]] = [
    {
        "key": "invoice_exception",
        "name": "Invoice Exception Agent",
        "tagline": "Clears the exception queue against the three-way match policy",
        "entity_label": "Invoice",
        "queue_label": "Open exceptions",
        "roles": ["admin", "ap_ops"],
        "dispositions": ["RELEASE", "HOLD", "RETURN TO VENDOR", "REQUEST CREDIT", "ESCALATE"],
        "actions": ["RECOMMENDED", "RELEASED", "HELD", "RETURNED_TO_VENDOR", "ESCALATED", "DISMISSED"],
        "tools": ["invoice_exception_queue", "invoice_context", "three_way_match", "policy_search"],
    },
    {
        "key": "payment_optimization",
        "name": "Payment Optimization Agent",
        "tagline": "Composes the payment run and defends the discount and DPO targets",
        "entity_label": "Invoice",
        "queue_label": "Discount windows",
        "roles": ["admin", "treasury"],
        "dispositions": [
            "PAY IMMEDIATELY", "PAY EARLY FOR DISCOUNT", "PAY THIS WEEK",
            "SCHEDULE NEXT RUN", "HOLD FOR CASH",
        ],
        "actions": ["RECOMMENDED", "APPROVED", "SCHEDULED", "HELD", "DISMISSED"],
        "tools": ["discount_capture_queue", "payment_run_proposal", "cash_commitment", "dpo_flux", "policy_search"],
    },
    {
        "key": "duplicate_fraud",
        "name": "Duplicate & Fraud Agent",
        "tagline": "Blocks double payment and routes recovery on what already went out",
        "entity_label": "Invoice pair",
        "queue_label": "Duplicate candidates",
        "roles": ["admin", "ap_ops", "treasury"],
        "dispositions": ["CONFIRMED DUPLICATE", "LIKELY DUPLICATE", "LEGITIMATE"],
        "actions": ["RECOMMENDED", "BLOCKED", "RECOVERY_RAISED", "CLEARED", "DISMISSED"],
        "tools": ["duplicate_risk_queue", "vendor_risk_profile", "invoice_context", "policy_search"],
    },
    {
        "key": "grir_reconciliation",
        "name": "GR/IR Reconciliation Agent",
        "tagline": "Clears aged goods-receipt / invoice-receipt balances",
        "entity_label": "PO line",
        "queue_label": "Open GR/IR items",
        "roles": ["admin", "ap_ops", "procurement"],
        "dispositions": ["CHASE INVOICE", "POST RECEIPT", "CORRECT QUANTITY", "WRITE BACK", "ESCALATE"],
        "actions": ["RECOMMENDED", "CHASED", "CORRECTED", "ESCALATED", "DISMISSED"],
        "tools": ["grir_queue", "three_way_match", "vendor_risk_profile", "policy_search"],
    },
    {
        "key": "vendor_performance",
        "name": "Vendor Performance Agent",
        "tagline": "Scores suppliers and surfaces non-PO and off-contract spend",
        "entity_label": "Vendor",
        "queue_label": "Vendors at risk",
        "roles": ["admin", "procurement", "exec"],
        "dispositions": ["MEETS EXPECTATION", "PERFORMANCE REVIEW", "WATCHLIST", "CONSOLIDATE", "ESCALATE"],
        "actions": ["RECOMMENDED", "WATCHLISTED", "REVIEW_SCHEDULED", "CLEARED", "DISMISSED"],
        "tools": ["vendor_scorecard", "vendor_risk_profile", "spend_compliance_queue", "policy_search"],
    },
]

AGENT_POLICY_DOMAIN = {
    "invoice_exception": "INVOICE",
    "payment_optimization": "PAYMENT",
    "duplicate_fraud": "DUPLICATE",
    "grir_reconciliation": "GRIR",
    "vendor_performance": "VENDOR",
}

AGENT_OUTPUT_DIRECTIVE = (
    "Respond with EXACTLY these four markdown headings, in this order, and nothing else:\n\n"
    "### ASSESSMENT\n"
    "2-3 sentences stating the problem with concrete facts from the JSON - ids, vendor, "
    "amounts, ages and risk reasons. Never leave this empty.\n\n"
    "### RECOMMENDATION\n"
    "One line starting with an ALL-CAPS disposition, then a colon, then the justification. "
    "Use one of the allowed dispositions listed for this agent.\n\n"
    "### ACTIONS\n"
    "Exactly 3 numbered lines. Each starts with an ALL-CAPS action name, a colon, the detail, "
    "then 'Owner:' and the accountable role.\n\n"
    "### POLICY BASIS\n"
    "1-2 bullets citing only policy excerpts supplied in the prompt. Do not invent policy.\n"
)


def agent_catalog() -> list[dict]:
    return AGENT_CATALOG


def _catalog_entry(agent_key: str) -> dict:
    entry = next((a for a in AGENT_CATALOG if a["key"] == agent_key), None)
    if entry is None:
        raise ValueError(f"Unknown agent key: {agent_key}")
    return entry


def _policy_block(domain: str | None, query: str, limit: int = 4) -> str:
    try:
        chunks = policy_lookup(query, domain, limit)
    except Exception:
        return ""
    lines = []
    for c in chunks:
        text = str(c.get("chunk") or c.get("clause_text") or "").strip().replace("\n", " ")
        if text:
            clause = c.get("chunk_id") or c.get("clause_id") or ""
            prefix = f"{clause}: " if clause else ""
            lines.append(f"- {prefix}{text[:700]}")
    return "\n".join(lines)


def recent_actions(agent_key: str | None = None, limit: int = 25) -> list[dict]:
    where = (
        f"WHERE AGENT_KEY = '{agent_key.replace(chr(39), chr(39)+chr(39))}'"
        if agent_key else ""
    )
    try:
        rows = run_query(f"""
            SELECT TOP {int(limit)}
                   REVIEW_ID AS action_id, AGENT_KEY AS agent_key,
                   ENTITY_TYPE AS entity_type, ENTITY_ID AS entity_id,
                   COMPANY_CODE AS company_code, ACTION_TAKEN AS action_taken,
                   EXPOSURE_AMOUNT AS exposure_amount, NOTES AS notes,
                   SOURCE AS source, ACTED_BY AS acted_by, CREATED_AT AS acted_at,
                   RECOMMENDATION AS recommendation
            FROM {qview("agent_review_log")}
            {where}
            ORDER BY CREATED_AT DESC
        """)
        return rows
    except Exception:
        # Keep the console usable when the optional action-log Delta table has
        # not yet been configured.
        return []


def console_snapshot() -> dict:
    """Return the same per-agent queue metrics consumed by the reference UI."""
    cache_key = "agent_snapshot"
    cached = None
    try:
        cached = __import__("app.db", fromlist=["cache_get"]).cache_get(cache_key)
    except Exception:
        pass
    if cached is not None:
        return cached

    rows = run_query(f"""
        SELECT 'invoice_exception' AS agent_key,
               COUNT(*) AS queue_size,
               ROUND(SUM(COALESCE(INVOICE_AMOUNT_LOCAL, 0)), 2) AS exposure,
               SUM(CASE WHEN PRIORITY_BAND = 'ACT_NOW' THEN 1 ELSE 0 END) AS act_now
        FROM {qview("invoice_exception_worklist_vw")}
        UNION ALL
        SELECT 'payment_optimization',
               COUNT(*),
               ROUND(SUM(COALESCE(DISCOUNT_VALUE, 0)), 2),
               SUM(CASE WHEN PRIORITY_BAND = 'ACT_NOW' THEN 1 ELSE 0 END)
        FROM {qview("discount_capture_worklist_vw")}
        WHERE DISCOUNT_WINDOW_STATUS <> 'EXPIRED'
        UNION ALL
        SELECT 'duplicate_fraud',
               COUNT(*),
               ROUND(SUM(EXPOSURE_AMOUNT), 2),
               SUM(CASE WHEN PRIORITY_BAND = 'ACT_NOW' THEN 1 ELSE 0 END)
        FROM {qview("duplicate_invoice_risk_vw")}
        WHERE RISK_SCORE >= 60
        UNION ALL
        SELECT 'grir_reconciliation',
               COUNT(*),
               ROUND(SUM(GRIR_EXPOSURE), 2),
               SUM(CASE WHEN PRIORITY_BAND = 'ACT_NOW' THEN 1 ELSE 0 END)
        FROM {qview("grir_reconciliation_worklist_vw")}
        UNION ALL
        SELECT 'vendor_performance',
               COUNT(*),
               ROUND(SUM(OPEN_EXPOSURE), 2),
               SUM(CASE WHEN RISK_BAND = 'HIGH_RISK' THEN 1 ELSE 0 END)
        FROM {qview("vendor_risk_profile_vw")}
        WHERE RISK_BAND IN ('HIGH_RISK', 'ELEVATED')
    """)
    by_key = {r.get("agent_key"): r for r in rows}
    out = {
        "agents": [
            {
                **entry,
                **by_key.get(entry["key"], {
                    "queue_size": 0, "exposure": 0, "act_now": 0
                }),
            }
            for entry in AGENT_CATALOG
        ],
        "recent_actions": recent_actions(limit=25),
    }
    try:
        from app.db import cache_set
        cache_set(cache_key, out)
    except Exception:
        pass
    return out


def agent_queue(
    agent_key: str,
    limit: int = 25,
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
) -> list[dict]:
    _catalog_entry(agent_key)
    from app.services import p2p_service as p2p

    if agent_key == "invoice_exception":
        return p2p.get_invoices(
            company=company, vendor_id=vendor_id, exception_code=exception_code,
            priority_band=priority_band, open_only=True, limit=limit,
        )["rows"]

    if agent_key == "payment_optimization":
        return p2p.get_discount_worklist(
            company=company, window_status=window_status, limit=limit,
        )

    if agent_key == "duplicate_fraud":
        score = int(min_score or 60)
        vendor_f = f" AND VENDOR_ID = '{esc(str(vendor_id))}'" if vendor_id else ""
        return run_query(f"""
            SELECT TOP {int(limit)}
                   INVOICE_NUMBER AS invoice_number,
                   MATCHED_INVOICE_NUMBER AS matched_invoice_number,
                   COMPANY_CODE AS company_code,
                   VENDOR_ID AS vendor_id,
                   VENDOR_NAME AS vendor_name,
                   INVOICE_AMOUNT_LOCAL AS invoice_amount,
                   MATCHED_AMOUNT AS matched_amount,
                   ROUND(AMOUNT_DELTA, 2) AS amount_delta,
                   POSTING_DATE AS posting_date,
                   MATCHED_POSTING_DATE AS matched_posting_date,
                   POSTING_DATE_GAP_DAYS AS posting_date_gap_days,
                   SAME_PO AS same_po,
                   INVOICE_STATUS AS invoice_status,
                   MATCHED_STATUS AS matched_status,
                   DUPLICATE_PAYMENT_AMOUNT AS duplicate_payment_amount,
                   RISK_SCORE AS risk_score,
                   RISK_BAND AS risk_band,
                   RISK_REASON AS risk_reason,
                   PRIORITY_BAND AS priority_band,
                   ROUND(EXPOSURE_AMOUNT, 2) AS exposure_amount
            FROM {qview("duplicate_invoice_risk_vw")}
            WHERE RISK_SCORE >= {score}{vendor_f}
            ORDER BY RISK_SCORE DESC, EXPOSURE_AMOUNT DESC
        """)

    if agent_key == "grir_reconciliation":
        return p2p.get_grir_worklist(
            vendor_id=vendor_id, age_bucket=age_bucket,
            min_age_days=int(min_age_days or 0), limit=limit,
        )

    if agent_key == "vendor_performance":
        return p2p.get_vendors(
            risk_band=risk_band, vendor_tier=vendor_tier,
            sort="risk", limit=limit,
        )["rows"]

    return []


def _entity_facts(agent_key: str, entity_id: str, extra: dict | None = None) -> dict:
    """Load the same entity-level facts as the Snowflake implementation."""
    extra = extra or {}
    if agent_key == "invoice_exception":
        from app.services import p2p_service as p2p
        detail = p2p.get_invoice_detail(entity_id, extra.get("company_code"))
        return {
            "invoice": detail.get("header"),
            "three_way_match": detail.get("match"),
            "status_history": (detail.get("status_history") or [])[-6:],
            "duplicate_candidates": detail.get("duplicates"),
        }

    if agent_key == "payment_optimization":
        rows = run_query(f"""
            SELECT TOP (1) d.*, p.RECOMMENDED_ACTION, p.TARGET_PAYMENT_DATE, p.FORECAST_BUCKET
            FROM {qview("discount_capture_worklist_vw")} d
            LEFT JOIN {qview("payment_run_worklist_vw")} p
              ON p.INVOICE_NUMBER = d.INVOICE_NUMBER
             AND p.COMPANY_CODE = d.COMPANY_CODE
            WHERE d.INVOICE_NUMBER = '{esc(entity_id)}'
        """)
        dpo = run_query(f"""
            SELECT TOP (3) CONCAT(YEAR, '-', RIGHT('0' + CAST(MONTH AS varchar(2)), 2)) AS month,
                   ROUND(WEIGHTED_DPO, 1) AS weighted_dpo
            FROM {qview("weighted_days_payable_outstanding_vw")}
            ORDER BY YEAR DESC, MONTH DESC
        """)
        return {"invoice": rows[0] if rows else None, "recent_dpo": dpo}

    if agent_key == "duplicate_fraud":
        pair = run_query(f"""
            SELECT TOP (1) *
            FROM {qview("duplicate_invoice_risk_vw")}
            WHERE INVOICE_NUMBER = '{esc(entity_id)}'
               OR MATCHED_INVOICE_NUMBER = '{esc(entity_id)}'
            ORDER BY RISK_SCORE DESC
        """)
        vendor = []
        if pair and pair[0].get("vendor_id"):
            vendor = run_query(f"""
                SELECT TOP (1)
                       VENDOR_ID, VENDOR_NAME, INVOICE_COUNT,
                       DUPLICATE_PAYMENT_INVOICES,
                       ROUND(DUPLICATE_PAYMENT_AMOUNT, 2) AS DUPLICATE_PAYMENT_AMOUNT,
                       TOTAL_DISPUTES, NON_PO_INVOICES,
                       ROUND(RISK_SCORE, 1) AS RISK_SCORE, RISK_BAND
                FROM {qview("vendor_risk_profile_vw")}
                WHERE VENDOR_ID = '{esc(str(pair[0].get("vendor_id")))}'
            """)
        return {"pair": pair[0] if pair else None, "vendor": vendor[0] if vendor else None}

    if agent_key == "grir_reconciliation":
        po_item = extra.get("po_item")
        item_f = f" AND PO_ITEM = {int(po_item)}" if po_item not in (None, "") else ""
        rows = run_query(f"""
            SELECT TOP (5) *
            FROM {qview("grir_reconciliation_worklist_vw")}
            WHERE PURCHASE_ORDER_NUMBER = '{esc(entity_id)}'{item_f}
            ORDER BY PRIORITY_SCORE DESC
        """)
        return {"grir_items": rows}

    if agent_key == "vendor_performance":
        from app.services import p2p_service as p2p
        detail = p2p.get_vendor_detail(entity_id)
        return {
            "vendor": detail.get("profile"),
            "compliance": (detail.get("compliance") or [])[:8],
            "grir": (detail.get("grir") or [])[:8],
            "duplicates": (detail.get("duplicates") or [])[:5],
        }
    return {}


def _parse_agent_sections(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("###"):
            if current:
                out[current] = "\n".join(buffer).strip()
            heading = stripped.lstrip("#").strip().upper()
            current = next(
                (s for s in ("ASSESSMENT", "RECOMMENDATION", "ACTIONS", "POLICY BASIS")
                 if s in heading),
                heading,
            )
            buffer = []
        elif current:
            buffer.append(line)
    if current:
        out[current] = "\n".join(buffer).strip()
    return out


def agent_recommend(
    agent_key: str,
    entity_id: str,
    context: dict | None = None,
    use_live_agent: bool | None = None,
) -> dict:
    entry = _catalog_entry(agent_key)
    context = context or {}
    facts = _entity_facts(agent_key, entity_id, context)
    if not any(facts.values()):
        raise ValueError(f"No data found for {entry['entity_label']} {entity_id}")

    domain = AGENT_POLICY_DOMAIN.get(agent_key)
    policy = _policy_block(domain, f"{entry['name']} {entry['tagline']}", 4)
    prompt = (
        "You are a Procure-to-Pay operational agent running over Microsoft Fabric.\n"
        "Use ONLY the supplied Fabric facts. Never invent values or policies.\n"
        f"Agent role: {entry['name']}\n"
        f"Task: {entry['tagline']}\n"
        f"Allowed dispositions: {', '.join(entry['dispositions'])}\n\n"
        f"Entity: {entry['entity_label']} {entity_id}\n"
        f"Facts JSON: {json.dumps(facts, default=str, ensure_ascii=False)[:9000]}\n"
        f"Policy excerpts:\n{policy or '- (no policy table configured)'}\n\n"
        f"{AGENT_OUTPUT_DIRECTIVE}"
    )
    try:
        text = _llm_complete(prompt)
        source = "azure_openai"
        if not text:
            raise RuntimeError("Azure OpenAI returned empty content")
    except Exception as exc:
        # Preserve a useful, deterministic result instead of changing the API
        # contract when the model is temporarily unavailable.
        assessment = f"Facts were loaded for {entry['entity_label']} {entity_id}, but Azure OpenAI did not return a recommendation."
        disposition = entry["dispositions"][0]
        text = (
            f"### ASSESSMENT\n{assessment}\n\n"
            f"### RECOMMENDATION\n{disposition}: Review the supplied Fabric facts before acting.\n\n"
            f"### ACTIONS\n"
            f"1. FACT REVIEW: Validate the record. Owner: Process owner.\n"
            f"2. EXCEPTION REVIEW: Confirm supporting evidence. Owner: Process owner.\n"
            f"3. ESCALATE: Escalate if the evidence is incomplete. Owner: Process owner.\n\n"
            f"### POLICY BASIS\n- No policy excerpt was available."
        )
        source = "azure_openai_fallback"
        model_error = str(exc)
    sections = _parse_agent_sections(text)
    result = {
        "agent_key": agent_key,
        "agent_name": entry["name"],
        "entity_id": entity_id,
        "entity_label": entry["entity_label"],
        "response": text,
        "sections": sections,
        "facts": facts,
        "policy": policy,
        "source": source,
        "agent_used": source == "azure_openai",
    }
    if source != "azure_openai":
        result["agent_error"] = model_error
    return result


def log_agent_action(
    agent_key: str,
    entity_type: str,
    entity_id: str,
    action_taken: str,
    acted_by: str,
    company_code: str | None = None,
    exposure_amount: float | None = None,
    recommendation: str | None = None,
    notes: str | None = None,
    source: str | None = None,
) -> dict:
    review_id = _next_int_id(qview("agent_review_log"), "REVIEW_ID")
    payload = {
        "REVIEW_ID": review_id,
        "AGENT_KEY": agent_key,
        "ENTITY_TYPE": entity_type,
        "ENTITY_ID": entity_id,
        "ACTION_TAKEN": action_taken,
        "ACTED_BY": acted_by,
        "COMPANY_CODE": company_code,
        "EXPOSURE_AMOUNT": exposure_amount,
        "RECOMMENDATION": recommendation,
        "NOTES": notes,
        "SOURCE": source or "ui",
        "CREATED_AT": onelake_writer.utcnow(),
    }
    try:
        result = _onelake_write_or_readonly("append", "ONELAKE_AGENT_REVIEW_LOG_PATH", [payload])
        return {"ok": True, **(result if isinstance(result, dict) else {})}
    except Exception as exc:
        logger.exception("Agent action logging failed")
        return {"ok": False, "message": str(exc)}


def policy_catalog() -> list[dict]:
    table = os.getenv("FABRIC_POLICY_TABLE", "").strip()
    if not table:
        return []
    try:
        safe_table = table.replace("_", "")
        if not safe_table.isalnum():
            raise ValueError("FABRIC_POLICY_TABLE contains invalid characters")
        rows = run_query(f"""
            SELECT CLAUSE_ID AS clause_id, POLICY_DOMAIN AS policy_domain,
                   CLAUSE_TITLE AS clause_title, CLAUSE_TEXT AS clause_text,
                   THRESHOLD_VALUE AS threshold_value, OWNER_ROLE AS owner_role
            FROM {qview(table)}
            ORDER BY POLICY_DOMAIN, CLAUSE_ID
        """)
        return rows
    except Exception:
        return []


def policy_lookup(query: str, domain: str | None = None, limit: int = 5) -> list[dict]:
    try:
        from app.services.agent_runtime import search_policy
        return search_policy(query, domain, limit)
    except Exception:
        return []
