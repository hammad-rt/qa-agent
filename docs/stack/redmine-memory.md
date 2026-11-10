# Redmine + memory store

## Redmine

Upstream: [REST API](https://www.redmine.org/projects/redmine/wiki/Rest_api).

Client: `agent_tester/preflight/redmine.py` (deterministic `requests`).

Env:

| Variable | Purpose |
|---|---|
| `REDMINE_URL` | e.g. `https://redmine.rolustech.com` |
| `REDMINE_API_KEY` | User API key (never commit) |

What the agent fetches for a task:

- Parent issue, journals/comments, attachments, linked issues
- Child whose subject starts with `Test Cases - ` (configurable prefix)
- Wiki pages listed in the pack (gap lookup — never copy secrets from wiki)

Posts (bug drafts, generated cases child) always require per-item console approval
(`bugs --post`, `cases --post`). Production / tracker writes are `always_ask`.

Pack selection: Redmine project id → pack with matching `redmine.project_ids`.

## Memory: three kinds

| Kind | Home | Lasting? |
|---|---|---|
| **Stable rules** (`fact`) | SQLite, `project:<slug>` only for app rules | Yes, with `created_at` + `source` / `decided_by`. Agent-proposed facts stay `proposed` until user approval. |
| **Lessons** (`lesson` / `recipe`) | SQLite: general → `core`, app-specific → `project:<slug>` | Lasting only after user approval **or** confirmation in ≥2 runs (`confirm_count`). |
| **Per-run log** | `runs/<run>/` ledger + `retrospective.md`; append-only `memory/run-history/` | Never standing facts. Original → changed → restored values stay with the run. |

`known_bug` / `open_bug_found` stay in SQLite as today.

### SQLite vs `learned.md` (no duplication)

| Store | Holds | Used how |
|---|---|---|
| **SQLite** (`MEMORY_DB`, export `memory/memory-export.json`) | Source of truth for lasting facts, lessons, recipes, bugs | Ranked retrieval into the turn (see budget) |
| **`packs/<slug>/learned.md`** | Pack-local Routes / Stuck / Discovery only (scrubbed) | At most 1–2 stuck + few route hints for the **same area:app** — never dumped wholesale |
| **`memory/run-prefs.json`** (gitignored) | Last-used account defaults for ask-each-run | Not lasting knowledge; not injected into the LLM |

App-specific knowledge belongs only under `packs/<slug>/` (facts.md, learned.md, pack seeds). Core seeds are app-agnostic.

### Knowledge layers (domain / platform / project)

Retrieval merges three layers; **more specific wins**:

| Layer | Scope | When loaded | Trust |
|---|---|---|---|
| **DOMAIN** | `domain:crm`, `domain:erp` | Always (short CRM/ERP behaviour seeds) | Seed lessons; project may override |
| **PLATFORM** | `platform:<id>` e.g. `suitecrm7`, `suitecrm8`, `sugarcrm` | Only when detection finds that platform | Starting assumptions (`trust=hint`); never fully trusted |
| **PROJECT** | `project:<slug>` + pack `learned.md` / facts | Always for the active pack | Always wins over domain/platform |

Seeds: domain notes live in `agent_tester/memory/seeds.py` (`DOMAIN_LESSONS`). Platform notes live under `packs/_platforms/<id>/` (YAML seeds + optional `docs` URLs) — not in core Python as project names. Project pack seeds stay under `packs/<slug>/` only.

**Overrides:** when live behaviour contradicts a domain or platform note, save a project-scoped lesson with key `overrides:<sha1>` whose text says what is *not* true here and what *is*. Retrieval suppresses the contradicted note for that project (see `memory/layers.py`).

### Verified discovery maps (reuse)

Per `(area, app, checklist item)` the memory DB keeps a scrubbed menu/label route keyed by env fingerprint (host + env; platform/version ride along). An item is reusable when verified (case `mark_step` after following the route, or covered by a prior/partial discovery pass), success age ≤ `DISCOVERY_REUSE_MAX_DAYS` (default 14), no failure since last success, and no change signal. Deploy markers (asset URL hash + version meta, code-only at run start) trigger a spot-check instead of blind reuse. Force with `--rediscover`, the web “Force rediscovery” checkbox, or `DISCOVERY_REUSE=0`. Routes flush incrementally (including on stop / SIGINT). Env-bound identity is refused. Discovery itself is read-only: admin/system actions (Quick Repair, Execute SQL, cache clear, …) may only be *located*, never run.

### Platform detection (discovery / env fingerprint)

During discovery, code inspects page markup, login screens, URL patterns, and (when reachable read-only) version/meta tags. Results are stored on the discovery / env fingerprint entry — **not** as lasting facts:

```text
platform: { id, confidence }
platform_version: { value, confidence }
```

Signals (examples): SuiteCRM 7 `index.php?module=`; SuiteCRM 8 `/#/` Angular routes; Sugar sidecar `#Module` routes. Unknown or custom is normal → only domain + project knowledge loads.

Module: `agent_tester/platform_detect.py`. Caps: detection is read-only and cheap (string/URL checks on snapshots already taken).

### Stuck lookup ladder

When the tester stalls or loops, **before** explore / `ask_user`, run this fixed order (`agent_tester/stuck_lookup.py`):

1. Memory retrieval (layered, budgeted)
2. Project **code** (read-only) — only if pack `code_root` or `CODE_ROOT` / `QA_CODE_ROOT` is set; Python walk + regex (no ripgrep); short `file:line` snippets
3. That platform version’s **official docs** — only if a platform was detected *and* a docs search provider is configured
4. Wider **web** — only if a search provider is configured
5. Explore (existing screen map)
6. `ask_user`

Query is built from stuck context (goal, page, what was already tried). Caps: `STUCK_LOOKUP_MAX_SNIPPETS` (default 3), `STUCK_LOOKUP_TIMEOUT_SEC` (default 8), stay inside `MEMORY_TURN_TOKEN_BUDGET` (1200). Fetched text is **untrusted**: summarize to 2–3 short snippets, cache by query, never pass raw pages, never approve or change data. Gates unchanged.

Providers (all optional; **off when unset**):

| Variable | Purpose |
|---|---|
| `DOCS_SEARCH_URL` | SearXNG-style HTTP search endpoint (`?q=` JSON or HTML) |
| `CONTEXT7_API_URL` | Optional Context7-compatible HTTP API base |
| `CONTEXT7_API_KEY` | Optional bearer/token for Context7 |
| `CODE_ROOT` / `QA_CODE_ROOT` | Default code tree when pack has no `code_root` |
| `STUCK_LOOKUP_TIMEOUT_SEC` | Cap wall time per ladder run (default 8) |
| `STUCK_LOOKUP_MAX_SNIPPETS` | Cap returned snippets (default 3) |

No new Python packages required (stdlib + existing `requests`). Optional external services stay laptop-approved.

### Learning from ladder hits

Snippets from code / docs / web are saved as **`proposed`** (unconfirmed) lessons at the right scope (`platform:…` for docs/web, `project:…` for code) with source path or URL + version when known. They become lasting only by the existing rule (confirmed in ≥2 runs or user approval). Two failures → `needs_recheck`. Env guard still blocks logins, record ids, test-client emails and hosts.

### Env-detail guard

Never store as lasting memory or in learned.md: test client names/emails, logins, order/record ids, hosts/URLs, passwords.

Enforced in code on every write path (`propose`, `correction`, `learned.add`, promotion, resource `put`).  
`resource` rows keep only the **secret env var name** (if any); values come from pack.yaml / `.env` / this run’s answers each time.

### Verify before trust

Rows carry `last_confirmed_at` (and optional `env_at_confirm`). Stale (`MEMORY_STALE_DAYS`, default 30) or post-deploy (env fingerprint changed) memory is a **hint** and must be re-verified live; failures → `needs_recheck`.

### End-of-run retrospective

Every run writes `runs/<run>/retrospective.md` from the action log and verdicts **in code** (no LLM required). Candidate lessons are deduped against the store; repeated confirmation bumps `confirm_count`. Optional polish: set `MEMORY_REVIEW_LLM=1` for one short capped LLM call.

### Lean injection (hard constraint)

Retrieval is **code-side** (SQL/FTS/embeddings) — never an extra LLM call to pick rows.

| Setting | Default |
|---|---|
| `MEMORY_TURN_TOKEN_BUDGET` | `1200` tokens (~4800 chars) total memory per turn |

Per case/turn the coordinator selects by area + app + query relevance, ranks by relevance / confirmed / success, truncates, dedupes, and caches per `area:app` for the run so later turns do not re-query or re-send the same ids. Run logs and retrospectives are never preloaded wholesale. More detail stays on-demand via `recall_lessons`.

Injected size is logged on `state.data["memory_injection_log"]` each turn.

```bash
.venv/bin/python -m agent_tester memory list --project savostore
.venv/bin/python -m agent_tester memory pending --project savostore
.venv/bin/python -m agent_tester memory approve|retire <id...> --project savostore
.venv/bin/python -m agent_tester correct runs/<folder> TC4 PASS "why" [--general]
```

## Result labels (from core testing spec)

`PASS` | `FAIL` | `PARTIAL` | `BLOCKED` | `NOT VERIFIED` | `SKIPPED` | `AGENT INCOMPLETE`

`AGENT INCOMPLETE` is an agent limit / stuck / missing action-log step — not an app bug.
Known open bugs matched in the pack/memory are labelled `still present: bug N` (no new draft).
