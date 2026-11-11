# Agent guide (qa-agent)

**Before any code change / PR:** run skill [`pr-gate`](.cursor/skills/pr-gate/SKILL.md) (step one). It runs the regression / env-leak / UI subagents, both-mode unit tests, and the Regression audit PR body. Leave PRs unmerged for laptop testing. No smoke/screenshots unless asked.

**Always-on rules:** `preserve-behaviour`, `protected-behaviours`, `graphify`, `stack`, `qa-agent-core`, `live-run-gaps`, `root-cause-first`, plus `agent-tester-python` / `packs` when those trees change.

**Code navigation:** query the local graph before grep. Prefer `graphify query` / `graphify explain` / `graphify path` (skill [`.agents/skills/graphify`](.agents/skills/graphify/SKILL.md); rule `graphify`). Rebuild with `graphify update .` after structural edits. Fall back to grep only for literal strings the graph cannot answer.

**Stack for decisions:** skill [`qa-stack`](.cursor/skills/qa-stack/SKILL.md). Prose: [`docs/stack/`](docs/stack/), status: [`docs/HANDOVER.md`](docs/HANDOVER.md).

**Prompt rules (`agent_tester/agents/prompts.py`):** add a rule as a whole new `- ` line after a complete one; indented continuations only. `tests/test_prompt_lint.py` enforces this.

**Web console:** [`web-ui-ux`](.cursor/skills/web-ui-ux/SKILL.md) + [`ui-ux-pro-max`](.cursor/skills/ui-ux-pro-max/SKILL.md). Polling must not steal focus or wipe typed text.

| Need | Open |
|---|---|
| **Open / update a PR** | skill **`pr-gate`** first |
| Regression audit of a branch | `.cursor/agents/regression-auditor.md` |
| Env-bound identity / secrets / fixtures | `.cursor/agents/env-leak-checker.md` |
| Web tabs / guidance / focus | `.cursor/agents/ui-invariants-checker.md` |
| HANDOVER gaps / readiness | `.cursor/agents/auditor.md` (also points at the three above) |
| Choose ADK vs fixed code vs helper vs pack | rule `stack`, skill `qa-stack` |
| Edit `agent_tester/**/*.py` | rule `agent-tester-python` |
| Edit `packs/**` | rule `packs` |
| Run / resume / report | skill `run-qa-test` |
| New project / helper | `onboard-pack` / `write-helper` |
| Failed live run / browser session | `debug-live-run` / `debug-browser-session` |
| Any bug fix | rule `root-cause-first` |
| Locked behaviours | rule `protected-behaviours`, `tests/test_protected_behaviours.py` |
| Find code / architecture | `graphify query` / `explain` / `path` (before grep) |
| Labels / workflow | `core/core-testing-spec.md` |

Do not invent features ahead of HANDOVER §9. Secrets only via masked prompt → `.env`. Core stays app-agnostic; no project names in core seeds. No env-bound identity in facts, memory, discovery_maps, learned.md, settings, or golden-trace fixtures.

## Learned User Preferences

- Prefer a single start command that finishes the run; use `resume` only for intentional human gates (CAPTCHA, approvals), not for transient agent/LLM errors.
- After actions are pre-approved for a run, do not re-prompt before each matching step—the agent should execute them itself.

## Learned Workspace Facts

- Staging may pre-approve `change_settings` at the plan gate; `redmine_post` / `send_email` / `prod_write` stay one-by-one; prod never pre-approves.
- Ending Playwright MCP stdio without a graceful `browser_close` hard-kills Chrome and drops cookies; persist login via clean close plus storage-state (e.g. `portal.json`), and cap re-login offers per app per run.
- Helper `McpBrowser` must send Playwright MCP element args as `target` (not `ref`); `_do` must raise on MCP errors so silent “ok” failures cannot hide empty logins.
- Offer manual login only for pack `manual_login_apps` (real CAPTCHA apps); CRM and other non-CAPTCHA apps use `login_form`, not `needs_manual_login` after a failed password type.
- Fake-browser unit tests alone miss Playwright MCP schema drift; prefer a check that compares helper args to the live MCP tool schema.
