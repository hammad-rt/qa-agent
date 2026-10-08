# Stack reference (qa-agent)

Project-specific notes for the libraries this agent uses. Design intent lives in
`docs/test-agent-technical-spec.md` and `docs/HANDOVER.md`; this folder is the
day-to-day cheat sheet with upstream links.

> **For agents:** stack decision rules are embedded in `.cursor/rules/stack.mdc` (always on) and skill `.cursor/skills/qa-stack/`. This folder is the longer cheat sheet with upstream links — load it when the rule/skill is not enough.

| Layer | Choice | Project docs |
|---|---|---|
| Language | Python 3.12+ venv | — |
| Agent framework | Google ADK (`google-adk`) | [google-adk-litellm.md](google-adk-litellm.md) |
| LLM access | LiteLLM → OpenAI-compatible vLLM | [google-adk-litellm.md](google-adk-litellm.md) |
| Browser | Playwright MCP (`@playwright/mcp`) | [playwright-mcp.md](playwright-mcp.md) |
| Project config | Packs + helpers | [packs-helpers.md](packs-helpers.md) |
| Tracker / memory | Redmine REST + SQLite memory | [redmine-memory.md](redmine-memory.md) |

## Upstream docs (bookmark these)

- [ADK docs](https://google.github.io/adk-docs/) / [ADK LiteLLM](https://adk.dev/agents/models/litellm/)
- [ADK Python repo](https://github.com/google/adk-python)
- [Playwright MCP introduction](https://playwright.dev/mcp/introduction)
- [Playwright MCP capabilities](https://playwright.dev/mcp/capabilities)
- [Playwright MCP configuration](https://playwright.dev/mcp/configuration/options)
- [LiteLLM OpenAI-compatible endpoints](https://docs.litellm.ai/docs/providers/openai_compatible)
- [Model Context Protocol](https://modelcontextprotocol.io/)
- [Redmine REST API](https://www.redmine.org/projects/redmine/wiki/Rest_api)
- [Pydantic v2](https://docs.pydantic.dev/latest/)

## Pin / version notes (as of Oct 2026)

- `requirements.txt`: `google-adk>=1.10`, `litellm>=1.60`, `mcp>=1.9`, `pydantic>=2.7`
- Prefer `litellm>=1.84` when upgrading (ADK docs requirement; avoid compromised 1.82.7/1.82.8)
- Playwright MCP launched via `npx -y @playwright/mcp@latest` (stdio), not a pinned npm dep
- Model: `openai/gemma4` at `LLM_BASE_URL` (Rolustech vLLM); only reachable on VPN / Rolustech network

## Local setup reminders

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
npx -y @playwright/mcp@latest --help   # downloads Chromium on first use
.venv/bin/python -m agent_tester check
```
