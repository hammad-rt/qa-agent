---
name: debug-live-run
description: >-
  Diagnose qa-agent live runs using ADK traces, Playwright evidence paths, and HANDOVER stack gaps.
  Use for AGENT INCOMPLETE, NOT VERIFIED, crashes, or runs/ folder investigation.
---

# Debug a live run

Skill `qa-stack` + rule `live-run-gaps` explain *why* these failure modes exist.

## Artifacts (stack outputs)

```text
runs/<run>/state.json     # plan, results, checkpoints, manual_logins
runs/<run>/trace.jsonl    # ADK / tool trace
runs/<run>/progress.log
runs/<run>/shots/          # intended Playwright MCP evidence
runs/<run>/profiles/<app> # MCP user-data-dir (login state for the run)
```

Also scan **repo root** for stray `*.png` (MCP cwd save — evidence path bug).

## Symptom → stack cause

| Symptom | Stack cause | Fix direction |
|---|---|---|
| Missing shots in report | MCP saved relative name to cwd | Absolute under `shots/`; assert exists |
| NOT VERIFIED | Screenshot without `record_value` | Require observation in code |
| CRM no records / budget | Helpers off; search format; 40 calls | Enable helpers; scale budget |
| CAPTCHA / BLOCKED | Portal reCAPTCHA | Manual MCP login; never auto-solve |
| Invisible login window | MCP child missing DISPLAY | Pass full env; `--headed` |
| Model unreachable | LiteLLM host/VPN | `check` / fail-fast; resume later |
| Context length | Huge SuiteCRM snapshot in ADK turn | Prune/cap; token accounting TODO |
| API case SKIPPED | UI-only tools | Read-only MCP network tools |

`AGENT INCOMPLETE` ≠ app bug. Resume: never replay a turn that already wrote.

```bash
.venv/bin/python -m agent_tester resume runs/<folder>
.venv/bin/python -m unittest discover -s tests -t .   # offline anywhere
```
