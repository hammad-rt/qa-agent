# Packs and helpers

A **pack** is everything project-specific. The engine (`agent_tester/`) must stay free of
Savostore (or any client) hard-coding.

## Layout

```text
packs/<slug>/
  pack.yaml          # apps, envs, allowlist, actions, helper config
  facts.md           # addresses, roles, UI recipes, known bugs (no secrets)
  helpers/*.py       # optional project helpers
  evals/
    known_answers.json
    corrections.json
    source/          # past report PDFs for replay
packs/_template/     # copy with `pack new` or `onboard`
```

## `pack.yaml` essentials

| Key | Purpose |
|---|---|
| `slug` / `name` | Pack identity |
| `redmine.project_ids` | Which Redmine projects map to this pack |
| `apps.<key>.envs` | URLs per `staging` / `preprod` / `prod` |
| `apps.<key>.accounts` | Usernames + `password_env` names only |
| `manual_login_apps` | CAPTCHA apps (person logs in once) |
| `allowlist` | Known hosts that need no plan-gate confirm |
| `preapprovable_actions` / `always_ask_actions` | Approval policy |
| `risky_patterns` | Regexes that force approval on matching clicks |
| `memory_areas` | Lesson preload keys |
| `helpers.<name>` | Path / labels / fields that enable a helper |

Never put passwords, API keys, or card numbers in the pack. Store secrets in `.env`
via the masked prompt (`write_env_secret`).

Validate after edits:

```bash
.venv/bin/python -m agent_tester pack validate <slug>
```

## Helper contract

Each helper module must define:

```python
HELPER = {
    "name": "read_wallet",
    "app": "portal",          # or "any"
    "changes_data": False,
    "action_type": None,      # policy action type if it writes
    "description": "...",
    "params": {"account": "..."},
    "needs_config": ["path", "labels"],  # required under helpers.<name>
}

async def run(browser, ctx, **params) -> dict:
    # return {"values": {...}, "screenshots": [...], "notes": "..."}
```

Rules:

- Core helpers live in `agent_tester/helpers/core/`; pack helpers override same name.
- A helper stays **disabled** until every `needs_config` key is set in `pack.yaml`
  (fill from a live run — never guess labels).
- Passwords are read from env inside the helper; never return or log them.
- Data-changing helpers use the same approval gate as browser clicks.
- Prefer `run_helper` from the tester once a helper is enabled.

Commands:

```bash
.venv/bin/python -m agent_tester helpers list --project <slug>
.venv/bin/python -m agent_tester helpers candidates --project <slug>
```

## Onboarding a new project

```bash
.venv/bin/python -m agent_tester onboard <issue_id_or_project>
# or
.venv/bin/python -m agent_tester pack new <slug>
```

`onboard` drafts `pack.yaml` + `facts.md` from Redmine (addresses only), asks one batch of
questions, and saves only after an explicit yes.

## Current Savostore status (HANDOVER)

- All project helpers are still empty in `pack.yaml` → disabled until paths/labels come from a live session.
- Priority helpers once filled: `crm_move_order_status`, `crm_add_parcel`, `crm_ship_parcel`, plus an "open record by number" helper (A1148 vs 1148 search confusion).
- Wiki credentials pages: read **addresses only** — never copy secrets into packs or memory.
