"""Bug drafts in the core format (core spec section 5). Drafts only: posting is a separate, approved step."""
from __future__ import annotations

import json
from pathlib import Path

from ..redact import redact

SEVERITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, None: 4}


def build_drafts(state, pack) -> dict:
    """Return {"drafts": [...], "still_present": [...]} and save bug_drafts.json/.md in the run folder.
    A failure matching a known open bug is listed as still present, never drafted as new (FR-22a)."""
    plan = state.plan
    by_id = {c.id: c for c in plan.cases}
    known = {b["n"]: b for b in pack.known_bugs}
    drafts, still = [], {}
    envs = {app: pack.url(app, plan.env) for app in pack.apps}
    for cid, raw in state.data["results"].items():
        r = state.result(cid)
        if r.verdict not in ("FAIL", "PARTIAL"):
            continue
        if r.known_bug:
            b = known.get(r.known_bug, {"n": r.known_bug, "severity": "?", "text": "(not in facts.md)"})
            still.setdefault(r.known_bug, {**b, "cases": []})["cases"].append(cid)
            continue
        case = by_id.get(cid)
        did = f"D{len(drafts) + 1}"
        steps = [s.text for s in case.steps] if case else r.steps_done
        drafts.append({
            "id": did, "title": r.bug_title or (f"{case.title}: {r.actual[:80]}" if case else r.actual[:100]),
            "severity": r.severity or ("High" if r.verdict == "FAIL" else "Medium"),
            "environment": f"{plan.env}: " + ", ".join(f"{a} {u}" for a, u in envs.items() if u),
            "steps": steps, "expected": r.expected or (case.expected if case else ""), "actual": r.actual,
            "values": r.values, "evidence": r.evidence, "linked_cases": [cid], "task_id": plan.task_id,
            "what_this_means": r.note or "", "status": "draft", "posted_issue": None,
            "generated_case": bool(case and case.generated),
        })
        if did not in r.bug_draft_ids:
            r.bug_draft_ids.append(did)
            state.data["results"][cid] = r.model_dump(mode="json")
    drafts.sort(key=lambda d: SEVERITY_ORDER.get(d["severity"], 4))
    out = {"drafts": drafts, "still_present": sorted(still.values(), key=lambda b: b["n"])}
    prev = load(state.dir)
    for d in out["drafts"]:                       # keep posting status across rebuilds
        old = next((p for p in prev.get("drafts", []) if p["linked_cases"] == d["linked_cases"]), None)
        if old:
            d["status"], d["posted_issue"] = old["status"], old["posted_issue"]
    save(state.dir, out)
    state.save()
    return out


def load(run_dir: Path) -> dict:
    p = run_dir / "bug_drafts.json"
    return json.loads(p.read_text()) if p.exists() else {"drafts": [], "still_present": []}


def save(run_dir: Path, data: dict) -> None:
    (run_dir / "bug_drafts.json").write_text(redact(json.dumps(data, indent=2, default=str)))
    (run_dir / "bug_drafts.md").write_text(to_markdown(data))


def draft_text(d: dict) -> str:
    """Redmine (textile-friendly) body for one draft."""
    lines = [f"*Severity:* {d['severity']}", f"*Environment:* {d['environment']}", "", "*Steps to reproduce:*"]
    lines += [f"# {s}" for s in d["steps"]]
    lines += ["", f"*Expected:* {d['expected']}", f"*Actual:* {d['actual']}"]
    if d.get("values"):
        lines += ["", "*Values:* " + ", ".join(f"{k} = {v}" for k, v in d["values"].items())]
    if d.get("evidence"):
        lines += ["", "*Evidence:* " + ", ".join(d["evidence"])]
    lines += ["", f"*Linked cases:* {', '.join(d['linked_cases'])} (task #{d['task_id']})", "",
              f"*What this means:* {d['what_this_means']}"]
    if d.get("generated_case"):
        lines += ["", "_Found by a test case the agent wrote from the task description. Confirm the expected result before filing._"]
    return redact("\n".join(lines))


def to_markdown(data: dict) -> str:
    out = ["# Bug drafts (not posted)", ""]
    for d in data["drafts"]:
        out += [f"## {d['id']} [{d['severity']}] {d['title']}  ({d['status']}"
                + (f", #{d['posted_issue']}" if d.get("posted_issue") else "") + ")", "", draft_text(d), ""]
    if data["still_present"]:
        out += ["# Known bugs still present", ""]
        out += [f"- Bug {b['n']} [{b['severity']}]: {b['text']} (cases {', '.join(b['cases'])})" for b in data["still_present"]]
    return redact("\n".join(out)) + "\n"


def review_and_post(run_dir: Path, poster, policy, out=print) -> list[int]:
    """Show each unposted draft and post only the ones the user approves, one by one (core spec 5).
    poster(draft, body) -> new issue id. `redmine_post` is an always-ask action, so the policy's
    approver asks for every single draft; a denial skips it."""
    from ..tools.policy import PolicyViolation
    data = load(run_dir)
    posted = []
    for d in data["drafts"]:
        if d["status"] != "draft":
            continue
        out(f"\n{d['id']} [{d['severity']}] {d['title']}\n{draft_text(d)}")
        try:
            policy.gate(f"post bug draft {d['id']} to Redmine", "bug drafts", action_type="redmine_post")
        except PolicyViolation:
            d["status"] = "skipped"
            continue
        d["posted_issue"] = poster(d, draft_text(d))
        d["status"] = "posted"
        posted.append(d["posted_issue"])
        save(run_dir, data)
    save(run_dir, data)
    return posted
