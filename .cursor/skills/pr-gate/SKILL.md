---
name: pr-gate
description: Mandatory pre-PR checklist for qa-agent — regression audit, env-leak, UI invariants, both-mode unit tests, PR body. Use before opening or updating any PR into fixes.
---

# PR gate (mandatory)

Run this **before** `ManagePullRequest` create/update for any code change into `fixes`. Do not merge; the user merges after laptop testing. No smoke runs or screenshots unless the user asks.

## 1. Subagents (in order)

1. **`.cursor/agents/regression-auditor.md`** — diff `origin/fixes...HEAD`; list every touched existing behaviour; check against `protected-behaviours.mdc` and tests.
2. **`.cursor/agents/env-leak-checker.md`** — scan the diff, packs touched, `learned.md`, memory exports, and **`tests/fixtures/`** for emails, hosts/URLs, record/order ids, secrets.
3. **`.cursor/agents/ui-invariants-checker.md`** — if `agent_tester/web/index.html` (or web CSS/JS) changed: tabs under title, always-visible guidance, focus/polling, stuck Continue/Skip/Stop.

Keep the general **auditor** (`.cursor/agents/auditor.md`) for HANDOVER gaps; point it at these three for PR work.

## 2. Unit tests (both modes)

```bash
REPORT_PDF=0 QA_TEST=1 QA_INTERACTIVE=0 python3 -m unittest discover -s tests -t .
REPORT_PDF=0 QA_TEST=1 QA_INTERACTIVE=1 python3 -m unittest discover -s tests -t .
```

(Prefer `.venv/bin/python` when the project venv exists.) Both must be OK.

## 3. Diff scope

```bash
git diff --stat origin/fixes...HEAD
```

Only task-needed files. Revert anything else.

## 4. PR body

Use `.github/pull_request_template.md`. Fill:

- **Regression audit** — table of touched behaviours vs preserved + covering test
- **Protected behaviours touched**
- **Env-leak check**
- **Tests** — both-mode counts

Use Context7 for library/API docs when unsure. Open the PR **unmerged**.
