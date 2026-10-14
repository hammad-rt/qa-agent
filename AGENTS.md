# Agent guide (qa-agent)

**Stack for decisions:** always-on Cursor rules `stack`, `qa-agent-core`, `live-run-gaps`, `root-cause-first`, plus skill [`qa-stack`](.cursor/skills/qa-stack/SKILL.md). Detailed prose: [`docs/stack/`](docs/stack/), status/gaps: [`docs/HANDOVER.md`](docs/HANDOVER.md).

**Web console work:** follow skill [`web-ui-ux`](.cursor/skills/web-ui-ux/SKILL.md) (no jumping on live updates, ASK prompts, headless check).

| Need | Open |
|---|---|
| Choose ADK vs fixed code vs helper vs pack | rule `stack`, skill `qa-stack` |
| Edit `agent_tester/**/*.py` | rule `agent-tester-python` |
| Edit `packs/**` | rule `packs` |
| Run / resume / report | skill `run-qa-test` |
| New project | skill `onboard-pack` |
| New helper | skill `write-helper` |
| Failed live run | skill `debug-live-run` |
| Browser / login / CAPTCHA session bug | skill `debug-browser-session` |
| Any bug fix | rule `root-cause-first` (evidence before edits) |
| Audit gaps / code readiness | subagent `.cursor/agents/auditor.md` |
| Labels / workflow | `core/core-testing-spec.md` |

Do not invent features ahead of HANDOVER §9. Secrets only via masked prompt → `.env`.

## Learned User Preferences

- Prefer a single start command that finishes the run; use `resume` only for intentional human gates (CAPTCHA, approvals), not for transient agent/LLM errors.
- After actions are pre-approved for a run, do not re-prompt before each matching step—the agent should execute them itself.

## Learned Workspace Facts

- Staging may pre-approve `change_settings` at the plan gate; `redmine_post` / `send_email` / `prod_write` stay one-by-one; prod never pre-approves.
- Ending Playwright MCP stdio without a graceful `browser_close` hard-kills Chrome and drops cookies; persist login via clean close plus storage-state (e.g. `portal.json`), and cap re-login offers per app per run.
- Helper `McpBrowser` must send Playwright MCP element args as `target` (not `ref`); `_do` must raise on MCP errors so silent “ok” failures cannot hide empty logins.
- Offer manual login only for pack `manual_login_apps` (real CAPTCHA apps); CRM and other non-CAPTCHA apps use `login_form`, not `needs_manual_login` after a failed password type.
- Fake-browser unit tests alone miss Playwright MCP schema drift; prefer a check that compares helper args to the live MCP tool schema.
