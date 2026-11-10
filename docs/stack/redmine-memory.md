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
