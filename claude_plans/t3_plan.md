# T3 Plan — App APIs + React UI (the first real product)

> **Goal:** turn the auth plumbing from T2 into the actual product. After T3 a rep can open
> the deployed app, see a **paginated, filterable customer list**, click into a **360° detail
> view** (Profile · Metrics · Activity · Notes · Segment), **add a note**, and **override a
> segment** — with every write landing in Lakebase staging AND an audit row, in one
> transaction. T3 exercises **every** read/write pattern the training covers:
>
> | Pattern | Where |
> |---|---|
> | Lakebase **synced reads** (as app SP) | list + detail + activity |
> | **SQL warehouse + OBO** (calling user's bearer) | metrics tab |
> | Lakebase **CRUD + audit** (transactional) | notes + segment override |
>
> **This is also where the entire React frontend is stood up** — today `app/frontend/src/`
> is just a `.gitkeep`. T3 creates `package.json`, the Vite/React/TS/TanStack-Query/Mantine
> app shell, the API client, and the two pages. So T3 is genuinely large; we build it as
> **three deployable slices** (§0) so each is tested on the real Apps runtime before the next.
>
> **See also:** `master_plan.md` §2 (data/identity model), §3-D2 (auth: Lakebase=SP,
> warehouse=OBO), §3-D4 (pooling — MVP now, pool in Optimizations), §5 (task order rows 4–6),
> §7 (best practices to bake in), `t1_plan.md` (staging table DDL), `t2_plan.md`
> (`obo_client`/`sp_client`/`lakebase_sp`). Styling decision (this chat): **Mantine**
> component library, themed with the Databricks palette used in the earlier `ops-agent` app
> (navy `#1B3139`, lava `#FF3621`).

---

## 0. How T3 is sliced (3 deploy checkpoints, master_plan §5 rows 4–6)

We do NOT build all of T3 then deploy once. Each slice is a working vertical cut, deployed
and tested on the real app (OBO/SP only behave correctly behind the Apps proxy) before moving
on. This keeps failures small and localized.

| Slice | Backend | Frontend | Deploy checkpoint / "working" looks like |
|---|---|---|---|
| **3A — Read path** | `GET /api/customers` (paginated list), `GET /api/customers/{id}` (profile + last-20 txns) — both Lakebase synced via SP | **Stand up the whole React app** (package.json, shell, router, Query, Mantine, api client) + `Customers.tsx` list + `CustomerDetail.tsx` Profile & Activity tabs | List paginates server-side (never 10k rows); click a row → detail renders profile + recent activity |
| **3B — Metrics** | `GET /api/customers/{id}/metrics` — cross-table aggregate on **gold via SQL warehouse + OBO** | Metrics tab on the detail page (fanned out in parallel with the others) | Metrics tab loads lifetime spend, top-5 categories, 30/90-day totals, open tickets, avg CSAT; SQL audit attributes the query to **the user** |
| **3C — Write path** | `POST /api/customers/{id}/notes` and `POST /api/customers/{id}/segment` — staging INSERT/UPSERT **+ audit row in one txn** (SP; actor from `X-Forwarded-Email`) | Notes tab (list + add form) and Segment tab (current + override form), with optimistic-ish invalidate-and-refetch | Add note → appears immediately + audit row exists; re-submitting same segment = no-op (no dup row) |

> Order rationale (master_plan §5): read path first proves the SP→Lakebase→React round-trip
> and the whole frontend build/deploy story; metrics adds the OBO-warehouse path on top of an
> already-working page; writes come last because they depend on the detail page existing to
> host the forms.

---

## 1. Concept — what T3 teaches and why

### 1.1 The two data paths, side by side (this is the crux of T3)

The single most important thing T3 demonstrates is **why the app talks to two different data
stores with two different identities**:

```
 LIST + DETAIL + ACTIVITY            METRICS                    NOTES + OVERRIDE
 ─────────────────────────          ─────────────              ──────────────────────
 Lakebase synced tables             Delta gold                 Lakebase staging tables
 (customers_synced,                 (5 tables, joined)         (customer_notes_staging,
  transactions_synced)                                          _segment_overrides_staging,
        │                                 │                      customer_audit_log)
   as APP SP                          as USER (OBO)                  as APP SP
 (lakebase_sp)                       (obo_client → warehouse)   (lakebase_sp, 1 txn)
        │                                 │                            │
   sub-10ms point reads            heavy cross-table          transactional write +
   (why Lakebase exists)           aggregate (why the         audit; user identity
                                   warehouse exists)          from X-Forwarded-Email
```

- **Fast operational reads → Lakebase synced tables, as the SP.** Listing/paging 10k
  customers and pulling one customer + their 20 latest transactions are point/OLTP reads.
  Lakebase serves them in single-digit ms. The app SP is the only identity that can touch
  Lakebase (D2 — no OBO for postgres). `customers_synced` / `transactions_synced` are the
  CONTINUOUS mirrors from T1, so they're fresh within seconds of gold changing.
- **Heavy analytical aggregate → SQL warehouse on gold, as the user (OBO).** The Metrics tab
  joins `transactions × products × support_tickets` and computes lifetime spend, top-5
  categories, 30/90-day windows, open-ticket count, avg CSAT. That's an OLAP query — the
  warehouse is built for it, and running it **OBO** means workspace RLS/audit reflect the
  actual rep (task requirement). *Note:* `support_tickets` and `customer_segments` were
  deliberately **left in gold** (not synced — see task doc "Mapping into Lakebase"), which is
  exactly why metrics must take the warehouse path.
- **Writes → Lakebase staging, as the SP, transactionally, with audit.** Notes and overrides
  never touch gold directly (that's the forward-ETL job's job, T7). They land in app-owned
  staging tables. Every write also appends to `customer_audit_log` **in the same
  transaction**, and the human behind the write is captured from `X-Forwarded-Email` (since
  the DB identity is the SP, not the user).

### 1.2 What "the React app" means (for someone new to it)

Today there is no frontend. T3 creates a standard **Vite + React + TypeScript** single-page
app. The moving parts, in plain terms:

- **Vite** — dev server + bundler. `npm run dev` gives hot-reload locally (proxying `/api` →
  uvicorn, already configured in `vite.config.ts`); `npm run build` emits static files the
  FastAPI app serves in production.
- **React** — UI is built from **components** (functions returning markup). A page is a
  component; a table row is a component. React re-renders when state changes; we never touch
  the DOM by hand.
- **TypeScript** — types on our data shapes (a `Customer`, a `Page<Customer>`) so the editor
  catches mismatches before deploy.
- **TanStack Query (React Query)** — the data-fetching layer. Each `GET` becomes a
  `useQuery(['key'], fetcher)`; Query handles caching, loading/error state, background
  refetch, and cache invalidation after writes. This is what makes tab-switches and
  back-navigation instant (master_plan §7 caching) and how we fan out the detail-page fetches
  in parallel (`useQueries`).
- **Mantine** — the component library providing the look: `AppShell` (persistent left
  sidebar + top bar — exactly the task's layout), styled `Table`, `TextInput`/`Select`/
  `Button` forms, `Tabs`, `Modal`, `Notification`. We add `mantine-datatable` for the
  customer list (built-in server-side pagination). Themed with the Databricks colors.

### 1.3 The app shell (task "App design & UI requirements")

Persistent **left sidebar** (Customers · Dashboard · Reports — the latter two are stubs until
T4/T7), **top app bar** showing the signed-in user's email + a workspace badge, content in
the middle, and a **floating "Ask Genie" button** bottom-right (a stub placeholder in T3;
wired in T5). Built once with Mantine `AppShell` so every later page slots in.

---

## 2. Design decisions

### D1 — Pagination: `page`/`page_size` envelope, hard cap 100, keyset-ready (master_plan §7)
List endpoint signature: `GET /api/customers?segment=&min_ltv=&max_churn=&page=1&page_size=25`.
- Response envelope: `{ items: Customer[], total: int, page: int, page_size: int }`.
- `page_size` default **25**, **hard cap 100** → `422` above (FastAPI `Query(le=100)` does
  this automatically). Never return all 10k rows.
- **T3 ships OFFSET pagination** (`LIMIT :page_size OFFSET :((page-1)*page_size)`) — simplest
  correct thing, fine at 10k rows. We add the **composite index**
  `(segment_id, lifetime_value DESC)` in the staging/index step so ORDER BY + filter is cheap.
  **Keyset pagination** (`WHERE lifetime_value < :last_seen ORDER BY lifetime_value DESC
  LIMIT`) is called out in the Optimizations pass as the upgrade; note it in the reflection.
- `total` comes from a `COUNT(*)` with the same WHERE clause (one extra cheap query). Filters
  (`segment`, `min_ltv`, `max_churn`) are all optional and combine with AND.

### D2 — Metrics endpoint = SQL warehouse via OBO, `statement_execution` (task requirement)
Use `obo_client(request).statement_execution.execute_statement(warehouse_id=WAREHOUSE_ID,
statement=…, wait_timeout="30s", parameters=[…])` against `<catalog>.<schema>.*` gold tables.
- **Parameterized**, never string-interpolated (`:customer_id`), to avoid SQL injection and
  get plan reuse.
- Compute in **one round-trip where possible**: a single statement with CTEs / conditional
  aggregates returning one row (lifetime_value, spend_30d, spend_90d, open_tickets,
  avg_csat) + a small second statement (or `array_agg`) for top-5 categories — decide in code
  whether one statement with a struct/array is cleaner than two. Prefer **≤2 statements**.
- Catalog/schema come from config (`CAPSTONE_CATALOG` / `CAPSTONE_SCHEMA` — add to config +
  app.yaml env; values `ai_27` / `lakebase_apps_capstone_gold`).
- **OBO, not SP:** the task grades that the warehouse audit log attributes this to the user.
  If OBO token is missing → the existing `obo_client` 401 (no SP fallback).
- **Timeout** on the statement (`wait_timeout`, plus a client-side cap) so a slow warehouse
  doesn't tie up a worker (master_plan §7 API hygiene).

### D3 — Writes: staging + audit in ONE psycopg transaction; override is idempotent
Both write endpoints follow the same shape (master_plan §7 transactional integrity):
```python
with lakebase_sp() as conn:            # SP identity
    with conn.cursor() as cur:
        cur.execute("INSERT INTO customer_notes_staging (...) VALUES (...)", ...)
        cur.execute("INSERT INTO customer_audit_log (...) VALUES (...)", ...)
    conn.commit()                      # both rows or neither
```
- `lakebase_sp()` currently yields a connection in autocommit? **No** — psycopg3 default is a
  transaction; we make the two `execute`s + `commit()` explicit and roll back on exception.
  (Verify `lakebase_sp` doesn't set `autocommit=True`; if it does, wrap in an explicit
  `with conn.transaction():` block instead. **Action: confirm in db.py during 3C.**)
- **Actor email** = `caller_email(request)` (`X-Forwarded-Email`); reject the write `400/401`
  if absent (we must attribute audited writes to a human).
- **Notes** = plain INSERT (append; multiple notes per customer allowed). Audit `action="add_note"`.
- **Segment override = UPSERT** keyed on the `UNIQUE (customer_id)` constraint already in the
  T1 DDL:
  ```sql
  INSERT INTO customer_segment_overrides_staging (customer_id, override_segment, reason, author_email)
  VALUES (:id, :seg, :reason, :email)
  ON CONFLICT (customer_id) DO UPDATE
    SET override_segment = EXCLUDED.override_segment,
        reason = EXCLUDED.reason, author_email = EXCLUDED.author_email,
        created_at = NOW(), processed = FALSE
  WHERE customer_segment_overrides_staging.override_segment <> EXCLUDED.override_segment;
  ```
  The `WHERE ... <>` makes re-submitting the **same** value a true no-op (task done-when:
  "idempotent — re-submitting the same value is a no-op, not a duplicate row"). Audit row is
  only written when a change actually occurred (check `cur.rowcount`).

### D4 — Pydantic response models + minimal payloads (master_plan §7 API hygiene)
Define response models in `app/backend/models.py`: `Customer` (list row — 6-ish fields only,
NOT `SELECT *`), `CustomerDetail`, `Transaction`, `CustomerMetrics`, `Note`, `Page[T]`
envelope, plus request bodies `NoteCreate`, `SegmentOverride`. Routes declare
`response_model=…` so OpenAPI is typed and payloads are trimmed. SELECT only the columns each
shape needs.

### D5 — Frontend data layer: TanStack Query with per-key staleTimes (master_plan §7)
- `queryClient` defaults + per-query `staleTime`: **list 10s, detail 30s, metrics 60s,
  config/segments 5m** (`gcTime` ~5m). These map straight to the task's suggested defaults.
- Detail page **fans out** Profile + Activity + Metrics + Notes with `useQueries` /
  parallel `useQuery`s (task: "Fan out the per-tab fetches in parallel") so the page is one
  round-trip's latency, not four.
- After a write: `queryClient.invalidateQueries(['customer', id])` (and the notes key) so the
  UI refetches automatically — note appears "immediately," override reflects instantly.
- Filter inputs on the list are **debounced ~250ms** before triggering a refetch.

### D6 — Frontend structure & the build/serve model (ties to deploy)
- **`package.json` lives ONLY in `app/frontend/`** (master_plan scaffold trap — a root
  `app/package.json` would make the Apps runtime run `npm build` and fail). `vite.config.ts`
  is already at `app/` with `root: "frontend"` and `outDir: backend/static`.
- **Serving in production:** `main.py` mounts the built bundle. Add
  `app.mount("/", StaticFiles(directory="backend/static", html=True))` **after** all `/api`
  routes so API wins and everything else serves `index.html` (SPA fallback). For client-side
  routing to survive refresh on a deep link (`/customers/C0003600`), add a catch-all that
  returns `index.html` (StaticFiles `html=True` covers the root; a small catch-all handles
  sub-paths — decide in 3A).
- **Backend deps are automatic:** the runtime pip-installs `app/requirements.txt` on deploy
  (gotcha #6); the runtime start command is just `uvicorn` with **no build step**. So the
  only manual pre-deploy action is compiling the frontend (below) — the backend needs nothing.
- **Dev loop option:** locally `npm run dev` (Vite:5173, proxies `/api`→uvicorn:8000) gives
  hot reload for fast frontend iteration WITHOUT redeploying — but OBO/warehouse/real
  Lakebase only work on the deployed app. So: iterate UI locally against a running uvicorn for
  layout/wiring, then **deploy to verify the real data paths** (master_plan §D6 deploy-and-test).
- **Deploy packaging:** deploy is still `source_code_path` (uploads `app/`). We must **build
  the frontend** (`npm run build` → `backend/static`) and ensure that dir is uploaded.
  `.gitignore` ignores `app/backend/static/` — for `source_code_path` DABs sync this may
  exclude it. **Action (3A): confirm the built static dir reaches the deployed app**; if the
  bundle sync respects `.gitignore`, either un-ignore `backend/static` or add a `sync.include`
  in `databricks.yml`. (Committing `dist/` is formally a T8 concern, but the dev deploy needs
  the static files present now — resolve minimally in 3A, finalize in T8.)

### D7 — Segment names need a lookup (small server-side cache)
The list filter and the Segment tab show human segment names (Champions, Loyal, …), but
`customers_synced` only has `segment_id` (S1–S7). `customer_segments` lives in **gold** (not
synced). Options: (a) fetch the 7 rows once via the warehouse (OBO) and cache
server-side ~5m (`cachetools.TTLCache`, master_plan §7), exposed as `GET /api/segments`; or
(b) hardcode the 7-row map. **Decision: (a)** — `GET /api/segments` via warehouse, TTLCached;
it's the "cache the segments list" best practice the task calls out, and avoids drift. Small
scope; build in 3A (needed by the list filter) or defer the name display to 3B if the
warehouse path isn't wired until then. **Sub-decision: build `/api/segments` in 3B** (first
slice that already has the warehouse path), and in 3A show `segment_id` directly to avoid
pulling the warehouse into the read-only slice. Revisit if it hurts the 3A UX.

### D8 — Observability & hygiene, applied from the first endpoint (master_plan §7)
- `GZipMiddleware(minimum_size=1000)` on the app.
- Per-request **`X-Request-Id`** middleware (generate if absent, echo back) for correlation.
- Structured logging (`logging.getLogger(__name__)`); log queries slower than ~500ms at
  WARNING with params.
- Outbound timeouts on warehouse + Lakebase calls.
These are cheap to add now and are graded in the Optimizations "done when."

---

## 3. Step-by-step implementation

### Slice 3A — Read path + stand up the React app

**Backend**
1. **`app/backend/models.py`** — Pydantic models (D4): `Customer`, `CustomerDetail`,
   `Transaction`, `Page` (generic envelope `{items,total,page,page_size}`), request stubs.
2. **`app/backend/routers/customers.py`** — new `APIRouter(prefix="/api")`:
   - `GET /customers` — build WHERE from optional `segment/min_ltv/max_churn`; `COUNT(*)`
     + paged `SELECT` (only list columns) from `customers_synced` via `lakebase_sp()`;
     `page_size` via `Query(default=25, le=100, ge=1)`. Return `Page[Customer]`.
   - `GET /customers/{id}` — one `SELECT` for the profile from `customers_synced` + one
     `SELECT … ORDER BY transaction_date DESC LIMIT 20` from `transactions_synced`. Return
     `CustomerDetail`. (Two queries, not N+1.)
3. **`app/backend/main.py`** — `include_router(customers.router)`; add `GZipMiddleware`,
   `X-Request-Id` middleware, logging setup; mount `StaticFiles` for the built frontend
   (after API routes) + SPA fallback (D6).
4. **Index** — add `CREATE INDEX IF NOT EXISTS` on `customers_synced (segment_id,
   lifetime_value DESC)`. *But synced tables are managed/read-only* — indexes on synced
   tables may not be permitted or may be dropped on resync. **Action: verify** whether we can
   index `customers_synced`; if not, document that OFFSET over 10k is acceptable and note
   keyset+index as the Optimizations upgrade. (Put any index DDL in a small
   `lakebase/reverse_etl/04_indexes.py` run as the user, not baked into app startup.)

**Frontend (the big lift — first time the app exists)**
5. **`app/frontend/package.json`** — deps: `react`, `react-dom`, `react-router-dom`,
   `@tanstack/react-query`, `@tanstack/react-query-devtools`, `@mantine/core`,
   `@mantine/hooks`, `@mantine/notifications`, `mantine-datatable`, `@tabler/icons-react`;
   dev: `vite`, `@vitejs/plugin-react`, `typescript`, `@types/react`, `@types/react-dom`.
   Scripts: `dev`, `build` (`tsc && vite build`), `preview`. **No root `app/package.json`.**
6. **`app/frontend/index.html`** + **`src/main.tsx`** — mount React; wrap app in
   `MantineProvider` (Databricks theme), `QueryClientProvider` (with default staleTimes),
   `Notifications`, `BrowserRouter`.
7. **`src/theme.ts`** — Mantine theme with the Databricks palette (navy `#1B3139`, lava
   `#FF3621` as primary, the neutral bg/line/muted tokens from the ops-agent CSS).
8. **`src/api/client.ts`** — typed `fetch` wrapper (base `/api`, JSON, throws on non-2xx with
   the server detail, attaches nothing auth-related — the proxy does auth). Typed functions:
   `listCustomers(params)`, `getCustomer(id)`; TS types mirroring the Pydantic models.
9. **`src/components/AppShell.tsx`** (layout) — Mantine `AppShell` with sidebar nav
   (Customers active; Dashboard/Reports as disabled/placeholder links), top bar showing user
   email (from `/api/config` or a `whoami` call — reuse T2's `/api/whoami`) + a workspace
   badge, and a floating "Ask Genie" `ActionIcon` bottom-right (stub → T5).
10. **`src/pages/Customers.tsx`** — filter form (Select for segment, NumberInputs for
    min_ltv/max_churn, debounced), `mantine-datatable` `DataTable` with server-side
    pagination bound to `useQuery(['customers', filters, page], …)`; row click →
    `navigate('/customers/'+id)`.
11. **`src/pages/CustomerDetail.tsx`** — Mantine `Tabs`: **Profile** and **Activity** now
    (Metrics/Notes/Segment tabs added in 3B/3C). Parallel `useQuery`s. Profile shows
    name/contact/segment/signup/churn; Activity shows the 20-txn table.
12. **`src/App.tsx`** — routes: `/` → redirect `/customers`; `/customers` → list;
    `/customers/:id` → detail. Code-split with `React.lazy` + `<Suspense>` (master_plan §7).

**Deploy & test 3A** (§4). Checkpoint: list paginates server-side; detail shows profile +
activity; no 10k-row response.

### Slice 3B — Metrics (warehouse + OBO)

13. **`app/backend/warehouse.py`** (small helper) — `run_stmt(obo_ws, sql, params) -> rows`
    wrapping `statement_execution.execute_statement(warehouse_id=…, wait_timeout="30s",
    parameters=…)`, polling to terminal if needed, mapping columns→dicts, with a timeout and
    slow-query WARNING log (D2, D8).
14. **`models.py`** — add `CustomerMetrics` (lifetime_value, spend_30d, spend_90d,
    top_categories: list[{category, amount}], open_tickets, avg_csat).
15. **`routers/customers.py`** — `GET /customers/{id}/metrics` using `obo_client(request)`
    + the warehouse helper against gold; parameterized `:customer_id`; ≤2 statements (D2).
16. **`routers/customers.py`** — `GET /segments` via warehouse (OBO), `cachetools.TTLCache`
    ~5m (D7); add `cachetools` to requirements + pyproject.
17. **Frontend** — add **Metrics** tab to `CustomerDetail.tsx` (its own `useQuery`, staleTime
    60s), wire segment names into the list filter + Segment display via `/api/segments`.
18. Deploy & test 3B. Checkpoint: Metrics tab loads; confirm the **SQL audit log attributes
    the statement to the user** (task done-when for the warehouse path / mirrors T3a's intent).

### Slice 3C — Write path (staging + audit, transactional)

19. **`db.py`** — confirm transaction semantics (D3); if `autocommit`, switch writes to an
    explicit `with conn.transaction():`.
20. **`models.py`** — `NoteCreate {note_text}`, `SegmentOverride {override_segment, reason?}`,
    `Note` response.
21. **`routers/customers.py`**:
    - `POST /customers/{id}/notes` — INSERT note + INSERT audit (`add_note`) in one txn;
      actor from `X-Forwarded-Email` (400/401 if missing). Return the created `Note`.
    - `POST /customers/{id}/segment` — UPSERT (D3) + conditional audit (`override_segment`)
      only when changed. Return `{changed: bool, ...}`.
    - `GET /customers/{id}/notes` — list notes for the Notes tab (paged or last-N).
22. **Frontend** — **Notes** tab (list + `useMutation` add-note form, invalidate on success,
    Mantine `notifications.show`) and **Segment** tab (current segment + override form,
    `Select` of the 7 segments, idempotent submit, invalidate). Optimistic update optional.
23. Deploy & test 3C. Checkpoints: add-note appears immediately + audit row exists for every
    write; re-submitting same segment = no dup row.

---

## 4. How to deploy & test

Deploy stays `source_code_path` (dev mode). Use the **Homebrew CLI**
(`/opt/homebrew/bin/databricks` v1.5.0) — the PATH v0.291.0 breaks `bundle deploy`
(master_plan gotcha #3).

### What the runtime handles for us (so deploy is simple)
- **Python deps → automatic.** The Apps runtime installs from `app/requirements.txt` on
  deploy (gotcha #6). We never `pip install` by hand — we just keep `requirements.txt` in
  sync when we add a dep (e.g. `cachetools` in 3B).
- **Runtime start command is just `uvicorn`.** No build step runs on the App (D6 / master_plan
  D5). The app serves a **pre-built** static bundle.

### The one thing the runtime does NOT do: build the frontend
Browser React/TypeScript can't run as source — it must be compiled to plain JS/CSS first.
By design (D6, master_plan D5) **we build locally and ship the output**, rather than letting
the runtime build (which would need a root `app/package.json` — the exact trap the plan
avoids). So the frontend build is a real, required pre-deploy step; the backend needs nothing.

### Required deploy — two steps
```bash
# 1) REQUIRED: compile the React app → app/backend/static/ (only when frontend changed)
cd app/frontend && npm install && npm run build && cd ../..
# 2) REQUIRED: deploy (uploads app/, incl. backend/static; runtime pip-installs requirements,
#    then restarts uvicorn — in source_code_path mode `deploy` also restarts, so no `bundle run`)
/opt/homebrew/bin/databricks bundle deploy --target dev --profile DEFAULT
```
> `bundle run` is a **T8/git-source** concern (it makes Databricks pull the latest commit).
> In dev `source_code_path` mode, `bundle deploy` already uploads + restarts — so dev deploy
> is genuinely just the one `deploy` command (plus the frontend build when the UI changed).

### Optional pre-flight sanity (NOT deploy steps — just catch typos before a slow deploy)
Skip these if you like; they don't affect the deploy, they only fail fast locally:
```bash
uv run python -c "import backend.main"   # import doesn't crash (catches a syntax/typo)
ruff check app/backend                   # lint (optional)
/opt/homebrew/bin/databricks bundle validate --target dev --profile DEFAULT  # config sanity
```

Then, **in an authenticated browser** at
`https://customer360-984752964297111.11.azure.databricksapps.com`:

- **3A:** `/customers` shows a paged table; changing filters refetches; clicking a row opens
  `/customers/:id` with Profile + Activity. Verify in **Network tab** the list response is
  one page (≤100 rows), not 10k. `GET /api/customers?page_size=500` → **422**.
- **3B:** open a customer → Metrics tab populates; check **DBSQL query history / audit** shows
  the metrics statement ran as **your user** (OBO), not the SP.
- **3C:** add a note → it appears without a manual refresh; query Lakebase
  (`SELECT * FROM customer_notes_staging WHERE customer_id=…` and `customer_audit_log`) to
  confirm both rows. Override a segment twice with the same value → only **one** row in
  `customer_segment_overrides_staging`, and audit has one `override_segment` entry.

**Local UI iteration (optional, faster):** `cd app/frontend && npm run dev` (Vite:5173) with
`uvicorn backend.main:app --port 8000` running; the Vite proxy sends `/api`→uvicorn. Good for
layout/wiring; OBO/warehouse/real-Lakebase must still be verified on the deployed app.

---

## 5. Done-when checklist (from the task doc)

- [ ] All in-app endpoints return the correct shape, tested via the React UI
- [ ] Customer list paginates **server-side** (page-size cap enforced; never all 10k in one response)
- [ ] Adding a note appears in the list immediately AND a row exists in `customer_audit_log` for every write
- [ ] Overriding a segment is **idempotent** (re-submitting the same value is a no-op, not a duplicate row)
- [ ] Detail page fans out its tab fetches in parallel (no N+1; visible in logs / Network)
- [ ] Metrics runs on the **warehouse via OBO** (audit attributes it to the user)

---

## 6. Risks / gotchas specific to T3

- **Indexing synced tables (D1/3A step 4):** `customers_synced` is managed by the sync
  pipeline; a user index may be disallowed or dropped on resync. Verify; if blocked, OFFSET
  over 10k is fine for the capstone — record keyset+index as the Optimizations upgrade.
- **Static bundle not uploaded (D6):** `.gitignore` ignores `app/backend/static/`. If the
  `source_code_path` sync respects it, the deployed app 404s the UI. Confirm on the first 3A
  deploy; fix via un-ignore or `sync.include`.
- **Transaction semantics (D3/3C):** if `lakebase_sp()` yields an autocommit connection, the
  "same transaction" guarantee is silently lost. Verify and use explicit
  `conn.transaction()` / `commit()`.
- **Missing `X-Forwarded-Email` on writes:** don't write an audit row with a null/blank actor
  — reject the request. (Behind the proxy it's always present; a missing value means a
  misconfigured call.)
- **Metrics must be OBO, not SP:** easy to accidentally reuse `lakebase_sp`/`sp_client`. The
  grade is specifically that the warehouse statement runs as the user.
- **`CAPSTONE_CATALOG`/`CAPSTONE_SCHEMA` not in config/app.yaml yet:** metrics + `/segments`
  need them. Add to `config.py` + `app.yaml` env in 3B (values `ai_27` /
  `lakebase_apps_capstone_gold`). New deps `cachetools` → add to **both** `requirements.txt`
  (runtime installs from this) **and** `pyproject.toml` (gotcha #6).
- **SPA deep-link refresh:** without a catch-all → `index.html` fallback, refreshing
  `/customers/C0003600` 404s. Handle in the static mount (D6).
- **`page_size` cap:** enforce with `Query(le=100)` so oversize requests are `422`, not a
  silent full scan.

---

## 7. Files touched (summary)

**Backend:** `app/backend/models.py` (new), `app/backend/routers/__init__.py` (new),
`app/backend/routers/customers.py` (new), `app/backend/warehouse.py` (new, 3B),
`app/backend/main.py` (routers + middleware + static mount), `app/backend/config.py`
(+catalog/schema), `app/backend/db.py` (confirm txn), `app/requirements.txt` + `pyproject.toml`
(+cachetools), maybe `lakebase/reverse_etl/04_indexes.py` (new).

**Frontend (all new):** `app/frontend/package.json`, `index.html`, `src/main.tsx`,
`src/App.tsx`, `src/theme.ts`, `src/api/client.ts`, `src/components/AppShell.tsx`,
`src/pages/Customers.tsx`, `src/pages/CustomerDetail.tsx` (Dashboard/Reports placeholders and
`GenieWidget` stub as needed for nav).

---

## 8. Next after T3
→ **T7** Forward ETL (Pattern A job + Reports page) — closes the staging→gold loop and wires
the job the Reports button triggers (master_plan §5 row 7). Then T4 dashboard, T5 Genie, T6
`app.yaml` finalize, T8-full git-source, T3a external M2M. Write `t7_plan.md` first.
