---
name: onboard-pack
description: >-
  Onboard or validate a qa-agent project pack (pack.yaml, facts, helpers) that configures
  Redmine, Playwright sites, and approvals. Use for onboard/pack new/validate or new projects.
---

# Onboard a pack

Packs are the **project configuration layer** of the stack (see skill `qa-stack`, rule `packs`).

## Flow

```bash
.venv/bin/python -m agent_tester onboard <issue|project>   # preferred
.venv/bin/python -m agent_tester pack new <slug>
.venv/bin/python -m agent_tester pack validate <slug>
```

## Stack mapping to fill carefully

| Pack field | Affects |
|---|---|
| `redmine.project_ids` | Which tasks use this pack |
| `apps.*.envs` | Playwright allowed sites for the run |
| `password_env` | `.env` keys only — no values in yaml |
| `manual_login_apps` | Headed MCP login before cases |
| `allowlist` / actions / `risky_patterns` | `tools/policy.py` |
| `helpers.*` | Enables MCP helpers when live labels set |

Wiki → addresses only. Save onboard draft only after explicit yes. Template: `packs/_template/`.
