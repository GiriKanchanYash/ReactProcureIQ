"""
Fabric Warehouse connection layer. Provides the same qcol()/qview()/IM/
run_query()/run_execute() surface as the original legacy warehouse app/db.py so
sales_service.py and ai_service.py needed only syntax-level edits (T-SQL vs
legacy warehouse SQL), not a rewrite of their calling code.

Per the project's fixed environment-variable list, only these are read here:
FABRIC_SQL_SERVER, FABRIC_DATABASE, FABRIC_SCHEMA, AZURE_TENANT_ID,
AZURE_CLIENT_ID, AZURE_CLIENT_SECRET.
"""
from __future__ import annotations

import logging
import os
import struct
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from functools import lru_cache

import pyodbc
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

logger = logging.getLogger("orderlens.db")

# SQL_COPT_SS_ACCESS_TOKEN - the ODBC connection attribute msodbcsql uses to
# accept a raw Azure AD access token instead of a UID/PWD pair.
_SQL_COPT_SS_ACCESS_TOKEN = 1256
_FABRIC_TOKEN_SCOPE = "https://database.windows.net/.default"

# Fabric's SQL analytics endpoint is unreliable with in-connection-string
# "Authentication=ActiveDirectoryServicePrincipal" over ODBC Driver 18 -- it
# frequently hangs and fails with a generic HYT00 login timeout instead of a
# clean auth error. Acquiring the AAD token ourselves via azure-identity and
# passing it as SQL_COPT_SS_ACCESS_TOKEN is the path Microsoft recommends for
# Fabric and is what reliably works.
_token_lock = threading.Lock()
_cached_credential: DefaultAzureCredential | None = None
_cached_token: str | None = None
_cached_token_expires_at: float = 0.0

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# pyodbc SQLSTATE prefixes we can give a specific, actionable message for.
# See: https://learn.microsoft.com/sql/odbc/reference/appendixes/appendix-a-odbc-error-codes
_SQLSTATE_HINTS: dict[str, str] = {
    "IM002": (
        "ODBC Driver 18 for SQL Server is not installed (or not registered) on this "
        "machine. Install it: "
        "https://learn.microsoft.com/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server"
    ),
    "IM003": (
        "The ODBC driver could not be loaded. Re-install 'ODBC Driver 18 for SQL Server' "
        "for this OS/architecture."
    ),
    "08001": (
        "Could not reach the Fabric SQL endpoint (network/firewall/DNS). Confirm "
        "FABRIC_SQL_SERVER is correct, the Fabric Warehouse/Lakehouse SQL endpoint is "
        "enabled, and this machine's IP/network is allowed (VPN or workspace firewall rules)."
    ),
    "HYT00": (
        "Connection to the Fabric SQL endpoint timed out. Usually a network/firewall "
        "issue rather than bad credentials."
    ),
    "28000": (
        "Fabric rejected the login. The service principal (AZURE_CLIENT_ID/"
        "AZURE_CLIENT_SECRET/AZURE_TENANT_ID) is invalid, the secret has expired, or "
        "the app registration has not been added as a member with access to this "
        "Warehouse/Lakehouse in the Fabric workspace."
    ),
    "42000": (
        "Fabric denied the query (permissions or invalid object name). Confirm the "
        "service principal has at least Read/ReadData on this Warehouse and that "
        "FABRIC_SCHEMA / table names match what actually exists."
    ),
}


class FabricConnectionError(RuntimeError):
    """Raised when we can't open or use a connection to the Fabric SQL endpoint,
    with a human-actionable hint attached (as opposed to a raw pyodbc traceback)."""


def _hint_for(exc: pyodbc.Error) -> str:
    sqlstate = ""
    if exc.args:
        sqlstate = str(exc.args[0])
    for prefix, hint in _SQLSTATE_HINTS.items():
        if sqlstate.startswith(prefix):
            return hint
    msg = str(exc).lower()
    if "login failed" in msg or "cannot open server" in msg:
        return _SQLSTATE_HINTS["28000"]
    if "denied on the requested resource" in msg or "external policy action" in msg:
        return _SQLSTATE_HINTS["42000"]
    return "Unrecognized Fabric/ODBC error - see the underlying message for details."


