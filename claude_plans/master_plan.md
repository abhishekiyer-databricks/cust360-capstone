# Master Plan — Customer 360 on Databricks Apps + Lakebase

> **Status:** Phase 0 (planning). This is the single source of truth for architecture,
> cross-cutting decisions, task order, and best practices. Each task Tn gets its own
> `tN_plan.md` with implementation detail. We build as a **DABs git-source app from the
> start**, deploy + test on the workspace after each task, and push to a git repo for
> submission.

---

## 0. Context & environment (verified 2026-07-25)

| Thing | Value |
|---|---|
| Workspace | Azure Field Eng East — `https://adb-984752964297111.11.azuredatabricks.net` |
| CLI profile | `DEFAULT` (OAuth; token expires ~1h, re-auth with `databricks auth login --profile DEFAULT`) |
| User | `abhishek.iyer@databricks.com` |
| Gold catalog.schema | `ai_27.lakebase_apps_capstone_gold` |
| SQL warehouse | Shared Endpoint — `148ccb90800933a1` |
| Lakebase instance | `ai27-lb-apps-capstone` (state AVAILABLE) |
| Lakebase PG host | `ep-plain-art-e1jje7ek.database.eastus2.azuredatabricks.net` |
| PG database | `cust360ai27` |
| Lakebase UC catalog | `ai27_lb_apps_capstone` |
| Secret scope | `capstone-abhishek-iyer` (keys: pg_host, pg_database, pg_instance_name, pg_uc_catalog) |
| Dashboard ID | `01f187f4c665171295386e8e783eb17d` |
| Genie space ID | `01f187f4edaa146f889fecdc69cc2544` |
| Local working dir | `~/Desktop/lakebase_apps_capstone/capstone-app` |
| Toolchain | Databricks CLI 0.291.0 ✓ (≥0.290 for git-source apps), uv 0.10.7, node v24, **no bun → use `npm`** |

All of the above live in `app/.env` (already reconstructed).

---

## 1. What we're building (one paragraph)

A "Customer 360" customer-success web app for synthetic **Acme Retail** (10k customers).
Reps browse/filter customers, open a 360° detail view (profile, metrics, activity, notes,
segment override), ask **Genie** questions, view an embedded **AI/BI dashboard**, and
trigger a **forward-ETL** job. A separate `/api/external/*` surface exposes the same data
to partner systems via **M2M** auth. Backend = FastAPI + psycopg + Databricks SDK (Python
3.11, uv). Frontend = React + Vite + TypeScript + TanStack Query. Deployed as a
**git-source Databricks App via DABs**.

---

## 2. Architecture (the mental model)

```
                 reverse ETL (managed synced tables — T1)
   Delta gold  ─────────────────────────────────────────────▶  Lakebase (Postgres)
  ai_27.lakebase_apps_capstone_gold                            ai27_lb_apps_capstone.public.*
       ▲   customers / transactions / products                customers_synced (CONTINUOUS)
       │   customer_segments / support_tickets                transactions_synced (CONTINUOUS)
       │                                                       products_synced (TRIGGERED)
       │                                                              │  fast point reads (SP)
       │                                                              ▼
       │                                                        ┌────────────────┐
       │   forward ETL (MERGE processed=false → gold — T7)      │   FastAPI app  │
       ├────────────────────────────────────────────────────── │  + React (SP + │
       │        reads staging   ◀── app writes notes/overrides  │   OBO clients) │
       │                                                        └────────────────┘
   Lakebase staging (app-owned, writable — T1):                       │        │
     customer_notes_staging (→ new gold.customer_notes)               │        │ OBO
     customer_segment_overrides_staging (→ update gold.customers)     │        ▼
     customer_audit_log (append-only)                          SQL warehouse (metrics, T3)
                                                               Genie (T5), Dashboard embed (T4)
```

**Two data stores, two jobs:**
- **Delta gold** = analytical source of truth (OLAP). 5 tables provisioned by installer.
- **Lakebase (Postgres)** = operational serving layer (OLTP). Synced tables = fast read
  copies of gold; staging tables = app-owned write landing zone.

**Two ETL directions:**
- **Reverse ETL (T1):** gold → Lakebase synced tables (managed, declarative). READ path.
- **Forward ETL (T7):** Lakebase staging → gold (psycopg + MERGE, or Lakehouse Sync CDC). WRITE path.

