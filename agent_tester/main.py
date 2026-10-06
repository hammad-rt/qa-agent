"""CLI: python -m agent_tester <command>

  check                               connectivity to the LLM, Redmine and the pack's environments
  test <task_id> [--cases TC1,TC3] [--retest-bug N] [--env staging] [--dry-run] [--email] [--email-progress]
  rerun <task_id> --cases TC2,TC4     rerun chosen cases from the latest approved plan (no new planning)
  resume <run_dir>
  report <run_dir>                    rebuild report.html / report.pdf and bug drafts
  bugs <run_dir> [--post]             list bug drafts; --post asks before posting each one to Redmine
  correct <run_dir> <case_id> <VERDICT> "<why>" [--general]   save a lesson now and fix the result
  memory list|pending|approve <ids>|retire <ids> [--project savostore]
  leftovers <run_dir>                 test data and settings the run left behind
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from .config import ROOT, Settings


def open_memory(settings: Settings, pack):
    from .memory.embed import Embedder
    from .memory.service import Memory
    from .memory.store import MemoryStore
    mem = Memory(MemoryStore(settings.memory_db, Embedder()), pack.slug)
    n = mem.import_resources_json(ROOT / "memory" / "resources.json")
    if n:
        print(f"Moved {n} saved answer(s) from resources.json into memory.db.")
    return mem


def model_fingerprint(settings: Settings) -> dict:
    import requests
    try:
        r = requests.get(settings.llm_base_url.rstrip("/") + "/models", timeout=10)
        ids = [m.get("id") for m in r.json().get("data", [])]
        return {"requested": settings.llm_model, "served": ids, "base_url": settings.llm_base_url}
    except Exception as e:  # noqa: BLE001
        return {"requested": settings.llm_model, "error": f"{type(e).__name__}"}


def _closed_checker(rm):
    cache: dict[int, bool] = {}

    def closed(issue_id: int) -> bool:
        if issue_id not in cache:
            try:
                st = rm.issue(issue_id).get("status", {})
                cache[issue_id] = bool(st.get("is_closed")) or st.get("name", "").lower() in ("closed", "resolved", "rejected")
            except Exception:  # noqa: BLE001
                cache[issue_id] = False
        return cache[issue_id]
    return closed


def cmd_check(settings: Settings) -> int:
    import requests

    from .preflight.pack_loader import load_pack
    rows = []

    def probe(name, url, **kw):
        try:
            r = requests.get(url, timeout=10, **kw)
            rows.append((name, url, str(r.status_code)))
        except requests.RequestException as e:
            rows.append((name, url, f"unreachable ({type(e).__name__})"))

    probe("LLM", settings.llm_base_url.rstrip("/") + "/models")
    hdr = {"X-Redmine-API-Key": settings.redmine_api_key} if settings.redmine_api_key else {}
    probe("Redmine" + ("" if hdr else " (no API key)"), settings.redmine_url + "/users/current.json", headers=hdr)
    for p in sorted(settings.packs_dir.iterdir()):
        if (p / "pack.yaml").exists():
            pack = load_pack(p)
            for app in pack.apps:
                for env, url in pack.apps[app].get("envs", {}).items():
                    probe(f"{pack.slug} {app} {env}", url)
    for r in rows:
        print(f"{r[0]:<32} {r[2]:<28} {r[1]}")
    return 0


def cmd_test(settings: Settings, task_id: int, case: str | None, env: str, dry_run: bool,
             retest_bug: int | None = None, email: bool = False, email_progress: bool = False) -> int:
    from . import gate
    from .agents.planner import plan_task
    from .plan_checks import check_plan
    from .preflight import gap_resolver as G
    from .preflight.pack_loader import find_pack
    from .preflight.redmine import Redmine
    from .run_state import RunState
    from .tools.policy import from_pack

    rm = Redmine(settings.redmine_url, settings.redmine_api_key)
    print(f"Fetching #{task_id} from Redmine...")
    first = rm.issue(task_id)
    pack = find_pack(settings.packs_dir, [first["project"]["id"]])
    bundle = rm.fetch_task(task_id, pack.config["redmine"].get("test_case_subject_prefix", "Test Cases - "))
    if bundle.test_case_task is None and not bundle.issue["subject"].startswith("Test Cases"):
        print("No 'Test Cases - ...' child task found; planning from the task itself.")
    state = RunState.new(settings.runs_dir, task_id)
    print(f"Pack: {pack.slug}. Run folder: {state.dir}")

    from .reliability import free_gb
    if free_gb(settings.runs_dir.parent) < settings.min_free_gb:
        print(f"Warning: less than {settings.min_free_gb:g} GB free disk.")
    from .modes import cases_for_bug, parse_cases
    only = parse_cases(case)
    task_text = bundle.all_text()
    if retest_bug:
        linked, src = cases_for_bug(settings.runs_dir, task_id, retest_bug)
        if linked:
            print(f"Retesting bug {retest_bug}: cases {', '.join(linked)} (from {src.name}).")
            only = list(dict.fromkeys(only + linked))
        else:
            bug = next((b for b in pack.known_bugs if b["n"] == retest_bug), None)
            hint = bug["text"] if bug else f"Redmine issue #{retest_bug}"
            print(f"No earlier run links cases to bug {retest_bug}; the planner will pick cases related to it.")
            task_text += f"\n\nRETEST MODE: plan only the cases that check this fixed bug: {hint}"
    lessons_mem = open_memory(settings, pack)
    plan = asyncio.run(plan_task(settings, pack, task_text, task_id, env, only_case=",".join(only) or None,
                                 lessons=lessons_mem.as_prompt(lessons_mem.preload()), trace_path=state.dir / "trace.jsonl"))
    lessons_mem.store.close()
    if only:                                   # code enforces the case selection, whatever the planner returned
        from .modes import filter_plan
        plan = filter_plan(plan, [c for c in only if c in {x.id for x in plan.cases}])
    wiki_proj = pack.config["redmine"].get("wiki_project") or bundle.issue["project"]["id"]
    wiki = {t: (rm.wiki_page(wiki_proj, t) or "") for t in pack.config["redmine"].get("wiki_pages", [])}
    memory = open_memory(settings, pack)
    flagged = memory.recheck_linked(_closed_checker(rm))
    if flagged:
        print(f"{len(flagged)} saved lesson(s)/bug(s) are linked to closed issues and will be re-verified.")
    policy = from_pack(pack, env, approver=gate.console_approver())
    ctx = G.GapContext(pack=pack, env=env, memory=memory, wiki_texts=wiki, task_text=bundle.all_text(),
                       url_checker=G.http_url_checker(), allow_hosts=policy.allow_hosts)
    G.resolve(plan.requirements, ctx)
    gate.show_plan(plan, check_plan(plan))
    gate.ask_batch(plan.requirements, ctx)
    still = G.open_questions(plan.requirements)
    state.set_plan(plan)
    if still:
        print("\nStill missing: " + ", ".join(r.key for r in still) + ". Stopping before the run.")
        state.checkpoint("stopped at plan gate: missing requirements", status="blocked")
        return 2
    if dry_run:
        state.checkpoint("dry run: stopped after plan gate", status="dry_run")
        print(f"\nDry run done. Plan saved in {state.path}")
        return 0
    ok, pre = gate.plan_gate(plan)
    if not ok:
        state.checkpoint("user stopped at plan gate", status="stopped")
        return 1
    policy.preapprove(pre)
    state.data["approved"], state.data["preapproved"] = True, pre
    state.data["model"] = model_fingerprint(settings)
    state.data["mode"] = {"retest_bug": retest_bug, "cases": only, "email": email, "email_progress": email_progress}
    return _execute(settings, pack, plan, state, policy, memory)


def _execute(settings, pack, plan, state, policy, memory) -> int:
    from .agents.judge import make_judge
    from .agents.tester import make_tester
    from .coordinator import RunPaused, run_plan
    from .end_of_run import export_memory, finish_memory
    from .ledger import Ledger
    from .reliability import RunLock, RunLocked, apply_retention
    from .report.builder import build_report

    from .progress import Progress
    mode = state.data.get("mode") or {}
    notifier = None
    if mode.get("email") or mode.get("email_progress"):
        from .notify import make_notifier
        notifier = make_notifier()
    progress = Progress(state, len(plan.cases), notifier=notifier if mode.get("email_progress") else None)
    try:
        with RunLock(settings.runs_dir / ".run.lock"):
            tester = make_tester(settings, pack, plan.env, policy, state.dir, memory=memory)
            testers = {app: tester for app in pack.apps}
            progress.started(f"#{plan.task_id} on {plan.env}")
            try:
                results = run_plan(plan, state, testers, make_judge(settings, pack, memory), on_case_done=progress.case_done)
            except RunPaused as e:
                progress.paused(str(e))
                print(f"Resume with: python -m agent_tester resume {state.dir}")
                return 3
            except KeyboardInterrupt:
                state.checkpoint("stopped by user (Ctrl-C)", status="paused")
                print(f"\nStopped. Resume with: python -m agent_tester resume {state.dir}")
                return 130
    except RunLocked as e:
        print(f"Can't start: {e}")
        return 5
    led = Ledger(state)
    if led.unrestored():
        print("\nWARNING: these settings were not restored and verified: " +
              ", ".join(f"{u['app']}:{u['setting']} (was {u['original']})" for u in led.unrestored()))
    if led.leftovers():
        print(f"{len(led.leftovers())} test record(s) left behind; see `leftovers {state.dir}`.")
    finish_memory(memory, pack, state)
    export_memory(memory, ROOT / "memory" / "memory-export.json")
    out = build_report(state, pack)
    report = state.data.get("report_pdf") or out
    counts: dict = {}
    for r in results.values():
        if r:
            counts[r.verdict] = counts.get(r.verdict, 0) + 1
    progress.finished(counts, report)
    if notifier and mode.get("email"):
        try:
            notifier(f"QA report #{plan.task_id}", ", ".join(f"{v} {k}" for k, v in counts.items()), Path(report))
        except Exception as e:  # noqa: BLE001
            print(f"Report email not sent: {e}")
    trimmed = apply_retention(settings.runs_dir, keep=settings.run_retention)
    if trimmed:
        print(f"Trimmed {len(trimmed)} old run folder(s) to state.json and report only.")
    return 0 if all(r and r.verdict != "AGENT INCOMPLETE" for r in results.values()) else 4


def cmd_resume(settings: Settings, run_dir: Path) -> int:
    from . import gate
    from .preflight.pack_loader import find_pack
    from .run_state import RunState
    from .tools.policy import from_pack
    state = RunState(run_dir)
    plan = state.plan
    if not plan or not state.data.get("approved"):
        print("This run has no approved plan; start it again with `test`.")
        return 1
    import requests  # noqa: F401
    from .preflight.redmine import Redmine
    proj = Redmine(settings.redmine_url, settings.redmine_api_key).issue(plan.task_id)["project"]["id"]
    pack = find_pack(settings.packs_dir, [proj])
    policy = from_pack(pack, plan.env, approver=gate.console_approver())
    policy.preapprove(state.data.get("preapproved", []))
    for r in plan.requirements:
        if r.kind == "url" and r.value:
            from urllib.parse import urlparse
            policy.allow_host(urlparse(r.value).hostname or "")
    return _execute(settings, pack, plan, state, policy, open_memory(settings, pack))


def _pack_for_run(settings, plan):
    from .preflight.pack_loader import find_pack
    from .preflight.redmine import Redmine
    proj = Redmine(settings.redmine_url, settings.redmine_api_key).issue(plan.task_id)["project"]["id"]
    return find_pack(settings.packs_dir, [proj])


def cmd_correct(settings, run_dir: Path, case_id: str, verdict: str, why: str, general: bool, pack_slug: str | None) -> int:
    from .models import CaseResult
    from .preflight.pack_loader import load_pack
    from .report.builder import build_report
    from .run_state import RunState
    state = RunState(run_dir)
    old = state.result(case_id)
    if old is None:
        print(f"No result for {case_id} in {run_dir}")
        return 1
    pack = load_pack(settings.packs_dir / pack_slug) if pack_slug else _pack_for_run(settings, state.plan)
    mem = open_memory(settings, pack)
    area = next((c.area for c in state.plan.cases if c.id == case_id), None)
    mid = mem.correction(case_id, old.verdict, verdict, why, area=area, run_id=state.dir.name,
                         linked_issue=state.plan.task_id, general=general)
    fixed = CaseResult.model_validate({**old.model_dump(), "verdict": verdict,
                                       "note": f"{old.note or ''} Corrected by user: {why}".strip()})
    state.set_result(fixed)
    build_report(state, pack)
    print(f"Saved lesson #{mid} and updated {case_id}: {old.verdict} -> {verdict}.")
    return 0


def cmd_memory(settings, action: str, ids: list[int], pack_slug: str) -> int:
    from .preflight.pack_loader import load_pack
    mem = open_memory(settings, load_pack(settings.packs_dir / pack_slug))
    if action == "list":
        rows = mem.store.query(mem.scopes, statuses=("active", "needs_recheck"))
        for r in rows:
            print(f"[{r['id']}] {r['scope']:<20} {r['kind']:<15} {r['status']:<13} {r['text'][:100]}")
    elif action == "pending":
        for r in mem.pending():
            print(f"[{r['id']}] {r['kind']:<8} {r['text']}")
    elif action == "approve":
        mem.approve(ids)
    elif action == "retire":
        mem.reject(ids)
    return 0


def cmd_leftovers(run_dir: Path) -> int:
    from .ledger import Ledger
    from .run_state import RunState
    led = Ledger(RunState(run_dir))
    for e in led.leftovers():
        print(f"{e['app']:<8} {e['record']:<24} {e['action']:<8} {e['final_state']:<16} tag={e['tag']} ({e['case_id']})")
    for u in led.unrestored():
        print(f"SETTING  {u['app']}:{u['setting']} original={u['original']} now={u['restored']} verified={u['verified']}")
    if not led.leftovers() and not led.unrestored():
        print("Nothing left behind.")
    return 0


def cmd_rerun(settings: Settings, task_id: int, cases: str) -> int:
    """Rerun selected cases from the latest approved plan. Goes through the plan gate again."""
    from . import gate
    from .modes import filter_plan, latest_plan, parse_cases
    from .plan_checks import check_plan
    from .run_state import RunState
    from .tools.policy import from_pack
    old, src = latest_plan(settings.runs_dir, task_id)
    if old is None:
        print(f"No approved plan for #{task_id} yet; use `test {task_id}` first.")
        return 1
    try:
        plan = filter_plan(old, parse_cases(cases))
    except ValueError as e:
        print(e)
        return 1
    pack = _pack_for_run(settings, plan)
    state = RunState.new(settings.runs_dir, task_id)
    print(f"Rerunning {', '.join(c.id for c in plan.cases)} from {src.name}. Run folder: {state.dir}")
    gate.show_plan(plan, check_plan(plan))
    ok, pre = gate.plan_gate(plan)
    if not ok:
        return 1
    policy = from_pack(pack, plan.env, approver=gate.console_approver())
    policy.preapprove(pre)
    for r in plan.requirements:
        if r.kind == "url" and r.value:
            from urllib.parse import urlparse
            policy.allow_host(urlparse(r.value).hostname or "")
    state.set_plan(plan)
    state.data.update(approved=True, preapproved=pre, model=model_fingerprint(settings),
                      mode={"rerun_of": src.name, "cases": [c.id for c in plan.cases]})
    return _execute(settings, pack, plan, state, policy, open_memory(settings, pack))


def cmd_report(settings: Settings, run_dir: Path, pack_slug: str | None) -> int:
    from .preflight.pack_loader import load_pack
    from .report.builder import build_report
    from .run_state import RunState
    state = RunState(run_dir)
    pack = load_pack(settings.packs_dir / pack_slug) if pack_slug else _pack_for_run(settings, state.plan)
    out = build_report(state, pack)
    print(f"Report: {state.data.get('report_pdf') or out}")
    return 0


def cmd_eval(settings: Settings, project: str, live: bool, rebuild: bool, cases: str | None) -> int:
    """Replay self-test: judge past cases from their recorded results and score against the known answers."""
    from .evals.extract import build_known_answers
    from .evals.replay import load_known_answers, oracle_judge, replay
    from .modes import parse_cases
    from .preflight.pack_loader import load_pack
    pack = load_pack(settings.packs_dir / project)
    data = build_known_answers(pack) if rebuild or not (pack.root / "evals" / "known_answers.json").exists() \
        else load_known_answers(pack)
    print(f"Known answers: {len(data['cases'])} case(s) from {', '.join(data['sources'])}.")
    if live:
        from .agents.judge import make_judge
        mem = open_memory(settings, pack)
        judge = make_judge(settings, pack, mem)
    else:
        print("Offline check with the known answers as judge (plumbing only). Add --live to score the real judge.")
        judge = oracle_judge(data["cases"])
    out_dir = settings.runs_dir / f"eval-{pack.slug}-{datetime.now():%Y%m%d-%H%M%S}"
    score = replay(data["cases"], judge, out_dir, parse_cases(cases) or None)
    print(score.summary())
    for row in score.rows:
        if not row["agree"]:
            print(f"  {row['key']}: expected {row['expected']}, got {row['got']}")
    print(f"Details: {out_dir / 'replay_score.json'}")
    return 0 if score.passed else 1


def cmd_bugs(settings: Settings, run_dir: Path, post: bool, pack_slug: str | None) -> int:
    from . import gate
    from .preflight.pack_loader import load_pack
    from .preflight.redmine import Redmine
    from .report import bug_drafts as B
    from .run_state import RunState
    from .tools.policy import from_pack
    data = B.load(run_dir)
    if not data["drafts"] and not data["still_present"]:
        print("No bug drafts in this run. (Run `report` first if the run predates phase 4.)")
        return 0
    for d in data["drafts"]:
        print(f"{d['id']} [{d['severity']}] {d['title']}  ({d['status']}" + (f", #{d['posted_issue']}" if d.get("posted_issue") else "") + ")")
    for b in data["still_present"]:
        print(f"still present: bug {b['n']} [{b['severity']}] ({', '.join(b['cases'])})")
    if not post:
        print(f"\nFull drafts: {run_dir / 'bug_drafts.md'}")
        return 0
    state = RunState(run_dir)
    pack = load_pack(settings.packs_dir / pack_slug) if pack_slug else _pack_for_run(settings, state.plan)
    rm = Redmine(settings.redmine_url, settings.redmine_api_key)
    project = pack.config["redmine"]["project_ids"][0]
    policy = from_pack(pack, "staging", approver=gate.console_approver())
    posted = B.review_and_post(run_dir, lambda d, body: rm.create_issue(project, d["title"], body, relates_to=d["task_id"]), policy)
    print(f"Posted {len(posted)} bug(s): " + ", ".join(f"#{i}" for i in posted) if posted else "Nothing posted.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agent_tester")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    t = sub.add_parser("test")
    t.add_argument("task_id", type=int)
    t.add_argument("--cases", "--case", dest="case", help="comma-separated case ids, e.g. TC1,TC3")
    t.add_argument("--retest-bug", type=int, help="known bug number from facts.md, or a Redmine issue posted from a draft")
    t.add_argument("--env", default="staging", choices=["staging", "prod"])
    t.add_argument("--dry-run", action="store_true", help="plan and questions only")
    t.add_argument("--email", action="store_true", help="email the report to REPORT_EMAIL_TO")
    t.add_argument("--email-progress", action="store_true", help="also email start, halfway, pause and finish")
    rr = sub.add_parser("rerun")
    rr.add_argument("task_id", type=int)
    rr.add_argument("--cases", required=True)
    rp = sub.add_parser("report")
    rp.add_argument("run_dir", type=Path)
    rp.add_argument("--project")
    bg = sub.add_parser("bugs")
    bg.add_argument("run_dir", type=Path)
    bg.add_argument("--post", action="store_true")
    bg.add_argument("--project")
    r = sub.add_parser("resume")
    r.add_argument("run_dir", type=Path)
    c = sub.add_parser("correct")
    c.add_argument("run_dir", type=Path)
    c.add_argument("case_id")
    c.add_argument("verdict", choices=["PASS", "FAIL", "PARTIAL", "BLOCKED", "NOT VERIFIED", "SKIPPED", "AGENT INCOMPLETE"])
    c.add_argument("why")
    c.add_argument("--general", action="store_true")
    c.add_argument("--project")
    m = sub.add_parser("memory")
    m.add_argument("action", choices=["list", "pending", "approve", "retire"])
    m.add_argument("ids", nargs="*", type=int)
    m.add_argument("--project", default="savostore")
    ev = sub.add_parser("eval", help="replay self-test against past reports")
    ev.add_argument("--project", default="savostore")
    ev.add_argument("--live", action="store_true", help="use the real judge (needs the LLM)")
    ev.add_argument("--rebuild", action="store_true", help="re-extract known answers from evals/source/*.pdf")
    ev.add_argument("--cases", help="only these case ids")
    lo = sub.add_parser("leftovers")
    lo.add_argument("run_dir", type=Path)
    a = ap.parse_args(argv)
    settings = Settings.load()
    if a.cmd == "check":
        return cmd_check(settings)
    if a.cmd == "test":
        if a.env == "prod":
            print("Prod runs are read-only; every write needs your approval.")
        return cmd_test(settings, a.task_id, a.case, a.env, a.dry_run, a.retest_bug, a.email, a.email_progress)
    if a.cmd == "rerun":
        return cmd_rerun(settings, a.task_id, a.cases)
    if a.cmd == "report":
        return cmd_report(settings, a.run_dir, a.project)
    if a.cmd == "bugs":
        return cmd_bugs(settings, a.run_dir, a.post, a.project)
    if a.cmd == "correct":
        return cmd_correct(settings, a.run_dir, a.case_id, a.verdict, a.why, a.general, a.project)
    if a.cmd == "memory":
        return cmd_memory(settings, a.action, a.ids, a.project)
    if a.cmd == "leftovers":
        return cmd_leftovers(a.run_dir)
    if a.cmd == "eval":
        return cmd_eval(settings, a.project, a.live, a.rebuild, a.cases)
    return cmd_resume(settings, a.run_dir)


if __name__ == "__main__":
    sys.exit(main())
