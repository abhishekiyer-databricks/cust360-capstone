# T8 — Deploy `customer360` as a git-source app (via DABs)

> Style matches t3/t7 plans: **Concept → Decisions → Steps → Deploy/Test → Done-when.**
> Prereqs verified 2026-07-29: repo **public** (`abhishekiyer-databricks/cust360-capstone`,
> GitHub API 200), app SP id **144899163311454** / client_id `10c64b22-…`, CLI v0.291.0 (≥0.290 ✓),
> built SPA present at `app/backend/static/` (but git-ignored — see Step 1), root `app/package.json`
> already absent (trap avoided).

---

## 1. Concept — what T8 actually is (and isn't)

Today the app runs from a **workspace folder upload** (`source_code_path: ../app` → `bundle deploy`
copies `app/` up). T8 switches it to the **production pattern**: a **git-source app**. The DABs app
resource declares a GitHub **repo + branch + path**, and Databricks **pulls the source from GitHub**
on each `bundle run` and restarts the app. Source-code-path-only apps are **explicitly not accepted**
for this capstone.

**Not** a data task (ignore the JDBC/MySQL article — that's Spark reading MySQL; irrelevant).
**Not** a proxy-server task (no cluster/proxy to stand up — that was an earlier front-loading mistake).

Three mechanical requirements, that's all:
1. The app's source in DABs = `git_repository` + `git_source` (instead of `source_code_path`).
2. The GitHub source must be **self-contained for a pull** → the built React bundle must be **committed
   to git** (the runtime just runs `uvicorn`, no build step).
3. A git credential **bound to the app's service principal** (the pull runs as the SP, not as you).

---

## 2. Decisions

### D1 — Public repo, but register the SP credential anyway (user's call: best practices)
The repo is public, so the pull would likely succeed anonymously — but the **production pattern is a
private repo pulled by the app SP**, and the capstone grades that. So we register a GitHub PAT bound to
the app SP via `git-credentials create --json … principal_id:<APP_SP_ID>`. This is the *one* non-obvious
step: a **User Settings → Linked account credential does NOT apply to apps** (it's bound to your user;
the app pulls as its SP). One prerequisite you must supply: a **GitHub PAT** (fine-grained: read-only
"Contents" on this repo, or classic with `repo` scope).

### D2 — Source lives per-target; base resource carries NO source field
DABs rejects an app that has **both** `git_source` and `source_code_path`. To keep a fast dev loop
*and* a git-source prod, we put the source **only in each target override**, not in the base resource:
- `resources/app.yml` base → app name, scopes, resource bindings, **no source**.
- `databricks.yml` `targets.dev` → `source_code_path: ../app` (fast local iteration, unchanged).
- `databricks.yml` `targets.prod` → `git_repository` + `git_source` (the submission app).

> After T8, the **submission deploy path is `--target prod`**. Dev stays available for quick iteration,
> but note: a `bundle deploy --target dev` flips that same physical app back to source_code_path.
> Post-T8, prefer: commit + push → `bundle deploy/run --target prod`.

### D3 — Commit the built React bundle (`app/backend/static/`)
Vite builds to `app/backend/static/`, which `.gitignore` currently excludes (fine for dev — we
force-upload it via `sync.include`). For git-source, **git must contain it** so the runtime command
stays `uvicorn backend.main:app` with no build step. Fix: stop ignoring `app/backend/static/` and commit
the build output. Trade-off (accept + note in reflection): build artifacts now live in git and must be
rebuilt+committed before each prod deploy — the standard cost of the no-build-on-runtime pattern.

### D4 — Same physical app, updated in place
`customer360` already exists (created by the dev bundle). Deploying `--target prod` with the same app
`name: customer360` **updates the existing app** to git-source (apps are keyed by name) — same URL
(`customer360-984752964297111.11.azure.databricksapps.com`), no new SP, so the registered git credential
stays valid. We do **not** destroy/recreate (that would churn the SP id → re-register the credential).

### D5 — `resources/lakebase.yml`: document, don't destructively adopt (keep it simple)
The task lists a declarative synced-table spec. **But our 3 synced tables are already live/online**
(created via T1 psycopg DDL) and continuously syncing. Having DABs *manage* them risks a
recreate/adopt that could disrupt live sync. **Decision:** add `resources/lakebase.yml` as the
declarative spec **for documentation/portability** (the YAML equivalent of T1), but **do not deploy it
against the live instance in this pass** — comment it out of `include:` or keep it as reference. The
T8 done-when does **not** require it; the live tables + committed T1 scripts already cover "no drift."
Revisit if we ever rebuild the instance from scratch.

### D6 — `mode: production` friction — validate first
`prod` uses `mode: production`. Run `bundle validate --target prod` before deploy to catch any
run_as / naming complaints early. Deferred-from-T7 item (optional, note in reflection, not required for
done-when): set the forward-ETL job's prod `run_as` to the app SP + grant SP `USE CATALOG/SCHEMA` +
`MODIFY` on gold via a bundle `grants` block. Keep out of scope unless validate/deploy demands it.

---

## 3. Prerequisite you provide

- **GitHub PAT** for the app SP credential (fine-grained, read-only *Contents* on
  `abhishekiyer-databricks/cust360-capstone`; or classic `repo`). Have it ready for Step 4.

---

## 4. Steps (files touched, in order)

**Step 1 — Un-ignore + commit the built SPA.**
- Edit `.gitignore`: remove the `app/backend/static/` line (leave `dist/` if unused, or drop both).
- `cd app/frontend && npm run build` (rebuild fresh → `../backend/static/`).
- `git add app/backend/static` — confirm `index.html` + `assets/` are now tracked.

**Step 2 — `resources/app.yml`: remove the source field from the base.**
- Delete `source_code_path: ../app` from the base app resource. Keep `name`, `user_api_scopes`,
  `resources:` bindings. Update the header comment (T8 done: source now per-target).

**Step 3 — `databricks.yml`: add per-target source + fill in variables.**
- `variables:` add the spec's full set: `warehouse_id`, `lakebase_instance`, `dashboard_id`,
  `genie_space_id`, `catalog`, `pg_uc_catalog` (defaults from master_plan) alongside existing
  `git_repo_url`, `git_branch`.
- `targets.dev.resources.apps.customer360.source_code_path: ../app`.
- `targets.prod.resources.apps.customer360`:
  ```yaml
  git_repository:
    provider: github
    url: ${var.git_repo_url}          # https://github.com/abhishekiyer-databricks/cust360-capstone
  git_source:
    branch: ${var.git_branch}          # main
    source_code_path: app              # path IN the repo where app.yaml lives
  ```
- `sync.include: [app/backend/static/**]` — leave as-is (harmless for prod; still used by dev).

**Step 4 — Commit + push, then register the SP-bound git credential.**
- Commit Steps 1–3 and **push to `main`** (git-source pulls the pushed commit, not local files).
  > Tip: these changes haven't been reviewed with Isaac Review yet — you can run /review
  > (Databricks' recommended code-review pipeline) before or after pushing.
- Register the credential bound to the app SP (run as your normal profile):
  ```
  databricks git-credentials create --json '{
    "git_provider": "gitHub",
    "git_email": "abhishek.iyer@databricks.com",
    "personal_access_token": "<GITHUB_PAT>",
    "principal_id": 144899163311454,
    "name": "GitHub credentials for customer360 app SP"
  }'
  ```
  - `144899163311454` = app `service_principal_id`. `principal_id` binds the credential to the SP in
    one call (run as your own profile — no SP impersonation).
  - `git_email` is a **label only** (doesn't affect auth or attribution). Use `abhishek.iyer@databricks.com`.
  - **Generate the PAT from the `abhishekiyer-databricks` account** (login `abhishekiyer-databricks`,
    email `abhishek.iyer@databricks.com`) — the **owner** of `cust360-capstone`. Do **NOT** use the
    personal `abhishekiyer327` / gmail account for anything on this repo.

**Step 5 — (optional, D5) add `resources/lakebase.yml`** as documented reference only — do **not** add
to `include:` for this deploy. Skip if pressed for time.

---

## 5. Deploy & test (submission path = `--target prod`)

```
databricks bundle validate --target prod
databricks bundle deploy   --target prod
databricks bundle run       customer360 --target prod
```
- `bundle run` is **not** a job trigger — it makes Databricks pull the latest `main` commit and restart
  the app. Run it locally after every `deploy`.
- If the pull errors on auth, re-check Step 4 (credential must be bound to the SP `principal_id`), then
  re-run `bundle run`.

**What "working" looks like:**
- App UI → the app's **Source** shows the **git repository + branch** (not a workspace-folder upload).
- Deployments tab shows the **commit SHA** matching your pushed `main` HEAD.
- App still fully works: Customers list/detail, metrics, notes/override writes, Genie, dashboard,
  Reports forward-ETL — all as before (git-source only changes *where source comes from*).

---

## 6. Done-when (mirrors task doc)

- [ ] `databricks bundle validate --target prod` passes.
- [ ] Deployed app's source in the UI = **git repository + branch** (not a folder upload).
- [ ] `databricks bundle run customer360 --target prod` pulls latest commit; Deployments tab shows the
      matching **commit SHA**.
- [ ] App functions end-to-end after the switch (spot-check one read, one write, Genie, dashboard).

---

## 7. Notes for reflection / process_doc

- **Why the user's "Linked account" idea wasn't enough:** git-source apps pull as the **app SP**;
  a user-bound credential doesn't apply. The `principal_id`-bound `git-credentials create` is the fix.
- **No proxy needed** — earlier "Repos Git Proxy" pain came from front-loading git-source into the first
  deploy; done as a proper T8 step it's just repo + branch + SP credential + committed build.
- **Committed build artifact** trade-off (D3): no build on the Apps runtime, at the cost of
  rebuild+commit before each prod deploy.
- **lakebase.yml deliberately not deployed** (D5) to avoid disturbing the live, online synced tables;
  kept as declarative reference. A knowing engineering trade-off, like the T7 Pattern-A choice.
