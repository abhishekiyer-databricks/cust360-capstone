# T4 + T5 + T6 Plan — Dashboard embed, Genie chat, and `app.yaml` finalize

> **Why these three together?** They're the remaining *feature-and-config polish* before
> the deploy/packaging endgame (T8-full → T3a → T9 → optimizations). T4 is genuinely tiny
> (`/api/config` already exists — we add one field + one iframe page). T5 is the only real
> build of the trio (3 OBO endpoints + a floating chat widget), and it's low-risk because
> the `dashboards.genie` OBO scope is already granted (T2). T6 is mostly **finalize + verify**
> now that OBO shipped — and its two done-when checks (*"no missing-secret errors"* +
> *"obo_client can call SQL, Lakebase, and Genie without 401s"*) are **exercised by T4 and T5
> themselves**. So T6 rides along as the config-hardening pass that closes out all three.
>
> **Three deployable slices** (same one-slice-at-a-time cadence as t3/t7):
>
> | Slice | Task | Core work | Deploy checkpoint |
> |---|---|---|---|
> | **A** | T4 dashboard embed | `databricks_host` → `/api/config`; `Dashboard.tsx` iframe; enable `/dashboard` route + nav; **workspace embed allowlist prereq** | Dashboard renders in-app, no `X-Frame-Options` error |
> | **B** | T5 Genie chat | `routers/genie.py` (3 OBO endpoints: start / message / poll+attachment); `GenieWidget.tsx` floating overlay | "Top segment by LTV" answers; follow-ups keep context |
> | **C** | T6 finalize | `FORWARD_ETL_JOB_ID` → `valueFrom` job binding; PG vars → secret `valueFrom`; reconcile OBO scope location; verify done-when | App starts clean; OBO hits SQL + Lakebase + Genie with no 401 |
>
> **See also:** `master_plan.md` §2 (architecture — dashboard/Genie hang off the OBO/warehouse
> box), §3-D2 (identities: warehouse + Genie = **OBO**), §5 rows 8–10 (task order T4→T5→T6),
> §10 (open items: **embed allowlist**); `t2_plan.md` (OBO client `obo_client(request)` →
> 401 if no `X-Forwarded-Access-Token`, no SP fallback; scopes live in `resources/app.yml`
> `user_api_scopes`, not app.yaml); `t3_plan.md` §3B (the OBO→warehouse pattern Genie reuses);
> `t7_plan.md` (the `FORWARD_ETL_JOB_ID` we hardcoded and now bind properly). Task doc:
> `CAPSTONE_TASKS.md` T4 / T5 / T6.

---

## 0. Concept — what these teach

- **T4 (iframe embed):** the *supported* way to bring an existing AI/BI dashboard into a
  custom app. No API, no data plumbing — the dashboard authenticates the *viewer* itself
  (they're already logged into the workspace in the same browser). The only real gotcha is
  browser security: the workspace must **allowlist the app's domain** or `X-Frame-Options`
  kills the frame. Teaches: embed integration + the external-access security model.
- **T5 (Genie Conversation API):** natural-language Q&A over the gold data, driven by the
  calling user's identity (**OBO** — so Genie's row/column security and audit reflect the
  real user, not the app SP). Teaches: the async conversation→message→poll→attachment loop,
  and building a polished stateful widget on top of a polling API.
- **T6 (`app.yaml` finalize):** the config that ties the *deployed* app to provisioned
  resources. Teaches: env wiring, **secret binding via `valueFrom`**, **resource binding**
  (job id from the bundle, not a magic number), and where OBO scopes actually live under DABs.

---

## Locked decisions (before we touch code)

### DA — Dashboard embed URL + host source (T4)
- **Embed path:** `${host}/embed/dashboardsv3/${dashboard_id}` (per task doc + the embed
  blog). `dashboard_id` is already in `/api/config`; we **add `databricks_host`**.
- **Host source:** `config.DATABRICKS_HOST` (already read in `config.py` from the
  runtime-injected `DATABRICKS_HOST`). Do **not** hardcode the workspace URL in the frontend.
