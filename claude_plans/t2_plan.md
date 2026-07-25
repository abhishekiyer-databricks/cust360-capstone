# T2 Plan — Auth: OBO + service-principal clients + `lakebase_sp()`

> **Goal:** give the app its two identities and the plumbing to use them. After T2 the
> app can (a) act **as the calling user** against the SQL warehouse + Genie (OBO), (b) act
> **as itself** (the app service principal) for everything else, and (c) open a pooled
> Postgres connection to Lakebase as the SP. No user-facing features yet — this is the
> identity layer every later route (T3 reads/writes, T4 dashboard, T5 Genie, T7 job) sits on.
>
> **This is the first task that ships real backend code and must be tested on the DEPLOYED
> app** — OBO headers only exist behind the Databricks Apps proxy; they are never present
> when you run uvicorn locally. Dev loop = `bundle deploy` (source_code_path) + hit the app URL.
>
> **See also:** `master_plan.md` §2 (two identities), §3-D2 (auth model), §3-D4 (pooling),
> `t1_plan.md` §2-D6 (app SP client_id), `process_doc.md` (OBO prereq already verified).

---

## 1. Concept — what T2 teaches and why

A Databricks App runs as its own **service principal (SP)** — a non-human identity the
platform creates for the app (ours: `client_id 10c64b22-ac46-4123-bb60-041dc9d4fa92`). But
users log into the app as themselves. So there are **two identities** in play, and picking
the right one per call is the whole point of T2.

### The two identities

| | **OBO (On-Behalf-Of user)** | **App service principal (SP)** |
|---|---|---|
| Who it is | the logged-in human (e.g. `abhishek.iyer@…`) | the app itself |
| How the app gets it | reads the `X-Forwarded-Access-Token` header the Apps proxy injects into each request | uses the runtime's ambient SP credentials (env the platform sets) |
| Used for | **SQL warehouse** (T3 metrics) + **Genie** (T5) | **all Lakebase access** + the **forward-ETL job** trigger (T7) |
| Why | workspace RLS / audit / permissions reflect the actual user | Lakebase can't do OBO; jobs/DB are app-level work not tied to a user |

