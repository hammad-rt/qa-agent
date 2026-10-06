# qa-agent (phases 1-8)

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

## Phase 4: report and modes
- Full HTML + PDF report (headless Chrome): summary, top new bugs, known bugs still present, per-case steps,
  expected vs actual, values, "What this means", screenshots, baseline vs final, test data, questions asked.
- Bug drafts in the core format (`bug_drafts.md`). Posting to Redmine is one approval per draft (`bugs --post`).
- Modes: `--cases`, `--retest-bug N`, `rerun`, `report`. Progress in `progress.log`.
- Email (`--email`, `--email-progress`) only to REPORT_EMAIL_TO; any other address is confirmed at the console.

## Phase 5: replay self-test
- `packs/<slug>/evals/known_answers.json` is rebuilt from past report PDFs in `evals/source/` (counting rules,
  bug numbers mapped to facts.md by text, secrets masked, user corrections from `evals/corrections.json`).
- `eval --live` judges each recorded result and scores it: pass bar is 90% agreement, no known FAIL judged PASS,
  and repeat bugs labelled "still present: bug N". Without `--live` it checks the plumbing only.

## Phase 6: helpers
- Core helpers in `agent_tester/helpers/core/` (login_form, read_table); project helpers in `packs/<slug>/helpers/`.
- The tester calls them with `run_helper`. Same allowlist, read-only and approval checks as its own clicks;
  data-changing helpers always go through the approval gate. login_form types the password from the env var
  itself, so the model never sees it.
- Project helpers stay off until their page path and labels are set under `helpers:` in pack.yaml.
  `helpers list` shows status; `helpers candidates` lists recipes that worked 3+ times.

## Phase 7: more projects
- `onboard <issue|project>` drafts pack.yaml and facts.md from Redmine (addresses only, never credentials),
  asks one batch of questions, and saves only after a yes. `pack new <slug>` copies `packs/_template/`.
- `pack validate <slug>` checks keys, allowlist covers every app host, no literal secrets, project ids not
  shared with another pack, risky patterns compile, and the core always-ask actions are present.
- The pack is picked by the task's Redmine project id. Memory, allowlist, known bugs and helpers are per pack
  (see the fictional fixture in `tests/fixtures/packs/`).

## Live-run fixes (6 Oct)

- **Environment asked each run.** `test <id>` asks staging, pre-prod or production (Enter = staging), or take
  `--env staging|pre-prod|production`. URLs missing for an environment (pre-prod today) are asked once and
  saved with their source. Production asks for an explicit yes, never pre-approves anything, asks before every
  data change, and the staging test card is never pre-approved off staging.
- **Users asked each run.** Every run (reruns too) asks which portal and CRM user to use; Enter keeps the last
  one used for that environment. Passwords are kept per user and environment in `.env`
  (e.g. `SAVOSTORE_CRM_PASSWORD__HAMMAD_IQBAL`), asked in a masked prompt only the first time. Turn off per pack
  with `ask_accounts_each_run: false`.
- **Settings changes** are never pre-approved; the agent asks before each one instead of crashing.
- **Plans** read current values before testing around them, change records (like an exempt client) as explicit
  steps that are set back at the end, restore settings once after the last case, and reopen records for
  persistence checks. Answers at the plan gate are fed back to the planner.
- **Sites per run.** The browser may use only the sites of the URLs picked or confirmed for this run (any port).
  If the app sends it somewhere else, such as a payment page, it asks you once and then allows that site for the
  rest of the run. The pack `allowlist` is only a list of known sites that don't need confirming at the plan gate.
- **Restore pass.** If any setting is still changed after the run (even after failures), the agent lists it and
  offers to put it back.

## Phase 8: cases from the task description
- When a task has no "Test Cases - ..." child, the planner writes cases (G1, G2...) from the description,
  comments, parent and facts.md: main flow, boundaries for every limit, invalid input, cross-app checks.
- Code marks them generated and checks that every expected result quotes text really found in the task or
  facts. When the task doesn't say what should happen, you're asked at the plan gate (blank skips the case).
  You can drop cases (dependants go too) or add one-line cases before approving.
- The report and bug drafts label generated cases: a FAIL there means "app or task wording needs a look".
- `cases <run folder>` shows them; `cases <run folder> --post` creates a "Test Cases" child in Redmine,
  only after you approve.

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
    .venv/bin/python -m agent_tester test 165348 --cases A2,A9 --email
    .venv/bin/python -m agent_tester test 165348 --retest-bug 1
    .venv/bin/python -m agent_tester rerun 165348 --cases A14
    .venv/bin/python -m agent_tester report runs/<run folder>
    .venv/bin/python -m agent_tester bugs runs/<run folder> [--post]
    .venv/bin/python -m agent_tester eval --project savostore [--live] [--rebuild]
    .venv/bin/python -m agent_tester helpers list|candidates --project savostore
    .venv/bin/python -m agent_tester onboard <issue id or project>
    .venv/bin/python -m agent_tester pack new|validate <slug>
    .venv/bin/python -m agent_tester cases runs/<run folder> [--post]

## Tests (offline)
    .venv/bin/python -m unittest discover -s tests -t .

## Status
Phases 1-8 are built and covered by 121 offline tests. No live run yet: the LLM endpoint (internal only) and
staging must be reachable from the machine running the agent, and the Redmine API key and account passwords
must be set. Nothing in code is task-specific, but that only gets proven by live runs on several different
tasks (for example #180966, #165348 and a fresh one) and a passing `eval --live`.

## CAPTCHA logins and tester memory (6 Oct, #180974)
- Each app gets one browser profile per run (`runs/<run>/profiles/<app>`), so a login lasts the whole run.
- Apps in the pack's `manual_login_apps` (Savostore: `portal`) ask at the start of the run to open a visible
  browser where you log in once and solve the CAPTCHA. If a tester still meets a CAPTCHA it calls
  `needs_manual_login`, you get the same window once, and the case is retried; if you skip it the case is BLOCKED.
- The tester keeps only its last 2 page reads in history (`TESTER_KEEP_PAGE_READS`), page text is cut at
  `TESTER_MAX_PAGE_CHARS` (30000), and its reply limit is `TESTER_MAX_TOKENS` (2048; the planner keeps
  `LLM_MAX_TOKENS`). A context-length error ends that case as AGENT INCOMPLETE instead of pausing the run.
- Planner: record status changes aren't settings (no restore step); records checked in a user's view must belong
  to the run's chosen user; prefer test records made for the run and leave their new status.
- The browser server now gets the full environment (DISPLAY / WAYLAND_DISPLAY), so the manual-login window really
  shows; after you press Enter it checks the page has left the login form. `--headed` on test/rerun/resume shows
  the testers' browsers too (for debugging).