- **Allowlist is a hard prerequisite, not code** — see Slice A step 0. Without it the
  done-when cannot pass regardless of code correctness.

### DB — Genie is OBO, and stateless on our side (T5)
- **Identity = OBO** (master_plan §3-D2). Every Genie endpoint uses `obo_client(request)`
  from `auth.py` → 401 if the user token is absent (no SP fallback). The `dashboards.genie`
  scope is **already granted** (resources/app.yml `user_api_scopes`), so no auth changes.
- **We hold no conversation state server-side.** The frontend owns `conversation_id` +
  `message_id` and passes them back; our endpoints are thin pass-throughs to the SDK. This
  keeps the backend stateless (correct for a multi-replica app) and puts the poll loop where
  the UX lives.
- **Polling, not streaming.** The Conversation API is async: create a message → poll
  `get_message` until status is terminal (`COMPLETED` / `FAILED` / `CANCELLED`) → if the
  completed message carries a query **attachment**, fetch its result rows for the preview.
  Cap client polling at **~30s** then surface a friendly timeout (task requirement).
- **Widget = floating overlay** mounted in the app shell (not a route) — the stub already
  exists at `AppLayout.tsx:90`. Compact panel bottom-right, Enlarge toggle, and an "Open in
  workspace" deep link (`${host}/genie/rooms/${genie_space_id}`) in the expanded header.

### DC — T6 secret + resource binding (per user decision 2026-07-28)
- **Switch Lakebase host vars to secret `valueFrom`.** The `capstone-abhishek-iyer` scope
  has exactly these keys → map:
  - `PGHOST` → `valueFrom` scope key `pg_host`
  - `PGDATABASE` → `valueFrom` scope key `pg_database`
  - `PG_INSTANCE_NAME` → `valueFrom` scope key `pg_instance_name`
  - (`pg_uc_catalog` exists in the scope but we don't currently read it as an env var — leave
    it unless a consumer appears.)
- **Keep as plain `value:`** the ids that are **not secrets and not in the scope**:
  `WAREHOUSE_ID`, `DASHBOARD_ID`, `GENIE_SPACE_ID`, `CAPSTONE_CATALOG`, `CAPSTONE_SCHEMA`.
  > **Reflection point:** the demarcation is "is it a credential/connection secret?" —
  > hostnames of a private Postgres endpoint arguably are; public resource ids are not. We
  > bind the former via the secret scope and leave the latter as declarative config. This is
  > a *deliberate* split to call out, not laziness.
- **`FORWARD_ETL_JOB_ID` → resource `valueFrom` binding.** Today it's a hardcoded literal in
  `app.yaml` with a `TODO(T6)`. Replace with a `valueFrom` that references the `forward_etl`
  job **resource** (declared in `resources/jobs.yml`) bound to the app in `resources/app.yml`
  → the deploy resolves the real job id. No more magic number that breaks on redeploy to a
  new workspace.
- **OBO scopes stay in `resources/app.yml` (`user_api_scopes`), NOT in `app.yaml`.** The task
  doc describes an `app.yaml` `user_authorization` block, but under **DABs** the app-resource
  field is authoritative and the two would fight. We document the equivalence and keep the
  single source of truth in the bundle. (This is the same call we made in T2.)

> ⚠️ **`valueFrom` mechanics are the one place to verify live during Slice C.** In Databricks
> Apps, an `app.yaml` env entry using `valueFrom` references a **resource key** declared under
> the app resource's `resources:` block (secret / job / warehouse binding) in
> `resources/app.yml`. Exact YAML key names (`valueFrom` vs nested `secret:`/`job:` shapes)
> have shifted across releases — confirm against the two T6 docs before finalizing, then
> `bundle validate` + a real deploy is the proof. Fall back to the current plain-value
> `app.yaml` if a binding shape fails validate; correctness of the running app > purity.

---

## Slice A — T4 dashboard embed

