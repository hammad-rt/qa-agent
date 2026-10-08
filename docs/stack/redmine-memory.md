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

## Memory (SQLite)

Path: `MEMORY_DB` (default `memory/memory.db`). Readable export:
`memory/memory-export.json`.

| Field | Values |
|---|---|
| Scope | `core` (all projects) or `project:<slug>` |
| Kind | `fact`, `lesson`, `recipe`, `known_bug`, `open_bug_found`, `resource` |
| Status | `active`, `proposed`, `needs_recheck`, `retired` |

Behaviour:

- Text is redacted; env secret values are scrubbed before save.
- Planner gets a short preload of lessons for the case area.
- Tester may `recall_lessons` / `propose_memory` (proposals wait for end-of-run approval).
- `correct` writes an active lesson immediately.
- Recipes that fail twice, or lessons tied to a now-closed Redmine bug → `needs_recheck`.
- Promote to `core` only with explicit approval after stripping project details.
- Embeddings optional via Ollama `nomic-embed-text`; otherwise keyword / FTS5 search.

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
