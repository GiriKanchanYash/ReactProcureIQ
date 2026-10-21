"""Azure OpenAI runtime for the P2P agent console.

This module intentionally contains no legacy AI dependencies. Fabric is
the source of truth; Azure OpenAI is used only to reason over facts already
retrieved from Fabric.
"""
from __future__ import annotations

import os
from typing import Any

from openai import AzureOpenAI, OpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

from app.db import qview, run_query

_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip()
_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview").strip()
_client: AzureOpenAI | OpenAI | None = None


def _client_get():
    global _client
    if _client is not None:
        return _client
    if not _ENDPOINT or not _DEPLOYMENT:
        raise RuntimeError("Azure OpenAI endpoint/deployment is not configured")
    key = os.getenv("AZURE_OPENAI_API_KEY", "").strip()
    if key:
        _client = AzureOpenAI(azure_endpoint=_ENDPOINT, api_key=key, api_version=_API_VERSION)
    else:
        provider = get_bearer_token_provider(DefaultAzureCredential(), "https://ai.azure.com/.default")
        _client = OpenAI(base_url=_ENDPOINT.rstrip("/") + "/openai/v1/", api_key=provider)
    return _client


def complete(prompt: str, max_tokens: int = 900) -> str:
    response = _client_get().chat.completions.create(
        model=_DEPLOYMENT,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=max_tokens,
    )
    return (response.choices[0].message.content or "").strip()


def search_policy(query: str, domain: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
    # Optional policy table. This is deliberately data-driven and does not
    # reference legacy warehouse legacy AI search services.
    table = os.getenv("FABRIC_POLICY_TABLE", "").strip()
    if not table:
        return []
    if not table.replace("_", "").isalnum():
        raise ValueError("FABRIC_POLICY_TABLE contains invalid characters")
    where = "WHERE 1=1"
    params: tuple[Any, ...] = (query,)
    if domain:
        where += " AND POLICY_DOMAIN = ?"
        params = (query, domain)
    sql = f"""
        SELECT TOP (?) POLICY_DOMAIN AS policy_domain,
               CLAUSE_ID AS chunk_id,
               CLAUSE_TITLE AS title,
               CLAUSE_TEXT AS chunk
        FROM {qview(table)}
        {where} AND (CLAUSE_TITLE LIKE '%' + ? + '%' OR CLAUSE_TEXT LIKE '%' + ? + '%')
        ORDER BY CLAUSE_ID
    """
    # Keep parameterization; avoid interpolating the user's query.
    params = (int(limit), domain, query, query) if domain else (int(limit), query, query)
    try:
        return run_query(sql, params)
    except Exception:
        return []
