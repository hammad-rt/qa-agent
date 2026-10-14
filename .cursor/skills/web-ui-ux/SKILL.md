---
name: web-ui-ux
description: Use when changing the qa-agent web console (agent_tester/web/index.html or server.py) - layout, live updates, forms, prompts, run CRUD. Covers the UX rules the page must keep and how to verify a change headlessly.
---
# Web console UI/UX

The console is one file (`agent_tester/web/index.html`, no build step) served by `agent_tester/web/server.py`
on localhost only. It polls: jobs every 1 s, the open run every 2 s, the run list every 5 s.

## Rules (keep these)
1. **Never move the user.** Polling must not scroll the window, a pane, or steal focus.
   - Render polled content with `setHTML(el, html)`, never `el.innerHTML=`. It skips unchanged HTML, keeps
     scroll position and open `<details>`, and does not re-render while the user is typing in that area.
   - Auto-scroll a log only when it was already at the bottom (`nearBottom(el)` before appending).
   - Focus a new input only when nothing else has focus, with `{preventScroll:true}`.
2. **Questions from the run** arrive as `\x1eASK {"prompt","secret"}` lines (see `auto.ask_line`). Show the full
   prompt (multi-line, `white-space:pre-wrap`). Mask only when `secret` is true; never guess from words.
   Masked answers stay `********` in the console.
3. **Errors are sentences.** Every API failure shows what happened and what to do (e.g. "restart
   agent_tester web"). A running run can't be deleted (409): say so.
4. **Layout:** `*{box-sizing:border-box}`, grid columns `minmax(0,1fr)`; long text wraps, nothing overflows the
   sidebar. Keep ids unique (a duplicate id once emptied the Cases tab).
5. **Destructive actions** (delete run, screenshot, memory) ask `confirm()` first.
6. Deep link `#run=<id>&tab=<pane>` must keep working.

## Verify every change
```bash
node -e "const h=require('fs').readFileSync('agent_tester/web/index.html','utf8');new Function(h.match(/<script>([\s\S]*)<\/script>/)[1])"
.venv/bin/python -m agent_tester web --port 8799 &
google-chrome --headless=new --no-sandbox --window-size=1366,900 --virtual-time-budget=6000 \
  --screenshot=/tmp/ui.png "http://127.0.0.1:8799/#run=<run>&tab=cases"
```
Look at the screenshot, check each tab, then run the unit tests. Tell the user to restart `agent_tester web`
after server.py changes and hard refresh (Ctrl+Shift+R) after index.html changes.
