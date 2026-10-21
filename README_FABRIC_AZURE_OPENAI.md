# ProcureIQ — Fabric + Azure OpenAI

This package is the cleaned backend/static bundle for the ProcureIQ P2P application.

## What was fixed

- Removed the broken `sales` router import; the API now mounts the P2P router.
- Removed Snowflake/Cortex runtime dependencies from the agent path.
- Azure OpenAI is used for Copilot and P2P recommendations.
- Azure OpenAI supports Microsoft Entra ID via `DefaultAzureCredential`; an API key is only an optional local-development fallback.
- Fabric SQL access uses Microsoft Entra tokens and supports managed identity/service-principal deployment.
- Added the missing Fabric DB cache/query helpers used by the P2P service.
- Added a Fabric SQL compatibility layer for legacy `LIMIT`, `NULLS LAST`, and similar query constructs.
- Replaced the missing YAML text-to-SQL catalog with a dynamic allow-list backed by `INFORMATION_SCHEMA`.
- Fixed router/service signature mismatches for Copilot, saved insights, agent actions, and frequent questions.
- Added Fabric/OneLake token authentication for Delta writes.
- Kept the reference demo login available by default because the React frontend is intentionally unchanged and its existing login flow calls `/api/auth/login`.
- Removed the uploaded `.env` and all embedded credentials from the deployable package.
- Added a Dockerfile that installs Microsoft ODBC Driver 18.

## Environment

Copy `.env.example` to `.env` for local development. In Azure App Service, Container Apps, AKS, etc., prefer platform application settings / managed identity instead of shipping `.env`.

Required:

- `FABRIC_SQL_SERVER`
- `FABRIC_DATABASE`
- `FABRIC_SCHEMA`
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_DEPLOYMENT`

For local service-principal authentication, also set:

- `AZURE_TENANT_ID`
- `AZURE_CLIENT_ID`
- `AZURE_CLIENT_SECRET`

For Azure-hosted production, prefer a managed identity and grant it access to the Fabric workspace/item and Azure OpenAI resource.

## Azure OpenAI

The application uses:

- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_DEPLOYMENT`
- `AZURE_OPENAI_API_VERSION`

If `AZURE_OPENAI_API_KEY` is absent, the code uses Microsoft Entra authentication with `DefaultAzureCredential`.

The Azure identity needs permission to invoke the deployed model (for example, the appropriate Azure OpenAI/Fabric AI resource role).

## Fabric

The SQL endpoint must allow the application identity to connect and read the curated analytical views.

The app expects these P2P views in `FABRIC_SCHEMA`:

- `INVOICE_360_VW`
- `INVOICE_EXCEPTION_WORKLIST_VW`
- `THREE_WAY_MATCH_EXCEPTION_VW`
- `DISCOUNT_CAPTURE_WORKLIST_VW`
- `PAYMENT_RUN_WORKLIST_VW`
- `CASH_COMMITMENT_SCHEDULE_VW`
- `DUPLICATE_INVOICE_RISK_VW`
- `GRIR_RECONCILIATION_WORKLIST_VW`
- `VENDOR_RISK_PROFILE_VW`
- `VENDOR_SPEND_CONCENTRATION_VW`
- `SPEND_COMPLIANCE_WORKLIST_VW`
- `INVOICE_STATUS_HISTORY_VW`
- `WEIGHTED_DAYS_PAYABLE_OUTSTANDING_VW`

The text-to-SQL feature is restricted to this curated list and discovers columns dynamically from `INFORMATION_SCHEMA`.

## OneLake writes

Saved insights and agent review logs can be written directly to Delta tables when the corresponding `ONELAKE_*_PATH` settings are supplied.

The identity needs OneLake/Fabric permissions for those tables.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Production

Use `Dockerfile` when possible so the Microsoft ODBC Driver 18 dependency is deterministic.

The unchanged React frontend uses the reference demo login. Keep `DEMO_AUTH_ENABLED=true` only where that compatibility login is intentionally permitted; for a production Entra-only deployment, set it to `false` and provide an Entra-integrated login flow (which would require a frontend change).

## Security action required

The uploaded project contained live-looking Azure/Fabric credentials in `.env`. That file has been removed from this cleaned package, but **rotate/revoke those credentials in Azure before using the original environment again**. Do not commit secrets to source control.
