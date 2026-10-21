# ProcureIQ Fabric Migration Audit / Fixes

Reference: `deploy 7(1).zip` (Snowflake)
Target: `ProcureIQ_Fabric_AzureOpenAI_fixed.zip` (Microsoft Fabric)

## Verification

- React static assets were byte-for-byte identical before backend fixes; they remain unchanged.
- FastAPI route paths/methods match the Snowflake reference for `/api/auth`, `/api/p2p`, and `/api/ai`.
- Python source compiles successfully with `compileall`.
- The Fabric package contains no Snowflake/Cortex runtime references.
- The Fabric SQL layer uses Entra access tokens with ODBC Driver 18.
- OneLake app-state writes use Delta-rs and the existing Delta schema before appending.

## Issues fixed

1. **Deployment ZIP layout**
   - The Fabric ZIP was nested under `ProcureIQ_react/`, while the startup script searches for `app/main.py` at the deployment root.
   - The final deployable ZIP is flattened so `app/`, `static/`, `requirements.txt`, `startup.sh`, and `Dockerfile` are at the root.

2. **Unchanged React login compatibility**
   - The unchanged React bundle calls `POST /api/auth/login` with the reference demo users.
   - The Fabric version had demo login disabled by default, making the unchanged UI unable to sign in.
   - Compatibility login is enabled by default and still requires `P2P_AUTH_SECRET`.
   - For an Entra-only production architecture, the UI must eventually be changed to initiate Entra sign-in; that is intentionally not done here because the requirement is zero frontend changes.

3. **CORS parity**
   - Added the reference `localhost:5175` origin to the Fabric defaults.

4. **Agent Console API contract**
   - The Fabric implementation had changed `agent_queue` from a list to `{"rows": [...]}`.
   - The Fabric implementation had replaced the reference snapshot with generic KPIs.
   - The recommendation response did not return the reference `sections.RECOMMENDATION`, `facts`, `policy`, and other fields consumed by the unchanged React UI.
   - Restored the reference agent catalog, queue shapes, snapshot metrics, entity fact loading, recommendation sections, and policy hooks while using Azure OpenAI instead of Cortex.

5. **Agent action logging**
   - Fabric action-log writes now generate and persist `REVIEW_ID`, matching the read-side contract used by recent actions.

6. **OneLake Delta schema enforcement**
   - Appending a Python list of dicts directly to Delta can infer `null` Arrow types for optional/all-null fields and fail against an existing Spark-created Delta schema.
   - The writer now loads the target Delta schema and constructs the Arrow table with that exact schema before append.
   - Delta-rs was upgraded to `>=0.32.0`.

7. **Policy integration**
   - Added an optional `FABRIC_POLICY_TABLE` setting so the Fabric policy catalog/search can use a curated Fabric table when present, instead of silently hard-coding Snowflake policy services.

## Known deployment prerequisites

Set these as Azure App Service/Container environment variables (do not commit secrets):

- `FABRIC_SQL_SERVER`
- `FABRIC_DATABASE`
- `FABRIC_SCHEMA`
- `AZURE_TENANT_ID`
- `AZURE_CLIENT_ID`
- `AZURE_CLIENT_SECRET` for service-principal authentication, or use managed identity
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_DEPLOYMENT`
- `P2P_AUTH_SECRET`

For app-state writes, also configure the three `ONELAKE_*_PATH` values with the actual ABFS table paths and grant the runtime identity write access to those OneLake tables.

## Important limitation

The ZIPs contain application code but not the deployed Fabric Warehouse/Lakehouse SQL objects/data themselves. Therefore object existence, column-level schema compatibility, stored procedure behavior, and workspace permissions cannot be proven offline. The package includes `/health/fabric` for a live connection check.

## Frontend constraint

No React source, JavaScript bundle, CSS, component, route, API call, or user-facing UI asset was modified.
