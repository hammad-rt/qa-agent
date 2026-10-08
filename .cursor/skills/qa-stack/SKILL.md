---
name: qa-stack
description: >-
  qa-agent stack guide for informed code changes (Google ADK, LiteLLM, Playwright MCP,
  packs/helpers, Redmine, SQLite memory). Use when adding agents, tools, browser wiring,
  helpers, pack fields, LLM config, memory, reports, or choosing between fixed code vs model.
---

# qa-stack — make informed changes

Also load always-on rules `stack`, `qa-agent-core`, `live-run-gaps`. Deep notes: `docs/stack/*`, `docs/HANDOVER.md`.

## Choose the right layer

| Change request | Put it here | Stack piece |
|---|---|---|
| Plan / judge / navigate unfamiliar UI | `agents/*.py` prompts + tools | ADK `LlmAgent` + LiteLLM |
| Case order, approvals, resume, baseline | `coordinator.py`, `gate.py` | Plain Python |
| Click policy, hosts, risky patterns | `tools/policy.py` | Code gate over MCP tools |
| Repeatable UI recipe | `helpers/core` or `packs/<slug>/helpers` | MCP via `McpBrowser` |
| Client URLs, accounts, known bugs | `packs/<slug>/` | YAML + facts.md |
| Secret | `.env` via masked prompt | never model / pack / memory value |
| Tracker fetch/post | `preflight/redmine.py` | Redmine REST |
| Lesson / recipe / known_bug | `memory/` | SQLite (+ optional Ollama embed) |
| PDF/HTML output | `report/` | Jinja2 + headless Chrome |

**Default bias:** if it can be deterministic, keep it out of the LLM.

## ADK + LiteLLM checklist

- [ ] Model string `openai/<id>`; `api_base` includes `/v1`
- [ ] JSON mode only for planner/reviewer/judge
- [ ] Tester gets tools + free-form; smaller `TESTER_MAX_TOKENS`
- [ ] `retry=True` never on post-write tester turns
- [ ] Lazy import; offline tests still pass
- [ ] litellm pin awareness (`>=1.84`, skip 1.82.7/8)

Upstream: https://adk.dev/agents/models/litellm/ · https://google.github.io/adk-docs/

## Playwright MCP checklist

- [ ] Launch via existing `playwright_toolset` pattern (profile per app, shots dir, viewport)
- [ ] Full env for headed/CAPTCHA (`DISPLAY` / `WAYLAND_DISPLAY`)
- [ ] No `--allowed-origins`; sites from run URLs + user OK
- [ ] Keep blocked tools blocked
- [ ] Evidence = absolute path under run `shots/`; assert exists
- [ ] Optional `--caps` only with explicit reason (vision/run_code = danger)

Upstream: https://playwright.dev/mcp/introduction

## Helper checklist

- [ ] `HELPER` + `async run(browser, ctx, **params)`
- [ ] `needs_config` filled from **live** UI before enable
- [ ] Password from env inside helper; not in return/log
- [ ] Writes set `changes_data` / `action_type` for policy
- [ ] `pack validate` after yaml changes

## Redmine / memory checklist

- [ ] Pack selected by `project_ids`
- [ ] Posts (`bugs`/`cases`) per-item approval
- [ ] Memory: redact + scrub secrets; store `secret_env` names only
- [ ] Promote to `core` only with approval after stripping project detail

## After coding

```bash
.venv/bin/python -m unittest discover -s tests -t .
# live only on laptop + VPN:
.venv/bin/python -m agent_tester check
```

Prefer HANDOVER §9 order when the change is "make live runs better".
