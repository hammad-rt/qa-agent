# qa-agent: how it works

qa-agent reads a Redmine ticket, plans the tests, runs them in a real browser and writes a report with screenshots.
It is written in Python on Google ADK (Agent Development Kit). The AI model is our own Gemma 4 server
(`ai.rolustech.com/v1`) and Chrome is driven through Playwright MCP.

**Design rule:** the AI is used only where judgment is needed: planning, finding its way around the page,
inferring missing steps, verdicts and notes. Plain code does everything else: Redmine, logins, reading values,
math, reports and approval gates. The agent never guesses field names, labels or selectors. It reads the actual page.

## 1. A run, step by step

1. **Fetch** (code): it pulls the ticket, its "Test Cases - …" task, attachments and the wiki pages listed in the pack from Redmine.
   - **Feature mode** (the default): if the ticket has subtasks, it walks the subtasks, their subtasks and "related to" tickets (up to 30 tickets).
   - It collects every Test Cases task. A ticket with no Test Cases task gets cases written for it, marked as *generated*.
   - Turn it off with `--no-subtasks`.
2. **Plan** (AI plus code):
   - The planner turns the cases into steps, and a reviewer checks the plan against the ticket.
   - Inferred steps are labelled as inferred. Steps that change data are marked as needing approval.
   - On SuiteCRM and SugarCRM, code adds Quick Repair and Rebuild as TC0, which runs once, and every other case depends on it.
   - In feature mode, case ids are prefixed with the ticket number (`181133.TC2`). Exact duplicates are merged into one case that lists every ticket it covers.
3. **Questions** (code): before running, it lists what's missing or unclear and asks the user: test data, the account to use, URLs, or ambiguous steps. Confirmed answers are saved to memory.
4. **Test** (AI plus code): a tester agent works through each case in the browser. It reads the page with snapshots, then clicks and types. Code handles logins, value reads, math and screenshots.
5. **Verdict** (AI): a judge agent reads the step log and evidence and gives PASS, FAIL, PARTIAL, BLOCKED, NOT VERIFIED or SKIPPED, with notes.
6. **Report and bugs** (code):
   - It builds an HTML report with screenshots, grouped by ticket in feature mode.
   - It drafts bugs, which are posted to Redmine through Hammad Iqbal's account only after the user approves.
   - It can also email the report.
7. **End of run** (code): it lists suggested memory items for approval, rechecks stored bugs whose tickets are now closed, offers to restore changed test data from the ledger, and exports memory as a backup.

## 2. How the tools are connected

| Connection | How |
|---|---|
| **Browser** | **Playwright MCP.** Code starts `@playwright/mcp` with `npx` for each case and connects through ADK's `McpToolset`, which gives the tester the browser tools (navigate, snapshot, click, type, and so on). Each run has its own Chrome profile and screenshot folder. After one login, the session is saved to a file (`runs/<run>/storage/<app>.json`) and reused by every tester. |
| **AI model** | ADK's `LiteLlm` talks to the OpenAI-style API at `ai.rolustech.com/v1` (model `gemma4`). Each job (planner, tester, judge) can use a different model through `LLM_<JOB>_MODEL` and `LLM_<JOB>_BASE_URL` in `.env`. The planner and judge must answer in JSON, which code checks. If the model server goes down, runs wait for it to come back. |
| **Redmine** | Not an MCP. Plain code calls its REST API with the key from `.env`. It reads tickets, attachments and wiki pages, and posts only after approval. |
| **Tester's own tools** | Functions written in code: `record_value` (save a value exactly as read), `calculate` (math over recorded values), `request_approval`, a stack-docs lookup, and helpers such as `login_form` and `read_table`. Project helpers live in `packs/<project>/helpers/` (Savostore: create order, add/ship parcel, move order status, apply rewards, read wallet). New helpers the agent generates need approval before they're used. |
| **CAPTCHA** | Staging only. A local ONNX image classifier (`models/recaptcha_cls_224.onnx`) is tried first, then the vision model, then a manual login in a visible browser. |

## 3. How rules and guides are set