### Step 0 (PREREQUISITE — verify, not code)
Workspace **Settings → Security → External Access → Embed Dashboard & Genie Agents** must
allowlist the app host `customer360-984752964297111.11.azure.databricksapps.com`. **Already
satisfied** (confirmed 2026-07-28): the workspace has `*.databricksapps.com`, which matches
our host at any sub-label depth. No action needed — the iframe render in step 3 is the
definitive proof. If it ever comes back blank with an `X-Frame-Options` console error, this
is the thing to revisit. (master_plan §10 open item — now closed.)

### Backend (`app/backend/main.py`)
- Add `databricks_host` to the `/api/config` payload (previously returned only
  `warehouse_id`, `dashboard_id`, `genie_space_id`).
- ⚠️ **GOTCHA (hit live 2026-07-28):** on the Apps runtime `DATABRICKS_HOST` is injected
  **without a scheme** (`adb-….azuredatabricks.net`), unlike `app/.env` which has `https://`.
  A frontend embed URL built from a scheme-less host is a **relative** URL → the browser
  resolves it against the app's own origin → the SPA catch-all serves `index.html` → the
  dashboard iframe loads *the whole app again* → infinite nested-app recursion (the customers
  table appears inside the Dashboard tab, nesting forever). **Fix:** normalize to
  `https://{host}` in `/api/config` (and defensively in `Dashboard.tsx`). Embed URL confirmed:
  `${host}/embed/dashboardsv3/${dashboard_id}` (the `/embed/` variant of the workspace's
  `.../dashboardsv3/{id}/published?o=…` URL). **Reflection point.**

