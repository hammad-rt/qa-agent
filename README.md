# qa-agent (phase 1)

Multi-project Redmine test agent. Design: `docs/test-agent-rdp.md`, `docs/test-agent-technical-spec.md`.
General rules: `core/core-testing-spec.md`. Project packs: `packs/<slug>/` (Savostore is the first).

## Phase 1 contents
- Fixed code: Redmine fetch (`preflight/redmine.py`), pack loading, gap resolver
  (pack, memory, wiki, task, then one batch of questions; masked passwords into `.env`),
  plan checks, action policy (allowlist, risky clicks, approvals, run pre-approvals),
  run state with checkpoints and resume, code-built action log, values and safe `calculate`,
  redaction, a simple HTML/JSON report.
- LLM roles on Google ADK (`agents/`): planner + reviewer, one tester per app with its own
  Playwright MCP browser, judge. The coordinator is plain code and runs one case at a time.

## Setup
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    cp .env.example .env && chmod 600 .env     # add REDMINE_API_KEY
    npx -y @playwright/mcp@latest --help        # first run downloads the MCP server

## Use
    .venv/bin/python -m agent_tester check
    .venv/bin/python -m agent_tester test 180966 --case TC3 --dry-run   # plan + gaps only
    .venv/bin/python -m agent_tester test 180966 --case TC3
    .venv/bin/python -m agent_tester resume runs/<run folder>

## Tests (offline)
    .venv/bin/python -m unittest discover -s tests -t .

## Not in phase 1
Memory with lessons and embeddings (phase 2), checkpoint/limits polish (3), full report and modes (4),
replay self-test (5), helpers (6), second project (7).
