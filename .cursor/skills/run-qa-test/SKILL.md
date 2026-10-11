---
name: run-qa-test
description: >-
  Run, resume, report, or correct a qa-agent Redmine test using the ADK + Playwright MCP stack.
  Use for agent_tester test/resume/report/bugs/cases or live/dry-run execution.
---

# Run a QA test

Load skill `qa-stack` if changing how the run is wired. Rules: `stack`, `qa-agent-core`. HANDOVER §§4–5, §10.

## Stack involved

- **LLM:** LiteLLM → `LLM_BASE_URL` (VPN). Check fails fast if host won't resolve.
- **Browser:** Playwright MCP per app; portal may need manual CAPTCHA login.
- **Config:** pack from Redmine project id; secrets only in `.env`.
- **Outputs:** `runs/<folder>/` state, trace, shots, report HTML/PDF, bug drafts.

## Preconditions

- Live runs on **laptop** only (`/var/www/html/qa-agent`). No package installs unless asked.
- `.env` mode 600; never print secrets.

## Commands

```bash
.venv/bin/python -m agent_tester check
.venv/bin/python -m agent_tester test <id> [--case TC3] [--dry-run] [--env staging|pre-prod|production] [--headed]
.venv/bin/python -m agent_tester resume runs/<folder>
.venv/bin/python -m agent_tester report|leftovers runs/<folder>
.venv/bin/python -m agent_tester bugs|cases runs/<folder> [--post]  # approve each
.venv/bin/python -m agent_tester correct runs/<folder> TC4 PASS "why" [--general]
```

## Gate behaviour (product of stack policy)

- Env each run; production explicit yes; no prod pre-approvals.
- Users each run; passwords masked into `.env` per user+env.
- Plan-gate answers → replan. Extra browser hosts → one OK.
- Never solve CAPTCHA. Staging may pre-approve `change_settings` at the plan gate; prod/post/email stay one-by-one.

Failures → skill `debug-live-run`.