- **Instructions per agent:** `agent_tester/agents/prompts.py` has the instructions for the planner, reviewer, tester, discovery, judge and case writer. They set what each agent may do, that it must read the page first, and the exact output it must return.
- **Project pack** (`packs/savostore/`):
  - `pack.yaml` lists the apps and their URLs for staging, pre-prod and production, the accounts (only the *name* of the environment variable holding each password), the Test Cases prefix, wiki pages, the stack and its docs, and which apps need a manual login.
  - `facts.md` is hand-written knowledge about how the apps behave, plus known bugs.
  - `learned.md` holds notes the agent writes (see section 4).
- **Rules enforced in code, not left to the AI:** ADK callbacks run around every model and tool call.
  - *Before each tool call* (`tools/policy.py`, `TesterSession.before_tool`):
    - It works out whether the action changes data and, if so, asks for approval.
    - On **production**, every save, edit, type, select or toggle asks every time, and payments are always refused. If nobody is there to approve, the step is denied.
    - Running raw JavaScript, uploading files and installing browsers are blocked.
    - Navigating to a site the run hasn't used needs approval.
  - *After each tool call* (`after_tool`):
    - It checks which site the browser ended up on.
    - It records the real outcome, so a timeout counts as an error, not "ok".
    - It saves screenshots.
    - It watches for loops: a warning after 3 repeats and a stop at 5.
  - *Budget:* reading the page doesn't count. Each step gets 30 actions, and the count resets when the step is marked done.
  - *Before each model call* (`before_model`): old page snapshots are trimmed so the context stays small.
- **Secrets:** passwords live only in `.env`, and code types them in. The AI never sees them, and they're masked in logs, reports and memory.
- **Shared session:** the agent never logs out unless the case is about logging out.

## 4. Memory and knowledge base

Three kinds of memory (detail: `docs/stack/redmine-memory.md`):

| Kind | Where | Notes |
|---|---|---|
| Stable rules (`fact`) | SQLite, project scope for app rules | Date + who decided (`source` / `decided_by`). Agent-proposed rules need approval. |
| Lessons (`lesson` / `recipe`) | SQLite (`core` or `project:<slug>`) | Lasting after user confirm or ≥2 run confirmations. |
| Per-run log | Run folder + `memory/run-history/` | Ledger original→changed→restored; never lasting facts. |

| Store | What | Who maintains it |
|---|---|---|
| `packs/<project>/facts.md`, `pack.yaml` | How the app works, business rules, known bugs, stack | People, edited in git. The AI only reads them. |
| `memory/memory.db` (SQLite) | Source of truth for lasting facts, lessons, recipes, bugs | Code. Proposals wait for approval (or ≥2 confirmations for lessons/recipes). |
| `packs/<project>/learned.md` | Routes / Stuck / Discovery only (scrubbed of env details) | Written by code. Not dumped into the LLM — only a few ranked lines for the current area. |
| `packs/<project>/docs/<app>.md` | Passages from the official stack docs it looked up | Saved the first time it needs them and read offline after that. |

**Lean injection:** each turn gets a small ranked memory block (`MEMORY_TURN_TOKEN_BUDGET`, default ~1200 tokens), cached per area:app for the run. More detail is on-demand via `recall_lessons`. Retrieval is code-side (no extra LLM). End-of-run retrospective is built from the action log in code.

**Env details** (emails, logins, URLs, record ids, passwords) are refused at write time and never kept as lasting memory; URLs/accounts come from pack / `.env` / this run’s answers.

**Test data ledger:** every data change a run makes (the record and its old and new values) is written to the run's ledger, so it can be restored at the end.

## 5. Using it

From `/var/www/html/qa-agent`:

    .venv/bin/python -m agent_tester web                          # local web UI (localhost only): start runs, live steps, answer questions, reports
    .venv/bin/python -m agent_tester test 178955 --env staging --dry-run   # plan and questions only
    .venv/bin/python -m agent_tester test 178955 --env staging             # run (feature mode is automatic)
    .venv/bin/python -m agent_tester test 183498 --cases TC1,TC3 --no-subtasks
    .venv/bin/python -m agent_tester rerun 183498 --failed        # rerun the cases that didn't pass
    .venv/bin/python -m agent_tester resume runs/<run folder>
    .venv/bin/python -m agent_tester correct runs/<run folder> TC4 PASS "why"
    .venv/bin/python -m agent_tester memory pending --project savostore

Each run's plan, step log, screenshots and report are kept in `runs/<run folder>/`.
