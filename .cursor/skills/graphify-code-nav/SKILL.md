---
name: graphify-code-nav
description: >-
  Navigate this repo with Graphify before grep or whole-file reads. Use for any
  codebase question, finding callers/callees, tracing paths between symbols, or
  auditing shared helpers before a change. App-agnostic.
---

# Graphify code navigation

Prefer the local graph (`graphify-out/`) over grep and full-file reads. Install: `uv tool install graphifyy` (package name has two y's) or `pip install graphifyy`. Ensure `~/.local/bin` is on `PATH`. Rebuild: `graphify update .` (~6s, no LLM/API key).

Also keep the upstream skill at [`.agents/skills/graphify`](../../../../.agents/skills/graphify/SKILL.md) (from `graphify install --project --platform cursor` / `agents`).

## When to use which command

| Need | Command |
|------|---------|
| Where is it defined? Callers / callees? | `graphify explain "<symbol>"` |
| How does A reach B? (e.g. API → tester) | `graphify path "A" "B"` |
| Open architecture / "where is X handled?" | `graphify query "<question>" --budget N` |
| Exact error string, CSS id, env var name | **grep** (literal only) |
| After code edits / before audit | `graphify update .` |

### `graphify explain "<symbol>"`

Use for a named function, class, or module member. Returns definition location and neighbors (callers, callees). **Before changing any shared function**, run explain and list every caller in the PR Regression audit.

```bash
graphify explain "_budget_check"
graphify explain "DiscoveryMapStore.upsert_route"
```

### `graphify path "A" "B"`

Trace how one piece reaches another (endpoint → coordinator → tester, helper → MCP, etc.).

```bash
graphify path "ensure_discovery" "_persist_discovered_routes"
graphify path "run_agent" "LLMOutputError"
```

### `graphify query "<question>" --budget N`

Open questions. Default to a **small budget** (e.g. `--budget 2000` or `3000`); raise only if the answer was truncated. Prefer focused questions over dumping the whole graph.

```bash
graphify query "where is stuck ask screenshot chosen" --budget 2500
```

### Grep — last resort

Only for **exact literals** the graph cannot answer:

- Error / log substrings (`"no text for 90s"`)
- CSS / DOM ids (`#guideIn`, `stuckAct`)
- Env var names (`LLM_IDLE_TIMEOUT`)

Do not grep for concepts ("how does discovery reuse work") — use `query` / `explain` / `path`.

## Reading files

- Open files **only at the lines** graphify returns (`loc=L…`). Do not read whole files by default.
- If `graphify-out/GRAPH_REPORT.md` exists, read **only the sections you need** — never the whole report unless auditing architecture end-to-end.
- If `graphify-out/wiki/index.md` exists, prefer navigating the wiki over raw dumps.

## Keep the graph fresh

```bash
graphify update .          # after edits; before regression / env-leak / UI audit
graphify hook install      # optional: local clone hook so updates run on commit
```

`graphify-out/` is gitignored and rebuilt in `.cursor/environment.json` on Cloud Agent install.

## PR Regression audit

When you change a shared symbol, include in the PR body:

| Symbol changed | Callers (from `graphify explain`) | Covering test |
|----------------|-------------------------------------|---------------|
| `foo` | `a.py`, `b.py` | `tests/test_…` |

## App-agnostic

No client/pack names in queries or examples beyond generic symbols. Core stays project-neutral.
