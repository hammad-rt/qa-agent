# qa-agent (phases 1-3)

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

## Phase 2: memory
- SQLite store at `memory/memory.db` (keyword search built in; optional Ollama `nomic-embed-text` embeddings).
  Scopes: `core` for every project, `project:<slug>` for one. Kinds: fact, lesson, recipe, known_bug,
  open_bug_found, resource. Text is redacted and env secret values are scrubbed before saving.
- The planner gets a short preload of lessons for the task's areas; testers can `recall_lessons` and
  `propose_memory` (proposals wait for approval at the end of the run). The judge gets lessons and
  likely known-bug matches from the pack.
- `correct` saves your verdict correction as an active lesson right away. Recipes that fail twice and
  lessons linked to a now-closed Redmine bug are set to needs_recheck. Lessons go to core only with your
  approval, after project details are stripped. A readable copy is exported to `memory/memory-export.json`.

## Phase 3: safety and reliability
- Run-level pre-approval of action types, always-ask actions, prod read-only, masked secrets, redaction
  of state files and traces.
- Read-only baseline and final snapshots; a test-data ledger tagged `qa-<run>`; admin settings must be
  restored to the recorded original or the case becomes PARTIAL.
- Limits per case, checkpoint after every turn, `resume`; Ctrl-C pauses cleanly. A tester turn that already
  made an approved change is never replayed (the run pauses instead).
- Retries with backoff for planner, reviewer and judge calls; one run at a time (lock file); keeps the last
  5 runs in full and trims screenshots and traces after 14 days; warns when disk is low; `trace.jsonl` per run.

## Setup
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    cp .env.example .env && chmod 600 .env     # add REDMINE_API_KEY
    npx -y @playwright/mcp@latest --help        # first run downloads the MCP server

## Use
    .venv/bin/python -m agent_tester check
    .venv/bin/python -m agent_tester test 180966 --case TC3 --dry-run   # plan + gaps only
    .venv/bin/python -m agent_tester test 180966 --case TC3
    .venv/bin/python -m agent_tester resume runs/<run folder>
    .venv/bin/python -m agent_tester correct runs/<run folder> TC4 PASS "why the verdict was wrong" [--general]
    .venv/bin/python -m agent_tester memory list|pending --project savostore
    .venv/bin/python -m agent_tester memory approve|retire <id...> --project savostore
    .venv/bin/python -m agent_tester leftovers runs/<run folder>

## Tests (offline)
    .venv/bin/python -m unittest discover -s tests -t .

## Status
Phases 1-3 are built and covered by offline tests. No live run yet: the LLM endpoint and staging must be
reachable from the machine running the agent, and the Redmine API key and account passwords must be set.
Next: full report and modes (4), replay self-test (5), helpers (6), second project (7).