**Two identities (T2):**
- **OBO** (`X-Forwarded-Access-Token`, calling user's identity) → SQL warehouse + Genie.
- **App SP** (service principal) → ALL Lakebase access + forward-ETL job trigger.
  Actor email for audit comes from `X-Forwarded-Email`.

---

## 3. Cross-cutting DECISIONS (decide once, applies everywhere)

These are the choices that touch multiple tasks. Locking them now avoids rework.

### D1 — Sync mode per synced table (T1 + reflection)
| Table | Mode | Rationale |
|---|---|---|
| `customers_synced` | **CONTINUOUS** | App must reflect gold changes (LTV, churn) within seconds. |
| `transactions_synced` | **CONTINUOUS** | Recent-activity feed; freshness matters. |
| `products_synced` | **TRIGGERED (hourly)** | Slow-changing catalog (200 rows); continuous streaming is wasteful. **Justify in reflection.** |
> Non-continuous choice for products is a *graded* decision — call it out in the writeup.

### D2 — Auth model (T2, dictates every route)
- **Lakebase = App SP always.** Lakebase does NOT support OBO (`generate_database_credential`
  with a user bearer fails: "OAuth token does not have required scopes: postgres"). So
  every in-app DB read/write runs as the SP; we stamp the calling user (`X-Forwarded-Email`)
  into the audit log.
- **SQL warehouse + Genie = OBO** (calling user's bearer) so workspace RLS/audit reflect the user.
- **External API (T3a) = OBO with the partner SP's M2M-minted bearer**, reads gold via warehouse only.
- **OBO scopes = exactly `sql` + `dashboards.genie`.** Nothing else (platform rejects others).
- **Prereq:** Workspace admin → Settings → Apps → **User authorization (preview)** must be ON,
  else scopes silently purge and `X-Forwarded-Access-Token` never injects. **Verify in T2.**

### D3 — Forward-ETL pattern (T7)
- **Choose Pattern A (psycopg + MERGE INTO Delta, pull/on-demand).** Reasons: explicit,
  easy to reason about, idempotent via `processed=false` filter, triggered by the Reports
  button through the Jobs API, and it forces us to create `gold.customer_notes` (clean
  demonstration of the write path). Pattern B (Lakehouse Sync CDC) is Beta and hides the
  mechanics we want to show.
- **Destinations:** notes → **new** `gold.customer_notes` table; segment overrides →
  **UPDATE** `gold.customers.segment_id`; audit log stays in Lakebase.

### D4 — Lakebase connection pooling (T2 + Optimizations)
- Use **`psycopg_pool` (psycopg 3, already in pyproject)**, size 2–10 per worker.
- Token rotation: Lakebase OAuth tokens expire ~1h. **Mint a fresh token per checkout via
  a `connection_factory`** (option (a) in the task). Document this choice.

### D5 — Deploy model (TWO phases — corrected 2026-07-25)
- **Dev/test loop (now → T7): `source_code_path` deployment.** `bundle deploy` uploads the
  local `app/` folder; the app runs from there in the real Apps runtime (so OBO/T2+ is
  testable). **No GitHub pull, no Repos Git Proxy, no PAT.** This is still a real DABs deploy.
- **T8 (production pattern, required for submission): git-source app.** Switch
  `resources/app.yml` from `source_code_path` to `git_repository` (provider/url) + `git_source`
  (branch/source_code_path), register the app SP git credential (PAT). The Git Proxy cluster is
  only needed here.
  > Earlier mistake: front-loaded git-source into the first deploy → hit the terminated shared
  > "Repos Git Proxy" cluster. Git-source is a T8-only concern; dev uses source_code_path.
- `databricks.yml` with `targets: dev / prod`; iterate with `bundle deploy` + `bundle run`.
- **Use the Homebrew CLI `/opt/homebrew/bin/databricks` (v1.5.0) for ALL bundle commands** —
  the PATH `databricks` (v0.291.0) fails `bundle deploy` with an expired-Terraform-GPG-key error.
- **Commit `app/frontend/dist/`** (built bundle) so the runtime command is just
  `uvicorn backend.main:app` — no build step on the App runtime.
- **Do NOT keep `package.json` at `app/` root** (Apps runtime would run `npm build` and fail).
  Already removed; keep `package.json` only in `app/frontend/`.
  (See Scaffold Fixes below.)

### D6 — Dev/test loop (per user's decision)
- **Deploy-and-test on the workspace via DABs after each task** — no local-only dev loop.
  Trade-off accepted: slower iteration, but we validate in the real Apps runtime (OBO proxy,
  SP bindings, networking) where auth behaves differently than local.
- Sanity checks (lint, type-check, `bundle validate`) run locally before each deploy to
  catch cheap errors without waiting on a deploy.

---

## 4. Scaffold realities & required fixes (before/along T6–T8)

The scaffold ships stubs + one trap:
- `databricks.yml` — **empty** (build in T8).
- `app/app.yaml` — **empty** (build in T6).
- `resources/` — only `.gitkeep` (build `app.yml`, `jobs.yml`, `lakebase.yml` in T7/T8).
- `app/backend/` — only `__init__.py` (all backend code is ours).
- `app/frontend/src/` — only `.gitkeep` (all React is ours).
- ⚠️ **`app/package.json` at root is a trap** — T8 says the Apps build runtime detects it and
  runs `npm build`, which fails (React lives in `app/frontend/`). **Fix:** remove root
  `package.json`; the real one is already in... *(actually the scaffold's is AT root, and
  `frontend/` has none yet)* — so when we scaffold the frontend we put `package.json` inside
  `app/frontend/` and delete the root one. Track this in T3/T8.
- ⚠️ **`.gitignore` ignores `app/backend/static/` and `dist/`** but T8 wants the built bundle
  committed. `vite.config.ts` builds to `backend/static`. **Fix at T8:** either build to
  `app/frontend/dist/` and un-ignore it, or force-add the static dir. Decide in T8; note now.

---

## 5. Task order (dependency-driven, not numeric)

Rationale: run the foundation first (T1), then stand up the **thinnest deployable end-to-end
slice** (SP Lakebase read → one endpoint → one page) so the DABs deploy path is proven early,
then layer features. DABs deploy config (T8) is partially front-loaded because we deploy from
the start.

| Order | Task | Why here | Deploy checkpoint |
|---|---|---|---|
| 1 | **T1** (plan: `t1_plan.md`) — synced+staging tables, then SP grants (using the app SP created by the first `source_code_path` deploy) | Foundation; app is useless without it. SP grants (T1-b) need the SP that only exists after the first deploy. T8 git-source packaging is deferred. | Tables via psql/UI → minimal deploy (app SP) → grants applied |
| 3 | **T2** Auth (OBO + SP + `lakebase_sp()`) | Every route needs identity. Validate OBO preview toggle + consent. | Deploy; test endpoints return correct identities |
| 4 | **T3 read path** `/api/customers` + `/customers/{id}` + Customers/Detail pages | First real vertical slice: SP Lakebase reads → React list/detail. | Deploy; list paginates, detail renders |
| 5 | **T3 metrics** `/customers/{id}/metrics` (warehouse + OBO) | Adds the OBO warehouse path. | Deploy; metrics tab loads |
| 6 | **T3 write path** notes + segment override (staging + audit, 1 txn) | Exercises CRUD + audit + idempotency. | Deploy; writes land in staging + audit |
| 7 | **T7** Forward ETL (Pattern A job + Reports page) | Closes the loop staging→gold; needs the job wired for T8 resources. | Deploy; Reports button runs job, gold updates |
| 8 | **T4** Dashboard embed | Low-risk iframe; needs embed allowlist. | Deploy; dashboard renders in-app |
| 9 | **T5** Genie chat (floating widget, OBO) | Low-risk; polish item. | Deploy; Genie answers + follow-ups |
| 10 | **T6** `app.yaml` finalize (env + scopes + valueFrom) | Consolidate config once features exist. | Deploy; no missing-secret/401 errors |
| 11 | **T8-full** Finalize DABs (resources/*.yml, git creds, commit dist) | Full git-source deploy w/ SP git credential. | UI shows git repo+branch+commit SHA |
| 12 | **T3a** External M2M API + `examples/m2m_test.py` | Separate surface; after app SP exists. | `m2m_test.py` returns 200 + JSON |
| 13 | **T9** Lakebase ops (branch+PITR, query insights) | Ops exercises against the live instance. | Screenshots captured |
| 14 | **Optimizations pass** pagination/caching/pooling/React perf/observability | Harden the app; document choices. | Perf "done when" targets met |
| 15 | **Submission** reflection + 3-min recording + repo/app URLs | Package it up. | — |

> Note: We deploy incrementally, so a lightweight `databricks.yml` + `resources/app.yml` is
> created at step 2 and grown through the project rather than authored wholesale at T8.

---

## 6. Repo layout (target)

```
capstone-app/
├── claude_plans/               # master_plan.md + tN_plan.md (this planning trail)
├── app/
│   ├── app.yaml                # T6: env + user_authorization scopes
│   ├── pyproject.toml          # backend deps (uv) — shipped
│   ├── backend/
│   │   ├── __init__.py
│   │   ├── main.py             # FastAPI app, static mount, /api/config, middleware
│   │   ├── auth.py             # T2: obo_client(), sp_client()
│   │   ├── db.py               # T2: lakebase_sp() + psycopg pool w/ token rotation
│   │   ├── models.py           # Pydantic response models
│   │   ├── routers/
│   │   │   ├── customers.py    # T3: reads + writes
│   │   │   ├── genie.py        # T5
│   │   │   ├── jobs.py         # T7: run-forward-etl + status
│   │   │   └── external.py     # T3a: M2M gold-via-warehouse
│   │   └── static/             # built React bundle (see D5/T8 for commit strategy)
│   └── frontend/
│       ├── package.json        # the ONLY package.json (root one removed — D5)
│       ├── index.html
│       ├── src/
│       │   ├── main.tsx, App.tsx, routes
│       │   ├── api/client.ts
│       │   ├── pages/ Customers.tsx, CustomerDetail.tsx, Dashboard.tsx, Reports.tsx
│       │   └── components/ GenieWidget.tsx, layout (sidebar, top bar)
│       └── dist/ or built into backend/static  # committed for git-source runtime
├── lakebase/
│   ├── reverse_etl/            # T1: synced-table setup + staging DDL + SP grants
│   └── forward_etl/
│       └── pattern_a_psycopg2/ # T7: MERGE job
├── resources/
│   ├── app.yml                 # T8: git-source app resource + bindings + scopes
│   ├── jobs.yml                # T8: forward-ETL job
│   └── lakebase.yml            # T8: declarative synced-table specs
├── examples/
│   ├── _token.py               # T3a M2M helper
│   └── m2m_test.py             # T3a happy-path test
├── databricks.yml              # T8: bundle root, targets dev/prod, variables
├── CAPSTONE_TASKS.md           # (React path — the one we follow)
└── .gitignore                  # adjust for committed dist (D5/T8)
```

---

## 7. Best practices to bake in (from the Optimizations section — apply continuously)

- **Pagination:** every list endpoint takes `page`+`page_size` (default 25, hard cap 100,
  `422` above). Return `{items, total, page, page_size}`. Never return 10k rows. Prefer
  keyset pagination (`WHERE lifetime_value < :last_seen ORDER BY ... LIMIT`) once large.
  Add Lakebase composite index on `(segment_id, lifetime_value DESC)`.
- **Caching:** server-side TTLCache (~5m) for `/api/config`, segments, products only (not
  per-customer). Client-side TanStack Query staleTimes: list 10s, detail 30s, metrics 60s,
  config/segments/products 5m. `invalidateQueries(['customer', id])` after writes. Browser
  `Cache-Control: private, max-age=…, must-revalidate` on idempotent GETs.
- **Connection pooling:** `psycopg_pool` size 2–10, fresh token per checkout (D4).
- **React perf:** `React.lazy` + `<Suspense>` route code-split; memoize list grid; debounce
  filters ~250ms; parallel fan-out (`useQueries`) on detail page (Profile+Metrics+Activity+Notes).
- **API hygiene:** `GZipMiddleware(minimum_size=1000)`; minimal payloads (no `SELECT *`);
  Pydantic response models; outbound timeouts on warehouse/Lakebase/Genie.
- **Observability:** structured JSON logging; per-request `X-Request-Id` echoed back; log
  slow queries (>500ms) at WARNING with params.
- **Transactional integrity:** notes/override + audit write in the **same** psycopg transaction.
- **Idempotency:** segment override UPSERT (re-submit same value = no-op, no dup row).

**Perf targets (Optimizations "done when"):** list endpoint <200ms server-side (cold, no
warehouse); detail first paint <800ms warm cache; visible TanStack cache hits on tab/back
nav; no N+1 Lakebase queries on detail.

---

## 8. Submission checklist (keep visible)

- [ ] Every task T1–T9 checked
- [ ] Repo URL (fork; public OK — T8 SP-bound git credential)
- [ ] Live app URL (git-source app via `bundle deploy` + `bundle run`)
- [ ] 3-min recording: list → detail (all tabs) → add note → override → genie → dashboard → forward-ETL
- [ ] `examples/m2m_test.py` stdout (200 + JSON) in writeup
- [ ] T9 screenshots (branch+PITR, before/after p95 latency)
- [ ] Reflection paragraph: sync-mode choices + which optimizations done / next

---

## 9. Learning approach (per user — 0 React/FastAPI background)

Each task gets a `tN_plan.md` written **before** coding, explaining:
1. **Concept** — what the task teaches and why (so the "vibe coding" is understood, not blind).
2. **Design decisions** — options + the one we pick + rationale.
3. **Step-by-step implementation** — files touched, what each does, in plain language.
4. **How to deploy & test** — exact commands + what "working" looks like.
5. **Done-when checklist** — mirrored from the task doc.

At the very end (submission): add a **Graphviz / diagram** ("graphiy labs graph") showing the
architecture + how future feature development plugs into these patterns — the "easy future
feature dev" story.

---

## 10. Open items to confirm as we go
- [ ] Workspace **OBO preview toggle** ON? (blocks T2/T6 — check first thing in T2)
- [ ] **Embed dashboard allowlist** for the app domain (blocks T4)
- [ ] GitHub repo/fork created + **PAT** for SP git credential (blocks T8 full)
- [ ] Confirm app name (plan assumes `customer360` per T8 examples)
```
