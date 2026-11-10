# Agent guide (qa-agent)

**Stack for decisions:** always-on Cursor rules `stack`, `qa-agent-core`, `live-run-gaps`, `root-cause-first`, plus skill [`qa-stack`](.cursor/skills/qa-stack/SKILL.md). Detailed prose: [`docs/stack/`](docs/stack/), status/gaps: [`docs/HANDOVER.md`](docs/HANDOVER.md).

**Prompt rules (`agent_tester/agents/prompts.py`):** add a rule as a whole new `- ` line after a complete one, never inside another rule; continuation lines are indented two spaces. `tests/test_prompt_lint.py` fails on spliced, orphaned or duplicate rules.

**Web console work:** follow skill [`web-ui-ux`](.cursor/skills/web-ui-ux/SKILL.md) (no jumping on live updates, ASK prompts, headless check) and use [`ui-ux-pro-max`](.cursor/skills/ui-ux-pro-max/SKILL.md) for visual and UX decisions (tokens, contrast, focus, icons); search it with `python3 .cursor/skills/ui-ux-pro-max/scripts/search.py "<query>" --domain ux`.

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

**Knowledge layers + stuck ladder:** domain (`domain:crm`/`domain:erp`) → platform (`packs/_platforms/<id>`, only when detected) → project (always wins). Detection stores `platform` / `platform_version` on the discovery fingerprint, not as facts. When stuck, run memory → code → platform docs → web → explore → ask_user (`docs/stack/redmine-memory.md`). Core stays app-agnostic; no project names in core seeds.

## Learned User Preferences

- Prefer a single start command that finishes the run; use `resume` only for intentional human gates (CAPTCHA, approvals), not for transient agent/LLM errors.
- After actions are pre-approved for a run, do not re-prompt before each matching step—the agent should execute them itself.

## Learned Workspace Facts

- Staging may pre-approve `change_settings` at the plan gate; `redmine_post` / `send_email` / `prod_write` stay one-by-one; prod never pre-approves.
- Ending Playwright MCP stdio without a graceful `browser_close` hard-kills Chrome and drops cookies; persist login via clean close plus storage-state (e.g. `portal.json`), and cap re-login offers per app per run.
- Helper `McpBrowser` must send Playwright MCP element args as `target` (not `ref`); `_do` must raise on MCP errors so silent “ok” failures cannot hide empty logins.
- Offer manual login only for pack `manual_login_apps` (real CAPTCHA apps); CRM and other non-CAPTCHA apps use `login_form`, not `needs_manual_login` after a failed password type.
- Fake-browser unit tests alone miss Playwright MCP schema drift; prefer a check that compares helper args to the live MCP tool schema.
