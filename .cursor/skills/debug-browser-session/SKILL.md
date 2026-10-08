---
name: debug-browser-session
description: >-
  Evidence checklist for qa-agent browser problems: login or CAPTCHA session not sticking, manual-login loop,
  "browser keeps crashing", profile locked, invisible window. Run it before editing tester/manual-login code.
---

# Debug a browser session problem

Rule `root-cause-first` applies: collect this evidence, state one cause, then change code.

## 1. Run the inspector (read-only)

```bash
.venv/bin/python .cursor/skills/debug-browser-session/inspect_run.py runs/<run>      # newest run if omitted
```

It prints, per `runs/<run>/profiles/<app>`:
- Chrome's exit flag (`Default/Preferences` → `profile.exit_type`): `Crashed` means Chrome was killed, not closed.
- Cookie rows in `Default/Cookies` (total, per host, persistent vs session-only) and when the file was last written.
- `SingletonLock` holder PID and whether that process is alive and what it is.
- Checkpoints from `state.json` (manual login time, case results) so you can compare times.
- Last `needs_manual_login` / `finish` calls in `trace.jsonl`.
- Live Chrome / Playwright MCP processes that use a `runs/` profile.

## 2. Read the result

| Evidence | Meaning | Fix direction |
|---|---|---|
| `exit_type: Crashed` + 0 cookies + Cookies older than the login checkpoint | Login never reached disk; Chrome was killed (SIGKILL, or MCP server exit without `browser_close`) | Close the browser with `browser_close` before leaving the MCP session; don't SIGKILL Chrome |
| Cookies present but `is_persistent=0` for the app host | Session-only cookie; Chrome drops it on every restart even after a clean close | Save storage state after login (`context.storageState`) and start testers from it (`--isolated --storage-state`), or keep one browser open for the run |
| Cookies present and persistent, still CAPTCHA | Site needs more than cookies (sessionStorage token, IP/UA check) | Check `Local Storage` / `Session Storage`; keep one long-lived browser per app |
| `SingletonLock` PID alive and is a Playwright Chrome | Previous browser still open; a second launch on the profile fails | Close the old one properly first; never start two Chromes on one profile |
| `SingletonLock` PID dead | Stale lock from a killed Chrome | Remove the stale lock, but find out who killed Chrome |
| Retry blocked seconds after "manual login done" | Session didn't carry over to the new browser | See the first three rows; also cap re-login to once per app per run |
| No window appears | MCP child has no `DISPLAY`/`WAYLAND_DISPLAY` | Pass the full env to `StdioServerParameters` |

## 3. Prove library behaviour before relying on it

Write a 20-line script, not an assumption: start Playwright MCP with `--user-data-dir`, log in or set a cookie,
close the way the code closes (`browser_close` vs ending the stdio session), relaunch, and check the cookie is
still there. Keep it as a test if it can run offline.

## 4. After the fix

- One change per commit, each checked with a real run.
- Regression test: log in → close → reopen → still logged in.
- Log one line for browser start, login saved, browser closed, process killed (with PID and time).

## Known case (Oct 2026, branch `fixes` ef0d0e7)

Runs `180974-20261008-153639` and `-155353`: both profiles `Crashed`, 0 cookies, Cookies last written before
the login. Cause: `manual_login` dropped `browser_close` and `release_profile` SIGTERM/SIGKILLs Chrome, so the
login never saved; the coordinator then re-offered login on every case (no cap) and `force_headed()` made each
killed window visible ("browser keeps crashing").
