# Google ADK + LiteLLM (how qa-agent uses them)

Upstream: [ADK docs](https://google.github.io/adk-docs/), [LiteLLM connector](https://adk.dev/agents/models/litellm/), [adk-python](https://github.com/google/adk-python).

## Roles in this repo

| Role | Module | ADK usage |
|---|---|---|
| Planner / reviewer / judge | `agent_tester/agents/` | `LlmAgent` + `Runner` + `InMemorySessionService` |
| Tester (per app) | `agent_tester/agents/tester.py` | `LlmAgent` + Playwright `McpToolset` |
| Coordinator | `agent_tester/coordinator.py` | Plain code (not an ADK agent) |

Deterministic work (Redmine fetch, approvals, action log, math, reports) stays in fixed Python. The model only plans, navigates unfamiliar UI, and judges.

## Model factory

```python
# agent_tester/agents/llm.py
from google.adk.models.lite_llm import LiteLlm

LiteLlm(
    model=f"openai/{settings.llm_model}",  # e.g. openai/gemma4
    api_base=settings.llm_base_url,        # .../v1
    api_key=settings.llm_api_key,
    max_tokens=...,
    response_format={"type": "json_object"},  # when LLM_JSON_MODE=1
)
```

Rules that matter here:

- Always prefix with `openai/` so LiteLLM routes to an OpenAI-compatible `/v1/chat/completions`.
- Parameter is `api_base`, not `base_url`.
- `api_base` must include `/v1`.
- JSON mode is for planner / reviewer / judge only; testers need free-form tool calls.
- Cap concurrency with `LLM_MAX_CONCURRENCY` (semaphore in `run_agent`).
- Retries (`retry=True`) are only for side-effect-free agents. Never auto-replay a tester turn that already changed data — pause and `resume` instead.

## MCP toolset pattern

```python
from mcp import StdioServerParameters
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams

McpToolset(connection_params=StdioConnectionParams(
    server_params=StdioServerParameters(
        command="npx",
        args=["-y", "@playwright/mcp@latest", "--user-data-dir", profile, ...],
        env=os.environ,  # need DISPLAY / WAYLAND_DISPLAY for headed login
    ),
    timeout=60,
))
```

Known ADK issue: MCP sessions historically could recreate per tool call and close the browser. This project keeps one toolset / profile per app for the run (`runs/<run>/profiles/<app>`). If logins vanish mid-case, check ADK MCP session persistence first.

## Env knobs

| Variable | Purpose |
|---|---|
| `LLM_BASE_URL` | OpenAI-compatible base (`https://…/v1`) |
| `LLM_MODEL` | Model id without `openai/` prefix |
| `LLM_API_KEY` | Often `none` for internal vLLM |
| `LLM_JSON_MODE` | `1` enables `response_format=json_object` |
| `LLM_MAX_TOKENS` | Planner/judge reply budget (server default may be 512) |
| `LLM_TIMEOUT` / `LLM_CALL_TIMEOUT` | LiteLLM whole-response timeout in seconds (default 180); also caps each `run_agent` turn |
| `LLM_STREAM_TIMEOUT` | LiteLLM stream idle / next-chunk timeout (default 60) |
| `LLM_IDLE_TIMEOUT` | Fail the attempt when ADK events carry no text for this many seconds (default 90) |
| `PLAN_BATCH` | Max cases per planner batch (default 6) |
| `PLAN_CONCURRENCY` | Concurrent planner batches (default 1 so hung calls cannot starve retries) |
| `PLAN_MAX_SECONDS` | Hard wall-clock plan deadline; cancels in-flight LLM calls |
| `TESTER_MAX_TOKENS` | Tester reply budget (smaller to protect context) |
| `TESTER_KEEP_PAGE_READS` | How many page reads stay in tester history |
| `TESTER_MAX_PAGE_CHARS` | Truncate page text before it hits the model |

## Upgrade / security notes

- ADK requires `litellm>=1.84`. Avoid 1.82.7 / 1.82.8 (supply-chain advisory, Mar 2026).
- Import ADK lazily so offline unit tests can run without the heavy stack.
- There is no local fallback LLM: if the host does not resolve, fail fast (`model_host_ok`) and pause the run.