def _load_conn_params() -> dict[str, str]:
    load_dotenv(_ENV_PATH, override=True)
    return {
        "server": os.getenv("FABRIC_SQL_SERVER", "").strip(),
        "database": os.getenv("FABRIC_DATABASE", "").strip(),
        "schema": os.getenv("FABRIC_SCHEMA", "").strip(),
        "tenant_id": os.getenv("AZURE_TENANT_ID", "").strip(),
        "client_id": os.getenv("AZURE_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("AZURE_CLIENT_SECRET", "").strip(),
    }


_P = _load_conn_params()
SCHEMA = _P["schema"] or "dbo"
Database = _P["database"]
IM = SCHEMA  # matches the legacy implementation's IM (schema-qualified prefix) usage


# Backwards-compatible names used by older modules.
DB = Database
AGENT = f"{Database}.{SCHEMA}"
AGENT_SCHEMA = SCHEMA

_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = lambda: float(os.getenv("P2P_CACHE_TTL_SECONDS", "300"))

def cache_get(key: str):
    now = time.time()
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if not item:
            return None
        expires, value = item
        if expires <= now:
            _CACHE.pop(key, None)
            return None
        return value

def cache_set(key: str, value):
    with _CACHE_LOCK:
        _CACHE[key] = (time.time() + _CACHE_TTL(), value)
    return value

def cache_clear() -> int:
    with _CACHE_LOCK:
        n = len(_CACHE)
        _CACHE.clear()
    return n

def esc(value: object) -> str:
    """Escape a literal used by legacy dynamically-built SELECTs."""
    return str(value).replace("'", "''")

def run_query_on_conn(conn, sql: str, params: tuple | None = None) -> list[dict]:
    sql = _normalize_fabric_sql(sql.replace("%s", "?"))
    cur = conn.cursor()
    cur.execute(sql, params or ())
    cols = [c[0] for c in cur.description] if cur.description else []
    return [_normalize_row(dict(zip(cols, r))) for r in cur.fetchall()]


def _build_conn_string(p: dict[str, str]) -> str:
    missing = [k for k in ("server", "database") if not p.get(k)]
    if missing:
        raise RuntimeError(f"Fabric connection details missing from .env: {', '.join(missing)}")
    # No UID/PWD/Authentication= here on purpose - auth is done via an AAD
    # access token passed through attrs_before (see _get_access_token_struct).
    return (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server=tcp:{p['server']},1433;"
        f"Database={p['database']};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )


def _get_access_token_struct(p: dict[str, str]) -> bytes:
    """Returns the SQL_COPT_SS_ACCESS_TOKEN struct pyodbc's attrs_before needs,
    fetching a fresh AAD token only when the cached one is near expiry."""
    global _cached_credential, _cached_token, _cached_token_expires_at
    with _token_lock:
        now = time.time()
        if _cached_token and now < _cached_token_expires_at - 60:
            token = _cached_token
        else:
            if _cached_credential is None:
                _cached_credential = DefaultAzureCredential(
                    exclude_interactive_browser_credential=True
                )
            try:
                aad_token = _cached_credential.get_token(_FABRIC_TOKEN_SCOPE)
            except ClientAuthenticationError as exc:
                logger.error("Azure AD token acquisition failed: %s", exc)
                raise FabricConnectionError(
                    "Azure identity could not acquire a Fabric SQL token. "
                    "Use Managed Identity in Azure or configure AZURE_TENANT_ID/"
                    "AZURE_CLIENT_ID/AZURE_CLIENT_SECRET locally. "
                    f"(raw: {exc})"
                ) from exc
            token = aad_token.token
            _cached_token = token
            _cached_token_expires_at = aad_token.expires_on

    token_bytes = token.encode("utf-16-le")
    return struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)


@contextmanager
def get_connection():
    p = _load_conn_params()
    global SCHEMA, IM
    SCHEMA = p["schema"] or SCHEMA
    IM = SCHEMA
    token_struct = _get_access_token_struct(p)
    try:
        conn = pyodbc.connect(
            _build_conn_string(p),
            attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: token_struct},
            timeout=30,
        )
    except pyodbc.Error as exc:
        hint = _hint_for(exc)
        logger.error(
            "Fabric connection failed (server=%s db=%s): %s | hint: %s",
            p.get("server"), p.get("database"), exc, hint,
        )
        raise FabricConnectionError(f"{hint} (raw: {exc})") from exc
    try:
        yield conn
    except pyodbc.Error as exc:
        hint = _hint_for(exc)
        logger.error("Fabric query failed: %s | hint: %s", exc, hint)
        raise FabricConnectionError(f"{hint} (raw: {exc})") from exc
    finally:
        conn.close()


