# Stack reference (detail)

Mirror of decision-critical bits from `docs/stack/` for when `SKILL.md` is not enough.

## Env knobs

| Var | Role |
|---|---|
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | vLLM OpenAI-compatible endpoint |
| `LLM_JSON_MODE` / `LLM_MAX_TOKENS` / `LLM_MAX_CONCURRENCY` | Planner/judge JSON & budgets |
| `TESTER_MAX_TOKENS` / `TESTER_KEEP_PAGE_READS` / `TESTER_MAX_PAGE_CHARS` | Tester context |
| `REDMINE_URL` / `REDMINE_API_KEY` | Tracker |
| `MAX_TOOL_CALLS_PER_CASE` / `MAX_MINUTES_PER_CASE` | Per-case limits |
| `MEMORY_DB` / `OLLAMA_BASE_URL` / `EMBED_MODEL` | Memory (+ optional embeds) |
| `QA_HEADED` | Show tester browsers |
| `RUN_RETENTION` / `MIN_FREE_GB` | Disk hygiene |
| `REPORT_PDF` / `REPORT_EMAIL_TO` / SMTP_* | Report delivery |

Passwords: `SAVOSTORE_CRM_PASSWORD__<USER>` style names written by masked prompt.

## MCP launch (canonical)

See `agent_tester/agents/tester.py` → `playwright_toolset()`. Soft-dependency import handles `McpToolset` vs older `MCPToolset`.

## Helper module skeleton

```python
from agent_tester.helpers import HelperError

HELPER = {
    "name": "example",
    "app": "crm",
    "changes_data": False,
    "action_type": None,
    "description": "…",
    "params": {},
    "needs_config": ["path", "labels"],
}

async def run(browser, ctx, **params):
    await browser.navigate(ctx.url(ctx.config["path"]))
    page = await browser.snapshot()
    # … label finds, clicks …
    shot = await browser.screenshot(f"{ctx.shot_prefix}{ctx.case_id}-example.png")
    return {"values": {...}, "screenshots": [shot], "notes": ""}
```

## Upstream links

- ADK: https://google.github.io/adk-docs/
- ADK LiteLLM: https://adk.dev/agents/models/litellm/
- Playwright MCP: https://playwright.dev/mcp/introduction
- LiteLLM OpenAI-compatible: https://docs.litellm.ai/docs/providers/openai_compatible
- Redmine REST: https://www.redmine.org/projects/redmine/wiki/Rest_api
