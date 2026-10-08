# Playwright MCP (how qa-agent drives the browser)

Upstream: [Introduction](https://playwright.dev/mcp/introduction), [Capabilities](https://playwright.dev/mcp/capabilities), [Configuration](https://playwright.dev/mcp/configuration/options).

## Why MCP, not raw Playwright for the tester

The tester LLM sees accessibility snapshots with element `ref`s and calls MCP tools
(`browser_click`, `browser_type`, …). Fixed helpers and PDF rendering use the Python
`playwright` package separately.

## How this repo launches it

From `agent_tester/agents/tester.py`:

```text
# Manual CAPTCHA login (once per app):
npx -y @playwright/mcp@latest
  --user-data-dir <run>/profiles/<app>
  --output-dir <run>/shots
  --viewport-size 1366,900
  --caps=storage                 # only for manual_login (browser_storage_state)

# Tester after a saved login:
npx -y @playwright/mcp@latest
  [--headless]                   # omitted when QA_HEADED=1 / --headed
  --isolated --storage-state <run>/storage/<app>.json
  --output-dir <run>/shots
  --viewport-size 1366,900

# Tester before any login (no storage file yet):
npx -y @playwright/mcp@latest
  [--headless]
  --user-data-dir <run>/profiles/<app>
  --output-dir <run>/shots
  --viewport-size 1366,900
```

Important project choices:

- **No `--allowed-origins`.** Playwright treats it as scheme://host:port and blocks CDNs / payment pages. Site limits are enforced in code (`Policy` + `TesterSession.after_tool`).
- **Session handoff:** after manual login, save `runs/<run>/storage/<app>.json` via `browser_storage_state`, then `browser_close` (never SIGKILL Chrome). Testers reopen with `--isolated --storage-state` so session-only cookies still apply.
- **`--caps=storage` only on manual login** so `browser_storage_state` is available; tester sessions do not get storage caps.
- Pass the full process env (especially `DISPLAY` / `WAYLAND_DISPLAY`) or headed login windows stay invisible.
- Apps in `manual_login_apps` get a visible browser once before cases; mid-run `needs_manual_login` re-offers **once** only for those same apps, then BLOCKED. Other apps are not offered a person login.

## Tools the tester may use

Allow-ish (read-only examples): `browser_snapshot`, `browser_take_screenshot`,
`browser_wait_for`, `browser_tab_list`, `browser_console_messages`,
`browser_network_requests`, `browser_navigate_back`, `browser_hover`.

Blocked in code: `browser_evaluate`, `browser_run_code` / `browser_run_code_unsafe`,
`browser_install`, `browser_file_upload`.

Every navigate / click still goes through the approval / allowlist / risky-pattern gate
in `agent_tester/tools/policy.py`. The action log is built from those callbacks — never
from the model's narration.

## Snapshot workflow (helpers)

Helpers wrap MCP via `agent_tester.helpers.McpBrowser`:

1. `navigate` → `snapshot`
2. Parse lines with `[ref=…]` (`parse_snapshot`)
3. `click` / `type` by label (+ optional role), not CSS selectors
4. Screenshot + return `{"values", "screenshots", "notes"}`

## Caps we do not enable by default

Upstream optional groups (`--caps=network,storage,testing,vision,pdf,devtools`) stay off
unless a deliberate change adds them. Exception: manual login enables `--caps=storage` only
long enough to call `browser_storage_state`. Vision (coordinate clicks) and `run_code_unsafe`
are especially dangerous for a QA agent that must not invent side effects.

## Debugging

```bash
.venv/bin/python -m agent_tester test <id> --case TC1 --headed
QA_HEADED=1 .venv/bin/python -m agent_tester resume runs/<folder>
```

Inspect `runs/<folder>/shots/`, `snapshots/`, `trace.jsonl`, and `progress.log`.
If the browser closes after every action, suspect ADK MCP session teardown (see
[google-adk-litellm.md](google-adk-litellm.md)).

## Evidence paths

`evidence_name` returns an **absolute** path under `runs/<run>/shots/`. Playwright MCP resolves
`filename` against process cwd, so bare names used to land in the repo root. Helpers pass the same
absolute `shot_prefix`. Report HTML embeds `shots/<basename>` only when that file exists.

## Remaining live-run gaps (see HANDOVER §8)

- SuiteCRM cases burn the 40-call budget; fill pack helpers from a live session before relying on CRM flows.
- CAPTCHA: never solve it; use `manual_login_apps` / `needs_manual_login`, or BLOCKED.
- Verify steps still need a recorded observation (gap #2) — screenshots alone can still yield NOT VERIFIED.