def _normalize_row(row: dict) -> dict:
    return {str(k).lower(): v for k, v in row.items()}


def _normalize_fabric_sql(sql: str) -> str:
    """Translate a small set of legacy legacy warehouse constructs into Fabric T-SQL."""
    import re
    s = sql
    s = re.sub(r"(\'[^\']*\')::DATE\b", r"CAST(\1 AS date)", s, flags=re.I)
    s = re.sub(r"\s+NULLS\s+LAST\b", "", s, flags=re.I)

    # LIMIT n OFFSET m is equivalent to SQL Server OFFSET/FETCH when an ORDER BY
    # exists (all paged application queries already provide one).
    s = re.sub(
        r"\bLIMIT\s+(\d+)\s+OFFSET\s+(\d+)\b",
        lambda m: f"OFFSET {m.group(2)} ROWS FETCH NEXT {m.group(1)} ROWS ONLY",
        s, flags=re.I,
    )

    # Remaining LIMIT n clauses are either ordered queries or simple detail
    # lookups. Convert ordered ones to OFFSET/FETCH; for unordered statements,
    # inject TOP into the last SELECT in the statement.
    while True:
        m = re.search(r"\bLIMIT\s+(\d+)\b", s, flags=re.I)
        if not m:
            break
        n = m.group(1)
        before = s[:m.start()]
        if re.search(r"\bORDER\s+BY\b", before, flags=re.I):
            replacement = f"OFFSET 0 ROWS FETCH NEXT {n} ROWS ONLY"
            s = s[:m.start()] + replacement + s[m.end():]
            continue
        selects = list(re.finditer(r"\bSELECT\b", before, flags=re.I))
        if not selects:
            s = s[:m.start()] + s[m.end():]
            continue
        sel = selects[-1]
        pos = sel.end()
        s = s[:pos] + f" TOP ({n})" + s[pos:m.start()] + s[m.end():]
    return s

def run_query(sql: str, params: tuple | None = None) -> list[dict]:
    sql = _normalize_fabric_sql(sql.replace("%s", "?"))
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        cols = [c[0] for c in cur.description] if cur.description else []
        return [_normalize_row(dict(zip(cols, r))) for r in cur.fetchall()]


def run_execute(sql: str, params: tuple | None = None) -> None:
    """This is a real Warehouse (confirmed: SAP_STG/RAW_VAULT/INFORMATION_MART
    and sys.managed_delta_tables are present), so normal T-SQL INSERT/UPDATE/
    DELETE work directly here -- no OneLake/delta-rs workaround needed."""
    sql = sql.replace("%s", "?")
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        conn.commit()


def qview(name: str) -> str:
    """Schema-qualified table reference. Tables are flat (no BASE_LAYER joins
    needed) so this just prefixes the configured schema."""
    return f"{Database}.{SCHEMA}.{name.strip().strip(chr(34)).lower()}"


def qcol(name: str, view: str | None = None) -> str:
    """Kept for call-site compatibility with the legacy implementation's
    qcol("col") / qcol("col", "some_view") calls. Fabric/T-SQL doesn't need
    legacy warehouse's quoted-vs-unquoted alias workaround, so this just normalizes
    to a plain lowercase column name. The `view` argument is accepted but
    unused."""
    return name.strip().strip('"').lower()