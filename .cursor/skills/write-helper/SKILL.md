---
name: write-helper
description: >-
  Author or enable a Playwright-MCP browser helper (core or pack) for qa-agent run_helper.
  Use when adding helpers/*.py, filling pack.yaml helpers:, or stabilizing SuiteCRM/portal steps.
---

# Write / enable a helper

Helpers are **fixed code over Playwright MCP** (skill `qa-stack`). Prefer them over longer tester prompts once UI labels are known from a live session.

## Stack contract

```python
HELPER = {"name": "...", "app": "crm"|"portal"|"any", "changes_data": bool,
          "action_type": None|str, "description": "...", "params": {...},
          "needs_config": ["path", "labels", ...]}

async def run(browser, ctx, **params) -> dict:  # values / screenshots / notes
    ...
```

- Browser API: `agent_tester.helpers.McpBrowser` (navigate, snapshot, click/type by label, screenshot).
- Do **not** call `browser_evaluate` / run_code. Do **not** put passwords in returns.
- Data-changing helpers share the approval gate with MCP clicks.
- Enable only after live `pack.yaml` `helpers.<name>` fills `needs_config`.

## Savostore (HANDOVER)

Helpers still empty → off. Fill CRM helpers first + open-by-number (`A1148` vs `1148`).

```bash
.venv/bin/python -m agent_tester helpers list|candidates --project savostore
.venv/bin/python -m agent_tester pack validate savostore
```

Examples: `helpers/core/login_form.py`, `packs/savostore/helpers/read_wallet.py`.