### Frontend
- `app/frontend/src/api/client.ts` — extend the `AppConfig` type + `getConfig()` to include
  `databricks_host` and `genie_space_id` (the latter is needed by T5's "open in workspace").
- **New** `app/frontend/src/pages/Dashboard.tsx`:
  - `useQuery(['config'], getConfig, { staleTime: 5*60_000 })`.
  - Render a full-height `<iframe src={`${host}/embed/dashboardsv3/${dashboard_id}`}>` with
    `title`, `style={{ width: '100%', height: '100%', border: 0 }}`, inside a Mantine `Box`
    that fills the main area. Loading spinner while config resolves; friendly message if
    `dashboard_id` missing.
- `app/frontend/src/App.tsx` — replace the `Placeholder` at the `/dashboard` route
  (`App.tsx:29`) with the lazy `Dashboard` page.
- `app/frontend/src/components/AppLayout.tsx` — flip the Dashboard nav item to `enabled: true`
  (`AppLayout.tsx:15`).

### Deploy & test (Slice A)
1. `cd app/frontend && npm run build` (compiles → `backend/static`).
2. `databricks bundle deploy --target dev`.
3. In an **authed browser**: click **Dashboard** → the AI/BI dashboard renders with data,
   **no** `X-Frame-Options` / auth error in the console.

**Done when (T4):** dashboard renders inside the app and displays data.

---

## Slice B — T5 Genie chat

### Backend — **new** `app/backend/routers/genie.py` (all OBO)
Use the SDK `w.genie.*` surface (via `obo_client(request)`), reading the space id from
`config.GENIE_SPACE_ID`. Three endpoints (task doc):

1. `POST /api/genie/conversations`
   - body: `{ "content": "<first question>" }`
   - `w.genie.start_conversation(space_id=..., content=...)` → return
     `{ conversation_id, message_id }` (start returns the first message too).
2. `POST /api/genie/conversations/{conversation_id}/messages`
   - body: `{ "content": "<follow-up>" }`
   - `w.genie.create_message(space_id, conversation_id, content=...)` → return
     `{ message_id }`. (Reusing `conversation_id` is what preserves context — the 2nd
     done-when.)
3. `GET /api/genie/conversations/{conversation_id}/messages/{message_id}`
   - `w.genie.get_message(space_id, conversation_id, message_id)`.
   - Return a normalized shape the widget can poll on:
     ```json
     {
       "status": "COMPLETED|IN_PROGRESS|FAILED|...",
       "content": "<text answer if any>",
       "attachment": { "query": "<sql>", "description": "..." } | null,
       "result": { "columns": [...], "rows": [...] } | null
     }
     ```
   - **If terminal + has a query attachment:** call
     `w.genie.get_message_attachment_query_result(space_id, conversation_id, message_id,
     attachment_id)` (confirm exact method name against the SDK during impl) and map the
     statement-response columns/rows into `result` (cap rows for the preview, e.g. first 50).
   - **Errors:** no OBO token → 401 (inherit from `obo_client`); SDK failure → 502 with a
     friendly detail (mirror `warehouse.py`'s handling). `PermissionDenied` on the
     `dashboards.genie` scope → 403 (the same re-consent gotcha as 3B; note it).
- Register the router in `main.py` (stage tag `t5-genie`), same pattern as `jobs.py`.
- `models.py` += `GenieStartRequest` / `GenieMessageRequest` / `GenieMessage`
  (+ `GenieResult`) Pydantic models.

> **Poll on the server or the client?** Client. Each `GET` is one `get_message` call; the
> **frontend** runs the poll loop (below) so the backend stays stateless and no request hangs
> 30s. (An outbound SDK timeout still guards each call.)

### Frontend — `app/frontend/src/api/client.ts`
Add types + `startGenieConversation(content)`, `sendGenieMessage(convId, content)`,
`getGenieMessage(convId, msgId)`.

### Frontend — **new** `app/frontend/src/components/GenieWidget.tsx`
- **Mount in `AppLayout.tsx`** replacing the current stub button (`AppLayout.tsx:90-107`):
  the floating lava `ActionIcon` bottom-right toggles the panel open/closed (remove the
  `navigate(location.pathname)` placeholder onClick and the "coming in T5" tooltip).
- **Panel:** compact (~380px) card anchored bottom-right; message list (user + Genie
  bubbles), a text input + send. **Enlarge toggle** in the header expands to a wide view
  (~720px). Expanded header shows an **"Open in workspace"** link →
  `${databricks_host}/genie/rooms/${genie_space_id}` (from `/api/config`, new tab).
- **Send flow (poll loop):**
  1. First message → `startGenieConversation`; store `conversation_id`. Subsequent → `sendGenieMessage(conversation_id, ...)`.
  2. Either returns a `message_id`. **Poll** `getGenieMessage` every ~1.5s, showing a
     **typing indicator**, until `status` terminal **or ~30s elapsed** (cap the poll count).
  3. Terminal → render `content` + (if present) a small result-preview table from `result`.
  4. Timeout / `FAILED` → friendly inline error ("Genie couldn't answer that in time — try
     rephrasing"), re-enable input.
- Keep it local-state (no TanStack cache needed for the conversation; it's ephemeral UI
  state). Manage the poll with a cancelable effect so closing the panel stops polling.

### Deploy & test (Slice B)
1. `npm run build` → `bundle deploy --target dev`.
2. Authed browser: open Genie → ask **"Top segment by LTV"** → typing indicator → answer +
   result preview appears. Ask a **follow-up** in the same panel → context preserved.
3. If Genie 403s on scope: **re-consent in incognito** (clear cookies for BOTH the app domain
   and the workspace domain) — same fix documented for 3B. The scope is granted; consent may
   be stale for the dev.

**Done when (T5):** "Top segment by LTV" returns an answer + result preview; follow-ups keep
context.

---

## Slice C — T6 `app.yaml` finalize + verify

### C1 — Secret `valueFrom` for Lakebase host vars (`app/app.yaml` + `resources/app.yml`)
- In `resources/app.yml`, add a `resources:` binding under the `customer360` app for the
  secret scope `capstone-abhishek-iyer` (declare the secret keys the app may read:
  `pg_host`, `pg_database`, `pg_instance_name`). *(Confirm the exact app-resource secret-
  binding YAML shape against the T6 "resources binding" + "env vars + secrets" docs.)*
- In `app/app.yaml`, change `PGHOST` / `PGDATABASE` / `PG_INSTANCE_NAME` from `value:` to
  `valueFrom:` the corresponding bound secret resource key.
- `config.py` needs **no change** — it already reads `PGHOST`/`PGDATABASE`/`PG_INSTANCE_NAME`
  from the env; the platform resolves `valueFrom` before the process starts.

### C2 — `FORWARD_ETL_JOB_ID` → job resource `valueFrom` (`app/app.yaml` + `resources/app.yml`)
- Bind the `forward_etl` job (from `resources/jobs.yml`) to the app in `resources/app.yml`'s
  app `resources:` block, giving it a resource key.
- Replace the hardcoded `FORWARD_ETL_JOB_ID: value: "31418249274366"` in `app.yaml` with a
  `valueFrom` referencing that job resource key. Removes the `TODO(T6)` and the magic number.
- Update the comment in `config.py:47-49` (which literally says "T6 upgrades this to a
  valueFrom binding") to reflect that it's now done.

### C3 — Scope-location reconciliation (documentation)
- Add a comment block to `app.yaml` stating: OBO scopes are **not** set here; they live in
  `resources/app.yml` `user_api_scopes` (`sql`, `dashboards.genie`) which is authoritative
  under DABs — the platform auto-adds `iam.current-user:read` + `iam.access-control:read`.
  (The `app.yaml` header already says most of this; make sure it's accurate post-changes.)

### C4 — Verify (this is the real T6 "work")
After `bundle deploy --target dev`:
1. **App starts clean** — `databricks apps logs customer360` shows a normal uvicorn startup,
   **no** missing-secret / KeyError / connection errors. (First done-when.)
2. **OBO reaches all three via the deployed UI** (second done-when — already exercised by A+B):
   - **SQL warehouse:** open a customer → Metrics tab loads (200, not 401/403). *(T3B path.)*
   - **Lakebase:** list + detail render (SP path — still fine) and a note/override write
     succeeds. *(T3 path.)*
   - **Genie:** the widget answers. *(Slice B.)*
   - **Dashboard:** renders. *(Slice A.)*
3. Confirm the resolved job binding works: **Reports → Run forward-ETL** still triggers (now
   via the `valueFrom` job id, not the literal).

**Done when (T6):** app starts with no missing-secret errors; `obo_client()` calls SQL,
Lakebase, and Genie without 401s.

---

## Risks / watch-items

1. **Embed allowlist not toggled** → T4 iframe silently blank (console `X-Frame-Options`).
   Do Slice A step 0 first. *(External, user action.)*
2. **Genie SDK method names** (`get_message_attachment_query_result` and the attachment id
   plumbing) — verify against the installed `databricks-sdk` during impl; the attachment/
   result shape is the fiddliest part of T5. The apps-cookbook Genie recipe + the Conversation
   API doc are the references.
3. **`valueFrom` YAML shape** (C1/C2) — the one spot that may need a live doc check +
   `bundle validate`; fall back to plain values if a binding shape fails to validate. Don't
   let config purity block a working deploy.
4. **Stale OBO consent for the dev** — Genie may 403 until re-consent in incognito (same as
   the 3B `sql`-scope gotcha). Fresh users won't hit it. Note for the reflection.
5. **`requirements.txt` sync** — no new backend deps expected for T4/T5 (Genie is in the
   already-installed `databricks-sdk`); if anything is added, mirror it into
   `app/requirements.txt` (gotcha #6).

## Reflection carry-forwards (for `process_doc.md` at task close)
- T4: iframe embed + the external-access allowlist as the *security* lesson (not a code one).
- T5: OBO for Genie (user identity → Genie's own governance/audit), async poll loop, stateless
  backend / client-owned conversation state, ~30s cap UX.
- T6: the secret-vs-config split (bind Postgres connection secrets via `valueFrom`, leave
  public ids as declarative values); job id via **resource binding** not a magic number; and
  scopes living in the bundle (`user_api_scopes`), not `app.yaml`, under DABs.
