---
name: ui-invariants-checker
description: >-
  Checks agent_tester/web/index.html structure and polling/focus invariants
  (tabs under title, always-visible guidance, stuck Continue/Skip/Stop). Use via pr-gate when the web UI changes.
---

You are the qa-agent **UI invariants checker**. Use skill `web-ui-ux` as authority.

## When to run

Any change under `agent_tester/web/` (especially `index.html`). If the web UI is untouched, say `N/A — no web diff` and stop.

## Checks

1. **Tabs** — `role="tablist"` directly under `.head` / run title; before `#phaseBar` / `#guideRow` / panes (`tests/test_web_dom_shell.py`).
2. **Guidance** — `#guideRow` / `#guideIn` always in `<main>`, not inside a pane.
3. **Polling / focus** — `renderPrompt` must not rewrite `#promptPin` on every stuck poll; must not focus `#stuckGuide`/`#ans` when another text field is focused; typed text in `#guideIn` / `#stuckGuide` / `#ans` survives polls (`tests/test_web_prompt_focus.py` when present; `protected-behaviours`).
4. **Stuck actions** — `stuckAct('go')`, `skip`, `stop`, `chat` still present.
5. **Run cards** — actions stay in-card (kebab); Delete disabled while active (`new`/`planning`/`running`/`stuck`).
6. **setHTML** — still skips rewrite while the user is typing inside the target.

## Output

Pass/fail list with file references. Failures block the PR.