**How OBO physically works:** the Databricks Apps proxy sits in front of your app. When an
authenticated user makes a request, the proxy injects headers — most importantly
`X-Forwarded-Access-Token` (a short-lived user OAuth token scoped to what the app requested)
and `X-Forwarded-Email` (the user's email). Your backend reads that token and builds a
`WorkspaceClient(token=...)`; any call through that client is now "as the user." **These
headers do not exist locally** — that's why T2 must be tested on the deployed app.

### The one hard rule: Lakebase is always the SP
`generate_database_credential` with a *user* OBO bearer fails —
`Provided OAuth token does not have required scopes: postgres`. Lakebase doesn't support OBO
scopes. So **every** in-app DB read/write runs as the SP. We don't lose the user's identity
though: we stamp `X-Forwarded-Email` into the `customer_audit_log` (T1 table) so writes are
still attributable to a human. There is deliberately **no `lakebase_obo()`**.

### What we build (3 files, ~1 concept each)
- `app/backend/auth.py` — `obo_client(request)` and `sp_client()`.
- `app/backend/db.py` — `lakebase_sp()`: a psycopg connection pool that mints a fresh
  Lakebase token per checkout (tokens expire ~1h).
- test routes in `app/backend/main.py` — `/api/whoami` (proves OBO), `/api/whoami-sp`
  (proves SP), `/api/db-check` (proves `SELECT 1` via `lakebase_sp()`), plus a small
  `/api/config` so the frontend later knows warehouse/dashboard/Genie ids.

---

## 2. Design decisions

### D1 — `obo_client` reads the header; if it's missing, that's a 401 (not a fallback to SP)
If `X-Forwarded-Access-Token` is absent, we do **not** silently fall back to the SP (that
would leak SP privileges to unauthenticated callers and mask a misconfigured OBO toggle).
We raise `401` with a clear message. This makes a broken OBO setup loud, which is exactly
what we want while validating the preview toggle/consent flow.

```python
def obo_client(request: Request) -> WorkspaceClient:
    token = request.headers.get("X-Forwarded-Access-Token")
    if not token:
        raise HTTPException(401, "No OBO token — is the app behind the Apps proxy and consent granted?")
    return WorkspaceClient(host=DATABRICKS_HOST, token=token)
```
> We build a **fresh** `WorkspaceClient` per request for OBO — the token is per-user,
> per-request and short-lived, so caching it would be wrong. (Cheap: it's just an HTTP client.)

### D2 — `sp_client` is a module-level singleton built from ambient runtime creds
On the Apps runtime, the platform injects the SP credentials into the environment, so a
bare `WorkspaceClient()` (no args) authenticates **as the app SP**. We build it **once** at
import and reuse it (it's stateless + thread-safe for our use).

```python
_sp = WorkspaceClient()          # picks up the app SP creds from the runtime env
def sp_client() -> WorkspaceClient:
    return _sp
```
> Locally (no runtime SP env), `WorkspaceClient()` falls back to the `DEFAULT` CLI profile —
> fine for local smoke tests, but the real SP identity only appears on the deployed app.
> That's why the "runs as the SP in audit logs" done-when is checked on the deployed app.

### D3 — `lakebase_sp()` = psycopg_pool + fresh-token-per-checkout (master_plan §3-D4)
Two problems to solve at once:
1. **Token expiry** — Lakebase OAuth tokens last ~1h. A long-lived pooled connection would
   die mid-session if we baked the token in at pool creation.
2. **Connection cost** — opening a new TLS Postgres connection per request is slow.

**Solution:** a `psycopg_pool.ConnectionPool` (size 2–10) with a **`connection_factory` /
`configure`-style hook that mints a fresh token at each connection open**, so the password is
always current. Concretely: the pool's `kwargs`/connect step calls
`sp_client().database.generate_database_credential(request_id=<uuid>, instance_names=[PG_INSTANCE_NAME]).token`
to get the password, connects as `user = <SP client_id>` (the PG role from T1), `sslmode=require`.

```python
from psycopg_pool import ConnectionPool
import uuid

def _fresh_conninfo() -> dict:
    tok = sp_client().database.generate_database_credential(
        request_id=str(uuid.uuid4()), instance_names=[PG_INSTANCE_NAME]
    ).token
    return dict(host=PGHOST, port=5432, dbname=PGDATABASE,
                user=SP_CLIENT_ID, password=tok, sslmode="require")

# One pool per worker; token refreshed per new physical connection.
_pool = ConnectionPool(min_size=2, max_size=10, open=False,
                       connection_class=..., kwargs=...)  # see §3 for the exact token-rotation wiring
```
> **Design note to resolve in code (§3-Step 3):** psycopg_pool caches `kwargs` at pool
> construction, so a naive `kwargs=_fresh_conninfo()` mints the token **once**. To rotate,
> we either (a) subclass/override the pool's connect to call `_fresh_conninfo()` each time,
> or (b) use `max_lifetime` (e.g. 45 min < 1h token TTL) so connections recycle before the
> token expires and reconnect with a fresh one, or (c) the simplest correct MVP: a
> `@contextmanager lakebase_sp()` that mints a token and opens a **single** short-lived
> connection per call (no pool). **Decision: ship (c) for T2** — it's obviously correct and
> proves the path; **upgrade to the pool (a/b) in the Optimizations pass** (master_plan §7).
> Document this in the reflection as a deliberate "correctness first, pool later" call.

### D4 — `user` for Lakebase = the SP `client_id` UUID, not `current_user()`
The T1 grants were made to the PG role named after the SP `client_id`
(`10c64b22-...`). On the deployed app, the SP *is* that role, so
`user = SP_CLIENT_ID` is correct and matches the grants. (The T1 setup scripts used
`current_user()` because they ran as *your* identity to create objects; the app connects as
the SP.) We read `SP_CLIENT_ID` from env/config.

### D5 — Config surface: env vars now, `app.yaml valueFrom` later (T6)
`auth.py`/`db.py` read config from environment variables (`DATABRICKS_HOST`, `PGHOST`,
`PGDATABASE`, `PG_INSTANCE_NAME`, `SP_CLIENT_ID`, `WAREHOUSE_ID`, `DASHBOARD_ID`,
`GENIE_SPACE_ID`). For T2 we add the ones we need to `app/app.yaml` as plain `env:` entries
(or rely on runtime-injected ones); the **secret-backed `valueFrom` + `user_authorization`
scopes block is finalized in T6**. We only add what T2 needs to boot and to run the whoami/db
checks. **`SP_CLIENT_ID`** is the one new value to inject (the app doesn't otherwise know its
own client_id at runtime in a convenient form — set it explicitly).

### D6 — OBO scopes must already be declared for the token to carry them
`X-Forwarded-Access-Token` only carries scopes the app declared under
`user_authorization.scopes` in `app.yaml`. We need exactly **`sql`** and
**`dashboards.genie`** (master_plan §3-D2). For T2's whoami test, `current_user.me()` works
with any valid user token, but to *fully* exercise OBO we add the scopes block now so T3/T5
inherit it. **Prereq already confirmed** (`process_doc.md`): workspace "Restrict OAuth scopes
for apps" = All APIs (*), so scopes won't be silently purged; first app load shows a one-time
consent screen per user.

---

## 3. Step-by-step implementation

**Step 1 — `app/backend/auth.py`.** Implement `obo_client(request)` (D1) and `sp_client()`
(D2). Read `DATABRICKS_HOST` from env. Add a tiny helper `caller_email(request)` returning
`request.headers.get("X-Forwarded-Email")` (used later by audit writes; handy to test now).
Import `WorkspaceClient` from `databricks.sdk`, `Request`/`HTTPException` from `fastapi`.

**Step 2 — `app/backend/config.py` (small).** Centralize env reads so routes don't call
`os.environ` everywhere: `DATABRICKS_HOST`, `PGHOST`, `PGDATABASE`, `PG_INSTANCE_NAME`,
`SP_CLIENT_ID`, `WAREHOUSE_ID`, `DASHBOARD_ID`, `GENIE_SPACE_ID`. (Optional but keeps
`auth.py`/`db.py` clean.)

**Step 3 — `app/backend/db.py`.** Implement `lakebase_sp()` per **D3 option (c)** for T2: a
`@contextmanager` that mints a fresh token via
`sp_client().database.generate_database_credential(request_id=uuid4, instance_names=[PG_INSTANCE_NAME])`,
opens one `psycopg.connect(host=PGHOST, port=5432, dbname=PGDATABASE, user=SP_CLIENT_ID,
password=token, sslmode="require")`, `yield`s it, and closes in a `finally`. Add a one-line
TODO/comment pointing at the pool upgrade (D3 a/b) for the Optimizations pass. Keep
`psycopg[pool]` in deps (already present) so the upgrade needs no dependency change.

**Step 4 — Test routes in `app/backend/main.py`.** Replace the T8-min stub body with:
- `GET /api/health` → `{"status":"ok","stage":"t2"}` (keep it).
- `GET /api/whoami` → `me = obo_client(request).current_user.me(); return {user_name, emails}` —
  proves OBO returns the **calling user**.
- `GET /api/whoami-sp` → `sp_client().current_user.me()` — proves the **SP** identity.
- `GET /api/db-check` → `with lakebase_sp() as c: cur.execute("SELECT 1"); return {"select_1": row}`.
- `GET /api/config` → `{warehouse_id, dashboard_id, genie_space_id}` (non-secret ids the
  frontend needs later; safe to expose).
- Add `Request` param to the OBO routes so FastAPI passes the raw request (that's how we read
  headers).

**Step 5 — `app/app.yaml`.** Add the `env:` entries T2 needs (at minimum `SP_CLIENT_ID`,
`DATABRICKS_HOST`, `PGHOST`, `PGDATABASE`, `PG_INSTANCE_NAME`, `WAREHOUSE_ID`,
`DASHBOARD_ID`, `GENIE_SPACE_ID`) and the `user_authorization` scopes block:
```yaml
user_authorization:
  scopes:
    - sql
    - dashboards.genie
env:
  - name: SP_CLIENT_ID
    value: "10c64b22-ac46-4123-bb60-041dc9d4fa92"
  - name: PGHOST
    value: "ep-plain-art-e1jje7ek.database.eastus2.azuredatabricks.net"
  # …PGDATABASE, PG_INSTANCE_NAME, WAREHOUSE_ID, DASHBOARD_ID, GENIE_SPACE_ID, DATABRICKS_HOST
```
> Non-secret values only (ids/hosts). Secret-backed `valueFrom` is a T6 concern. `DATABRICKS_HOST`
> is usually available at runtime, but set it explicitly to be safe. Keep the start `command:` block.

**Step 6 — Local sanity before deploy.** `ruff check app/backend`; `uv run python -c "import
backend.main"` (import doesn't crash — catches typos/missing deps). `bundle validate --target
dev`. These are cheap; they won't test OBO (needs the proxy) but catch broken code before a
slow deploy.

**Step 7 — Deploy + test on the workspace** (§4).

---

## 4. How to deploy & test

Use the **Homebrew CLI** (`/opt/homebrew/bin/databricks` v1.5.0) for bundle commands (the
PATH v0.291.0 breaks `bundle deploy`). Deploy is `source_code_path` (uploads local `app/`):

```bash
/opt/homebrew/bin/databricks bundle validate --target dev --profile DEFAULT
/opt/homebrew/bin/databricks bundle deploy   --target dev --profile DEFAULT
# source_code_path deploy → no `bundle run` git-pull needed; the app restarts on deploy.
```

Then, **in a browser (so OBO consent + headers apply)** hit the app URL
`https://customer360-984752964297111.11.azure.databricksapps.com`:

1. First load → **consent screen** for scopes `sql`, `dashboards.genie` → click Authorize (once).
2. `/api/whoami` → returns **your** email/user_name (not the SP). ✅ OBO works.
3. `/api/whoami-sp` → returns the **service principal** (name/app id). ✅ SP works.
4. `/api/db-check` → `{"select_1": 1}`. ✅ `lakebase_sp()` connects as the SP with T1 grants.
5. `/api/config` → the three ids.

> **`curl` caveat:** curling the app URL without the browser session won't carry
> `X-Forwarded-Access-Token`, so `/api/whoami` will 401 — that's expected. Test OBO routes
> **in the authenticated browser session** (or the app's built-in "open app" which carries auth).
> `/api/health` and `/api/whoami-sp`/`/api/db-check` don't need the user token.

**If `/api/whoami` 401s even in-browser:** OBO token isn't flowing. Check (a) the scopes block
is in `app.yaml` and deployed, (b) consent was granted, (c) the workspace scope-restriction
setting still allows `sql`/`dashboards.genie` (verified in `process_doc.md`).

---

## 5. Done-when checklist (from the task doc)

- [ ] `/api/whoami` (calls `obo_client(request).current_user.me()`) returns the **calling user**, not the SP
- [ ] `/api/whoami-sp` (uses `sp_client()`) runs as the **service principal** (confirm in audit logs)
- [ ] `SELECT 1` against Lakebase via `lakebase_sp()` works (`/api/db-check` → `{"select_1": 1}`)
- [ ] `app.yaml` declares `user_authorization.scopes: [sql, dashboards.genie]`; consent granted once
- [ ] Deployed + verified on the real app (not just local) — OBO headers only exist behind the proxy

---

## 6. Risks / gotchas specific to T2
- **OBO only behind the proxy.** Locally there's no `X-Forwarded-Access-Token`; don't judge
  OBO from a local uvicorn run. Test on the deployed app in an authed browser session.
- **Missing scopes block → silent OBO failure.** Without `user_authorization.scopes` in
  `app.yaml`, the token carries no usable scopes (T3/T5 will 403 even if whoami works).
- **Lakebase ≠ OBO.** Do not add `lakebase_obo()`; `generate_database_credential` with a user
  bearer fails on the `postgres` scope. All DB = SP; attribute via `X-Forwarded-Email`.
- **Token TTL in the pool.** The MVP `lakebase_sp()` opens a short-lived connection per call
  (always fresh token) — correct but not pooled. The pooled version (D3) must rotate tokens
  (`max_lifetime` < 1h or a per-connect factory) or long-lived connections die at ~1h.
- **Wrong PG user → permission denied.** Connect as `SP_CLIENT_ID` (the granted role), not
  `current_user()`. A `permission denied for table …` almost always means wrong user or the
  T1 grants were wiped by a destroy+redeploy (re-run `03_grant_sp.py`).
- **SP creds only on the runtime.** `WorkspaceClient()` = SP on the app, but = your CLI
  profile locally — so `/api/whoami-sp` only shows the true SP on the deployed app.

---

## 7. Next after T2
→ **T3 read path**: `GET /api/customers` (paginated) + `GET /api/customers/{id}` reading
`customers_synced` via `lakebase_sp()`, then the first React pages (Customers list + Detail).
That's the first real vertical slice. See `master_plan.md` §5 (order) and the T3 plan (to be written).
