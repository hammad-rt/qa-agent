"""Tools the tester drafts for itself. A draft is one Playwright function `async (page, args) => {...}` that runs
through Playwright MCP. Code checks it, the person running the test approves it, and only then is it saved as a
pack helper (packs/<slug>/helpers/gen_<name>.py) that this and every later run can call with run_helper.
Rejected or unanswered drafts go to helpers/proposed/ for review. Each use still passes the normal gates."""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

MAX_LEN = 3000
NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
HEAD = re.compile(r"^\s*async\s*\(\s*page\s*,\s*args\s*\)\s*=>\s*\{", re.S)
BANNED = [
    (r"cookie", "reads cookies"), (r"storageState|localStorage|sessionStorage|indexedDB", "reads stored sessions"),
    (r"passw|passcode|otp|secret|token|credential", "touches secrets"),
    (r"\bprocess\b|\brequire\s*\(|\bimport\s*\(", "reaches outside the page"),
    (r"\bfetch\s*\(|XMLHttpRequest|WebSocket|sendBeacon|\.route\s*\(|setExtraHTTPHeaders|\.request\.", "makes its own network calls"),
    (r"\.goto\s*\(|newPage|newContext|window\.open|location\s*(\.href)?\s*=", "opens other pages (navigate with the browser tools instead)"),
    (r"log\s*-?\s*out|sign\s*-?\s*out", "logs out"),
    (r"\beval\s*\(|new\s+Function|Function\s*\(", "runs generated code"),
    (r"'''|\\x|\\u00", "hides text in escapes"),
    (r"\.browser\s*\(|\bbrowser\.", "controls the whole browser"),
]


def check(name: str, js: str, existing: set[str]) -> list[str]:
    out = []
    if not NAME.match(name or ""):
        out.append("name must be lower_snake_case, 3-41 characters")
    if name in existing or f"gen_{name}" in existing:
        out.append(f"a helper called {name} already exists; use it with run_helper")
    if len(js or "") > MAX_LEN:
        out.append(f"script is longer than {MAX_LEN} characters; keep it to one small job")
    if not HEAD.match(js or ""):
        out.append("script must be exactly one function: async (page, args) => { ... }")
    for pat, why in BANNED:
        if re.search(pat, js or "", re.I):
            out.append(f"not allowed: it {why}")
    return out


def render(name: str, description: str, params: dict, js: str, changes_data: bool, action_type: str | None,
           case_id: str) -> str:
    meta = {"name": name, "app": "any", "description": description[:300], "changes_data": bool(changes_data),
            "action_type": action_type or None, "params": {str(k): str(v)[:120] for k, v in params.items()},
            "needs_config": []}
    return (f'"""Drafted by the tester in {case_id} on {date.today().isoformat()} and approved by the user.\n'
            f'Edit or delete freely."""\nimport json\n\nHELPER = {json.dumps(meta, indent=1)}\n'
            .replace(": true", ": True").replace(": false", ": False").replace(": null", ": None")
            + f"\nJS = r'''{js}'''\n\n\nasync def run(browser, ctx, **params):\n"
            "    code = \"async (page) => { const args = \" + json.dumps(params) + \"; return await (\" + JS + \")(page, args); }\"\n"
            "    out = await browser._do(\"browser_run_code_unsafe\", {\"code\": code})\n"
            "    return {\"values\": {}, \"screenshots\": [], \"notes\": str(out)[:1500]}\n")


def save(pack_root, name: str, source: str, approved: bool) -> Path:
    d = Path(pack_root) / "helpers" / ("" if approved else "proposed")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"gen_{name}.py"
    p.write_text(source)
    return p
