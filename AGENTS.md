# Agent guide (qa-agent)

**Stack for decisions:** always-on Cursor rules `stack`, `qa-agent-core`, `live-run-gaps`, plus skill [`qa-stack`](.cursor/skills/qa-stack/SKILL.md). Detailed prose: [`docs/stack/`](docs/stack/), status/gaps: [`docs/HANDOVER.md`](docs/HANDOVER.md).

| Need | Open |
|---|---|
| Choose ADK vs fixed code vs helper vs pack | rule `stack`, skill `qa-stack` |
| Edit `agent_tester/**/*.py` | rule `agent-tester-python` |
| Edit `packs/**` | rule `packs` |
| Run / resume / report | skill `run-qa-test` |
| New project | skill `onboard-pack` |
| New helper | skill `write-helper` |
| Failed live run | skill `debug-live-run` |
| Audit gaps / code readiness | subagent `.cursor/agents/auditor.md` |
| Labels / workflow | `core/core-testing-spec.md` |

Do not invent features ahead of HANDOVER §9. Secrets only via masked prompt → `.env`.
