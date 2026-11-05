"""CLI: python -m agent_tester <command>

  check                               connectivity to the LLM, Redmine and the pack's environments
  test <task_id> [--cases TC1,TC3] [--retest-bug N] [--env staging] [--dry-run] [--email] [--email-progress]
  rerun <task_id> --cases TC2,TC4     rerun chosen cases from the latest approved plan (no new planning)
  resume <run_dir>
  web [--port 8765]                  web UI: start runs, answer questions, watch steps, reports
  report <run_dir>                    rebuild report.html / report.pdf and bug drafts
  bugs <run_dir> [--post]             list bug drafts; --post asks before posting each one to Redmine
  correct <run_dir> <case_id> <VERDICT> "<why>" [--general]   save a lesson now and fix the result
  memory list|pending|approve <ids>|retire <ids> [--project <pack>]
  leftovers <run_dir>                 test data and settings the run left behind
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
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


def make_manual_login(pack, plan, state, input_fn=input, opener=None):
    """Returns login(app, reason) -> bool: a visible browser for a person to log in once (CAPTCHA)."""
    from .agents.tester import manual_login, run_urls

    def login(app: str, reason: str) -> bool:
        url = run_urls(pack, plan.env, state).get(app)
        if not url:
            return False
        user = next((r.value for r in plan.requirements if r.key.startswith(f"account.{app}.") and r.value), None)
        label = pack.apps.get(app, {}).get("label", app)
        print(f"\nThe {label} login needs a person: {reason}")
        return (opener or manual_login)(state.dir, app, url, user, input_fn=input_fn)
    return login


def login_first(pack, plan, state, login, input_fn=None) -> list[str]:
    """Apps listed in the pack's manual_login_apps (known CAPTCHA) are logged in by hand once before the cases."""
    from .auto import prompter
    input_fn = input_fn or prompter("y")
    used = {s.app for c in plan.cases for s in c.steps}
    done = state.data.setdefault("manual_logins", {})
    did = []
    if os.environ.get("CAPTCHA_AUTO", "1") != "0":
        return did          # try the checkbox automatically first; a person is asked mid-run only if a puzzle appears
    for app in pack.config.get("manual_login_apps", []) or []:
        if app not in used or done.get(app):
            continue
        label = pack.apps.get(app, {}).get("label", app)
        a = input_fn(f"\nThe {label} login has a CAPTCHA. Log in by hand once now so every case can use it? [Y/n] ")
        if a.strip().lower() in ("", "y", "yes") and login(app, "CAPTCHA at login (known for this app)"):
            done[app] = True
            did.append(app)
    state.save()
    return did


def run_sites(policy, plan) -> list[str]:
    """The browser may use only the sites of this run's URLs (picked or confirmed at the plan gate);
    anything else the app sends it to needs the user's OK during the run."""
    from urllib.parse import urlparse
    hosts = []
    for r in plan.requirements:
        if r.kind == "url" and r.value and r.status == "resolved":
            h = (urlparse(r.value).hostname or "").lower()
            if h and h not in hosts:
                hosts.append(h)
    policy.allow_hosts[:] = hosts
    return hosts


def parse_cases_early(case):
    from .modes import parse_cases
    return parse_cases(case)


def _feature_checks(feature, plan, pack) -> list[str]:
    """Feature mode: check each subtask's generated cases against that subtask's own text."""
    from . import casegen
    issues = []
    for b in feature or []:
        mine = [c for c in plan.cases if c.source_task == b.issue["id"] and c.generated]
        if mine:
            issues += casegen.check_generated(plan.model_copy(update={"cases": mine}), casegen.task_sources(b, pack.facts))
    return issues


def _state_for_run(runs_dir, task_id, draft, draft_src, draft_state, subtasks):
    """The run folder for a test run: a plan-only draft is run in its own folder (no second entry in the run
    list); a draft made with the other scope (feature vs only this ticket) is not used."""
    from .run_state import RunState
    if draft is not None and ((draft_state.get("mode") or {}).get("subtasks", True) is not False) != bool(subtasks):
        print("The plan-only draft was made with a different scope (feature vs only this ticket); planning again.")
        draft = None
    if draft is not None:
        return RunState(draft_src), draft
    return RunState.new(runs_dir, task_id), None


CASES_FILE_MAX = 200_000


def read_cases_file(path) -> str | None:
    """A Markdown or text list of test cases to use instead of the ticket's Redmine Test Cases task."""
    p = Path(path).expanduser()
    if p.suffix.lower() not in (".md", ".txt", ".markdown") or not p.is_file() or p.stat().st_size > CASES_FILE_MAX:
        return None
    text = p.read_text(errors="replace").strip()
    return text or None


_CASE_HEAD = __import__("re").compile(r"^#{2,4}\s+([A-Z]{1,3}\d+(?:-[A-Za-z0-9]+)?)(?=[.:)\s(]|$)", __import__("re").M)


def case_ids_in(text: str) -> list[str]:
    """Case ids from the file's headings (### A1. ..., ## TC3: ...), in file order."""
    return list(dict.fromkeys(m.group(1) for m in _CASE_HEAD.finditer(text or "")))


def with_cases_file(bundle, text: str, name: str, prefix: str = "Test Cases - "):
    """The file stands in for the Test Cases child, so the planner keeps its ids and nothing is generated."""
    from dataclasses import replace
    tc = {"id": None, "subject": f"{prefix}{name} (cases file)", "description": text,
          "project": bundle.issue.get("project"), "journals": []}
    return replace(bundle, test_case_task=tc)


def cmd_test(settings: Settings, task_id: int, case: str | None, env: str, dry_run: bool,
             retest_bug: int | None = None, email: bool = False, email_progress: bool = False,
             fresh_plan: bool = False, draft_run: str | None = None, subtasks: bool = True,
             cases_file: Path | None = None) -> int:
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
    pack = _pack_or_onboard(settings, first["project"]["id"])
    if env is None and pack is not None:
        env = gate.choose_env(pack)
        if env is None:
            print("Stopped: production not confirmed.")
            return 2
    tc_prefix = pack.config["redmine"].get("test_case_subject_prefix", "Test Cases - ")
    bundle = rm.fetch_task(task_id, tc_prefix)
    cases_text, file_ids = None, None
    if cases_file:
        cases_text = read_cases_file(cases_file)
        if cases_text is None:
            print(f"Can't use the cases file {cases_file}: it must be an existing .md or .txt file under 200 KB.")
            return 2
        bundle = with_cases_file(bundle, cases_text, Path(cases_file).name, tc_prefix)
        file_ids = case_ids_in(cases_text)
        only_early = parse_cases_early(case)
        if only_early:
            file_ids = [i for i in file_ids if i in only_early] or None
        print(f"Using the test cases in {Path(cases_file).name} instead of Redmine's (only this ticket)"
              + (f": {len(file_ids)} cases." if file_ids else "."))
        subtasks = False
        if not draft_run:
            fresh_plan = True                     # a draft planned from Redmine's cases is not this file's plan
    from . import casegen
    from . import feature as F
    feature = rm.feature_tasks(task_id, tc_prefix) if subtasks and not retest_bug else []
    if not F.is_feature(feature, task_id):
        feature = []
    generate = not feature and casegen.needs_generation(bundle, tc_prefix)
    if feature:
        print(f"Feature mode: #{task_id} has subtasks/related tickets; testing these together:")
        for b in feature:
            print(f"  #{b.issue['id']} ({b.via}) {b.issue.get('subject', '')[:80]}"
                  + ("  [no Test Cases: I'll write them]" if casegen.needs_generation(b, tc_prefix) else ""))
    if generate:
        print("No 'Test Cases - ...' task found; I'll write the cases from the task description for you to review.")
    from .modes import drafted_plan, draft_from
    if draft_run and not (dry_run or retest_bug or fresh_plan):
        draft, draft_src, draft_state = draft_from(settings.runs_dir, draft_run, task_id)
        if draft is None:
            print(f"{draft_run} is not a plan-only run of #{task_id}; planning again.")
    else:
        draft, draft_src, draft_state = (None, None, {}) if (dry_run or retest_bug or fresh_plan) else \
            drafted_plan(settings.runs_dir, task_id)
    if draft is not None and getattr(draft, "env", env) != env:
        draft = None                                  # drafted for another environment: plan again
    state, draft = _state_for_run(settings.runs_dir, task_id, draft, draft_src, draft_state, subtasks)
    state.data["env"] = env                       # known even if the run is stopped before planning
    state.data["mode"] = {"cases": parse_cases_early(case), "subtasks": bool(subtasks)}
    if cases_text is not None:
        (state.dir / "cases_file.md").write_text(cases_text)     # kept with the run, so a fresh plan can reuse it
        state.data["mode"]["cases_file"] = Path(cases_file).name
    state.save()
    print(f"Pack: {pack.slug}. Run folder: {state.dir}")

    from .reliability import free_gb
    if free_gb(settings.runs_dir.parent) < settings.min_free_gb:
        print(f"Warning: less than {settings.min_free_gb:g} GB free disk.")
    from .modes import cases_for_bug, parse_cases
    only = parse_cases(case)
    task_text = "\n\n".join(f"#{b.issue['id']}:\n{b.all_text()}" for b in feature) if feature else bundle.all_text()
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
    from .agents.llm import LLMOutputError
    try:
        if draft is not None:
            print(f"Continuing from the plan drafted in {draft_src.name} (plan only); no new planning.")
            plan = draft
        else:
          if feature:
            plan = asyncio.run(F.plan_feature(settings, pack, feature, task_id, env, tc_prefix,
                                              lessons=lessons_mem.as_prompt(lessons_mem.preload()),
                                              trace_path=state.dir / "trace.jsonl",
                                              preapprovable=list(pack.config.get("preapprovable_actions", []))))
            for ln in F.summary(feature, plan):
                print("  " + ln)
          else:
            plan = asyncio.run(plan_task(settings, pack, task_text, task_id, env, only_case=",".join(only) or None,
                                     lessons=lessons_mem.as_prompt(lessons_mem.preload()), trace_path=state.dir / "trace.jsonl", generate=generate,
                                     preapprovable=list(pack.config.get("preapprovable_actions", [])),
                                     batch_ids=file_ids))
    except LLMOutputError as e:
        lessons_mem.store.close()
        print(f"\nPlanning stopped: {e}")
        state.checkpoint(f"planner output unusable: {e}", status="blocked")
        return 4
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
    ctx = G.GapContext(pack=pack, env=env, memory=memory, wiki_texts=wiki, task_text=task_text,
                       url_checker=G.http_url_checker(), allow_hosts=policy.allow_hosts)
    G.ensure_account_requirements(plan, pack, env)
    G.resolve(plan.requirements, ctx)
    issues = check_plan(plan)
    if generate:
        issues += casegen.check_generated(plan, casegen.task_sources(bundle, pack.facts))
    issues += _feature_checks(feature, plan, pack)
    gate.show_plan(plan, issues)
    if generate:
        for n in casegen.coverage_notes(plan, bundle.all_text()):
            print(f"  note: {n}")
    gate.ask_batch(plan.requirements, ctx)
    user_answers = {r.key: r.value for r in plan.requirements
                    if r.kind not in ("url", "account") and r.source == "user" and r.value}
    if user_answers and draft is None:
        print("\nUpdating the plan with your answers...")
        try:
            if feature:
                new = asyncio.run(F.plan_feature(settings, pack, feature, task_id, env, tc_prefix,
                                                 lessons=memory.as_prompt(memory.preload()),
                                                 trace_path=state.dir / "trace.jsonl", answers=user_answers,
                                                 preapprovable=list(pack.config.get("preapprovable_actions", []))))
            else:
                new = asyncio.run(plan_task(settings, pack, task_text, task_id, env, only_case=",".join(only) or None,
                                        lessons=memory.as_prompt(memory.preload()), trace_path=state.dir / "trace.jsonl",
                                        generate=generate, answers=user_answers,
                                        preapprovable=list(pack.config.get("preapprovable_actions", [])),
                                        batch_ids=file_ids))
        except LLMOutputError as e:
            print(f"Couldn't update the plan ({e}); keeping the first plan.")
            new = None
        if new is not None:
            if only:
                from .modes import filter_plan
                new = filter_plan(new, [c for c in only if c in {x.id for x in new.cases}])
            old = {r.key: r for r in plan.requirements}
            for r in new.requirements:
                if r.key in old and old[r.key].status == "resolved":
                    new.requirements[new.requirements.index(r)] = old[r.key]
            for k, r in old.items():                 # keep answered items the new plan forgot to list
                if r.status == "resolved" and k not in {x.key for x in new.requirements}:
                    new.requirements.append(r)
            plan = new
            G.ensure_account_requirements(plan, pack, env)
            G.resolve(plan.requirements, ctx)
            issues = check_plan(plan)
            if generate:
                issues += casegen.check_generated(plan, casegen.task_sources(bundle, pack.facts))
            issues += _feature_checks(feature, plan, pack)
            gate.show_plan(plan, issues)
            gate.ask_batch(plan.requirements, ctx)
    if generate and draft is not None:
        state.data["case_answers"] = draft_state.get("case_answers")
    elif feature and any(c.generated for c in plan.cases):
        state.data["case_answers"] = casegen.ask_case_questions(plan)
    elif generate:
        state.data["case_answers"] = casegen.ask_case_questions(plan)
        plan = casegen.edit_cases(plan)
        (state.dir / "generated_cases.textile").write_text(casegen.to_textile(plan, bundle.issue.get("subject", "")))
        print(f"Generated cases saved in {state.dir / 'generated_cases.textile'}. "
              f"After the run you can post them to Redmine with: cases {state.dir} --post")
    from .auto import is_auto
    if is_auto():                                     # test data is the agent's job: find or create it in the run
        for r in G.open_questions(plan.requirements):
            if r.kind in ("data", "other"):
                r.status, r.source = "resolved", "agent"
                r.value = r.value or "found or created by the agent during the run (data prep)"
                print(f"  {r.key}: I'll find or create this myself during the run.")
    still = G.open_questions(plan.requirements)
    state.set_plan(plan)
    if still:
        print("\nStill missing: " + ", ".join(r.key for r in still) + ". Stopping before the run.")
        state.checkpoint("stopped at plan gate: missing requirements", status="dry_run" if draft is not None else "blocked")
        return 2
    if dry_run:
        state.checkpoint("dry run: stopped after plan gate", status="dry_run")
        print(f"\nPlan only: done. Plan saved in {state.path}. Start a normal run of this task to continue from it.")
        return 0
    sites = run_sites(policy, plan)
    print("\nThe browser will stay on: " + (", ".join(sites) or "no sites yet")
          + ". If the app sends it anywhere else (like a payment page), I'll ask you first.")
    ok, pre = gate.plan_gate(plan, policy=policy, issues=check_plan(plan))
    if not ok:
        state.checkpoint("user stopped at plan gate", status="dry_run" if draft is not None else "stopped")
        return 1
    policy.preapprove(pre)
    state.data["approved"], state.data["preapproved"] = True, pre
    state.data["model"] = model_fingerprint(settings)
    state.data["mode"] = {"retest_bug": retest_bug, "cases": only, "email": email, "email_progress": email_progress,
                          "subtasks": bool(subtasks)}
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
            state.data["pack_root"] = str(pack.root)
            state.save()
            login = make_manual_login(pack, plan, state)
            login_first(pack, plan, state, login)
            progress.started(f"#{plan.task_id} on {plan.env}")
            try:
                results = run_plan(plan, state, testers, make_judge(settings, pack, memory), on_case_done=progress.case_done,
                                   manual_login=login,
                                   manual_login_apps=list(pack.config.get("manual_login_apps") or []),
                                   memory=memory, data_prep=list(pack.config.get("data_prep") or []))
                left = Ledger(state).unrestored()
                if left:
                    print("\nThese settings were changed and not put back: " +
                          ", ".join(f"{u['app']}:{u['setting']} (was {u['original']})" for u in left))
                    from .auto import prompter
                    if prompter("y")("Put them back now? [Y/n] ").strip().lower() in ("", "y", "yes"):
                        from .coordinator import restore_pass
                        restore_pass(plan, state, testers, left)
            except RunPaused as e:
                progress.paused(str(e))
                _partial_report(state, pack)
                print(f"Resume with: python -m agent_tester resume {state.dir}")
                return 3
            except KeyboardInterrupt:
                state.checkpoint("stopped by user (Ctrl-C)", status="paused")
                _partial_report(state, pack)
                print(f"\nStopped. Resume with: python -m agent_tester resume {state.dir}")
                return 130
            except Exception as e:  # noqa: BLE001  (anything outside a single case: keep the run resumable)
                if os.environ.get("AGENT_DEBUG"):
                    raise
                _save_trace(state.dir / "agent_error.log")
                state.checkpoint(f"stopped by error: {friendly_error(e)}", status="paused")
                _partial_report(state, pack)
                print(f"\nThe run stopped: {friendly_error(e)}\nFinished cases are saved. Details: "
                      f"{state.dir / 'agent_error.log'}\nResume with: python -m agent_tester resume {state.dir}")
                return 1
    except RunLocked as e:
        print(f"Can't start: {e}")
        return 5
    led = Ledger(state)
    if led.unrestored():
        print("\nWARNING: these settings were not restored and verified: " +
              ", ".join(f"{u['app']}:{u['setting']} (was {u['original']})" for u in led.unrestored()))
    if led.leftovers():
        print(f"{len(led.leftovers())} test record(s) left behind; see `leftovers {state.dir}`.")
    _safe("saving what this run learned", finish_memory, memory, pack, state)
    _safe("exporting memory", export_memory, memory, ROOT / "memory" / "memory-export.json")
    out = _safe("building the report", build_report, state, pack) or state.dir / "state.json"
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
    trimmed = _safe("trimming old runs", apply_retention, settings.runs_dir, keep=settings.run_retention)
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
    run_sites(policy, plan)
    return _execute(settings, pack, plan, state, policy, open_memory(settings, pack))


def _pack_or_onboard(settings, project_id):
    """The run's pack. A project with no pack gets one built at the start of the run: drafted from its Redmine
    project (addresses, apps, areas), one batch of questions, saved after a yes. The run then goes on with it and
    fills learned.md (routes, recipes, how the app behaves) as it tests."""
    from .preflight.pack_loader import NoPackFound, find_pack
    try:
        return find_pack(settings.packs_dir, [project_id])
    except NoPackFound:
        if not sys.stdin.isatty() and not os.environ.get("QA_WEB"):
            raise
        print(f"No test pack covers Redmine project {project_id} yet; building one for it now.")
        if cmd_onboard(settings, str(project_id), None) != 0:
            raise
        return find_pack(settings.packs_dir, [project_id])


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


def cmd_rerun(settings: Settings, task_id: int, cases: str | None, src_run: str | None = None,
              failed: bool = False) -> int:
    """Rerun selected cases from the latest approved plan. Goes through the plan gate again."""
    from . import gate
    from .modes import filter_plan, latest_plan, parse_cases
    from .plan_checks import check_plan
    from .run_state import RunState
    from .tools.policy import from_pack
    if src_run:                                   # rerun the plan of one chosen run (web: "Rerun")
        import json as _json
        src = Path(src_run)
        if not src.is_absolute():
            src = settings.runs_dir / src_run
        try:
            st = _json.loads((src / "state.json").read_text())
        except (OSError, ValueError):
            print(f"Can't read the run {src_run}.")
            return 1
        if not st.get("plan"):
            print(f"Run {src.name} has no plan to rerun; start a new run instead.")
            return 1
        from .models import TestPlan
        old = TestPlan.model_validate(st["plan"])
        if failed:
            res = st.get("results") or {}
            cases = ",".join(c.id for c in old.cases if (res.get(c.id) or {}).get("verdict") != "PASS")
            if not cases:
                print(f"Every case in {src.name} passed; nothing to rerun.")
                return 0
    else:
        old, src = latest_plan(settings.runs_dir, task_id)
    if old is None:
        print(f"No approved plan for #{task_id} yet; use `test {task_id}` first.")
        return 1
    # Plans made before the CRM repair rule have no Quick Repair and Rebuild case; add it so reruns
    # don't test against a stale cache (#183498 TC1 false fail, 2026-10-28).
    from .casegen import add_repair_case
    n_before = len(old.cases)
    old = add_repair_case(old, _pack_for_run(settings, old))
    repair_added = len(old.cases) > n_before
    if repair_added:
        print("Added the Quick Repair and Rebuild case this older plan was missing; every case depends on it.")
    try:
        plan = filter_plan(old, parse_cases(cases))
    except ValueError as e:
        print(e)
        return 1
    if src_run and (failed or cases) and len(plan.cases) < len(old.cases) and not repair_added:
        return _rerun_in_place(settings, src, [c.id for c in plan.cases])
    pack = _pack_for_run(settings, plan)
    state = RunState.new(settings.runs_dir, task_id)
    print(f"Rerunning {', '.join(c.id for c in plan.cases)} from {src.name}. Run folder: {state.dir}")
    from .preflight import gap_resolver as G
    if G.ask_accounts_each_run(pack):             # users are chosen every run, reruns included
        accts = [r for r in plan.requirements if r.kind == "account"]
        for r in accts:
            r.status, r.source, r.value = "missing", None, None
        ctx = G.GapContext(pack=pack, env=plan.env, memory=open_memory(settings, pack), wiki_texts={}, task_text="")
        G.resolve(accts, ctx)
        gate.ask_batch(accts, ctx)
        if G.open_questions(accts):
            print("\nStill missing: " + ", ".join(r.key for r in G.open_questions(accts)) + ". Stopping before the run.")
            return 2
    gate.show_plan(plan, check_plan(plan))
    policy = from_pack(pack, plan.env, approver=gate.console_approver())
    sites = run_sites(policy, plan)
    print("\nThe browser will stay on: " + (", ".join(sites) or "no sites yet")
          + ". If the app sends it anywhere else (like a payment page), I'll ask you first.")
    ok, pre = gate.plan_gate(plan, policy=policy, issues=check_plan(plan))
    if not ok:
        return 1
    policy.preapprove(pre)
    run_sites(policy, plan)
    state.set_plan(plan)
    state.data.update(approved=True, preapproved=pre, model=model_fingerprint(settings),
                      mode={"rerun_of": src.name, "cases": [c.id for c in plan.cases]})
    return _execute(settings, pack, plan, state, policy, open_memory(settings, pack))


def _rerun_in_place(settings: Settings, run_dir: Path, case_ids: list[str]) -> int:
    """"Rerun not passed" / one case: continue the same run folder, redo only these cases, keep the passes."""
    from . import gate
    from .run_state import RunState
    from .tools.policy import from_pack
    state = RunState(run_dir)
    plan = state.plan
    if not plan or not state.data.get("approved"):
        print("This run has no approved plan; start it again with `test`.")
        return 1
    print(f"Rerunning {', '.join(case_ids)} in the same run; passed cases are kept. Run folder: {run_dir}")
    state.reopen_cases(case_ids)
    pack = _pack_for_run(settings, plan)
    policy = from_pack(pack, plan.env, approver=gate.console_approver())
    policy.preapprove(state.data.get("preapproved", []))
    run_sites(policy, plan)
    return _execute(settings, pack, plan, state, policy, open_memory(settings, pack))


def cmd_report(settings: Settings, run_dir: Path, pack_slug: str | None) -> int:
    from .preflight.pack_loader import load_pack
    from .report.builder import build_report
    from .run_state import RunState
    state = RunState(run_dir)
    if not state.plan:
        print("This run has no plan yet, so there is nothing to report.")
        return 1
    root = state.data.get("pack_root")
    if pack_slug:
        pack = load_pack(settings.packs_dir / pack_slug)
    elif root and (Path(root) / "pack.yaml").exists():
        pack = load_pack(Path(root))          # the run's own pack: no Redmine call needed
    else:
        pack = _pack_for_run(settings, state.plan)
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


def cmd_helpers(settings: Settings, action: str, project: str) -> int:
    from .helpers import candidates, load_helpers
    from .preflight.pack_loader import load_pack
    pack = load_pack(settings.packs_dir / project)
    if action == "list":
        for name, h in sorted(load_helpers(pack).items()):
            state = "ready" if h.enabled else f"off ({h.why_disabled})"
            kind = "changes data, asks first" if h.meta.get("changes_data") else "read-only"
            print(f"{name:24} {h.source:4} {h.meta.get('app', 'any'):7} {kind:24} {state}")
        return 0
    mem = open_memory(settings, pack)
    rows = candidates(mem.store, pack.slug)
    mem.store.close()
    if not rows:
        print("No recipe has worked 3+ times yet.")
    for r in rows:
        print(f"#{r['id']} worked {r['success_count']}x [{r['area'] or '-'}]: {r['text']}")
    if rows:
        print(f"Turn one into packs/{pack.slug}/helpers/<name>.py (format: agent_tester/helpers/__init__.py).")
    return 0


def cmd_onboard(settings: Settings, ref: str, slug: str | None, input_fn=input, out=print) -> int:
    """Draft a pack from Redmine, ask one batch of questions, show it, save only after a yes."""
    import yaml
    from urllib.parse import urlparse
    from .preflight import onboard as O
    from .preflight.packs import validate
    from .preflight.redmine import Redmine
    rm = Redmine(settings.redmine_url, settings.redmine_api_key)
    d = O.draft_pack(rm, ref, urlparse(settings.redmine_url).hostname or "")
    if slug:
        d.slug = d.config["slug"] = slug
    out(f"Drafting pack '{d.slug}' for {d.name} (Redmine project {d.project_id}) from {'; '.join(d.sources)}.")
    out("Questions (one batch; press Enter to skip any):")
    answers = {}
    for n, q in enumerate(d.questions, 1):
        answers[q.key] = input_fn(f"  {n}. {q.text}\n     > ").strip()
    O.apply_answers(d, answers)
    accounts, prods = {}, {}
    for nm, a in d.config["apps"].items():
        if "prod" not in a["envs"]:
            prods[f"prod:{nm}"] = input_fn(f"  {nm}: production address? (blank if none or unknown)\n     > ").strip()
        accounts[nm] = input_fn(f"  {nm}: test login username[,text visible after login for the role]? (no passwords here)\n     > ").strip()
    O.apply_answers(d, {**answers, **prods}, accounts)
    out("\n--- pack.yaml ---\n" + yaml.safe_dump(d.config, sort_keys=False) + "--- facts.md ---\n" + d.facts)
    if input_fn(f"Save this as packs/{d.slug}? [y/N] ").strip().lower() not in ("y", "yes"):
        out("Not saved.")
        return 1
    dest = O.save_draft(settings.packs_dir, d)
    from .preflight.pack_loader import load_pack
    probs = validate(load_pack(dest), settings.packs_dir)
    out(f"Saved {dest}." + ("" if not probs else "\nStill to fix:\n  - " + "\n  - ".join(probs)))
    envs = [acc["password_env"] for a in d.config["apps"].values() for acc in a["accounts"].values()]
    if envs:
        out(f"Passwords are asked through the masked prompt at the first plan gate ({', '.join(envs)}).")
    return 0


def cmd_pack(settings: Settings, action: str, slug: str, name: str | None, project_ids: list[int]) -> int:
    from .preflight.pack_loader import load_pack
    from .preflight.packs import new_pack, validate
    if action == "new":
        dest = new_pack(settings.packs_dir, slug, name, project_ids)
        print(f"Created {dest}. Fill pack.yaml and facts.md, then run `pack validate {slug}`.")
        return 0
    probs = validate(load_pack(settings.packs_dir / slug), settings.packs_dir)
    print("Valid." if not probs else "Problems:\n  - " + "\n  - ".join(probs))
    return 0 if not probs else 1


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


def cmd_cases(settings: Settings, run_dir: Path, post: bool, pack_slug: str | None) -> int:
    """Show the generated cases of a run, or post them to Redmine as a "Test Cases" child (asks first)."""
    from . import casegen, gate
    from .preflight.pack_loader import load_pack
    from .preflight.redmine import Redmine
    from .run_state import RunState
    from .tools.policy import PolicyViolation, from_pack
    state = RunState(run_dir)
    plan = state.plan
    if plan is None or plan.cases_source != "generated":
        print("This run used the task's own test cases; nothing to post.")
        return 0
    print(casegen.to_textile(plan))
    if not post:
        return 0
    if state.data.get("cases_posted"):
        print(f"Already posted as #{state.data['cases_posted']}.")
        return 0
    pack = load_pack(settings.packs_dir / pack_slug) if pack_slug else _pack_for_run(settings, plan)
    rm = Redmine(settings.redmine_url, settings.redmine_api_key)
    task = rm.issue(plan.task_id)
    policy = from_pack(pack, "staging", approver=gate.console_approver())
    try:
        new_id = casegen.post_cases(state, rm, policy, task["project"]["id"], task.get("subject", ""))
    except PolicyViolation:
        print("Not posted.")
        return 1
    print(f"Posted as #{new_id}.")
    return 0


UNREACHABLE_HINTS = ("Name or service not known", "Cannot connect to host", "Temporary failure in name resolution",
                     "Connection refused", "nodename nor servname")


def model_unreachable(e: BaseException | None) -> bool:
    """True when the error chain is a DNS or connection failure (not a model or code error)."""
    seen = 0
    while e is not None and seen < 12:
        name = type(e).__name__
        if name in ("gaierror", "ClientConnectorDNSError", "ConnectError", "APIConnectionError") or \
                any(h in str(e) for h in UNREACHABLE_HINTS):
            return True
        e, seen = (e.__cause__ or e.__context__), seen + 1
    return False


def model_host_ok(settings) -> str | None:
    """Fail fast before fetching or planning: returns an error message when the model server's name doesn't resolve."""
    import socket
    from urllib.parse import urlparse
    u = urlparse(settings.llm_base_url)
    try:
        socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
        return None
    except OSError as e:
        return unreachable_msg(settings, e)


def unreachable_msg(settings, e) -> str:
    from urllib.parse import urlparse
    host = urlparse(settings.llm_base_url).hostname
    return (f"Can't reach the model server {host} ({type(e).__name__}). It only resolves on the Rolustech network: "
            f"check the office network or VPN (`getent hosts {host}` should print an address), then run the "
            "command again.")


def friendly_error(e: BaseException) -> str:
    """One plain sentence for any failure, so the user never sees a raw traceback."""
    import requests
    from .preflight.redmine import RedmineError
    from .preflight.pack_loader import NoPackFound
    from .tools.policy import PolicyViolation
    from .notify import EmailNotAllowed
    from .agents.llm import LLMOutputError
    msg = str(e).strip() or type(e).__name__
    if isinstance(e, RedmineError):
        return f"Redmine: {msg}"
    if isinstance(e, NoPackFound):
        return f"No test pack matches this task: {msg}. Create one with `pack new` or `onboard`."
    if isinstance(e, PolicyViolation):
        return f"Blocked by a safety rule: {msg}"
    if isinstance(e, EmailNotAllowed):
        return f"Email not sent: {msg}"
    if isinstance(e, LLMOutputError):
        return f"The model gave an answer the agent couldn't use: {msg}. Try again; if it keeps happening, check the model server."
    if isinstance(e, requests.Timeout):
        return f"A server took too long to answer ({msg}). Check your connection or VPN and try again."
    if isinstance(e, requests.ConnectionError):
        return f"Couldn't connect to a server ({msg}). Check your connection or VPN and try again."
    if isinstance(e, FileNotFoundError):
        return f"A file or program is missing: {getattr(e, 'filename', None) or msg}."
    if isinstance(e, PermissionError):
        return f"No permission to access {getattr(e, 'filename', None) or msg}."
    if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
        return "The disk is full. Free some space and try again."
    if type(e).__name__ in ("YAMLError", "ScannerError", "ParserError", "JSONDecodeError"):
        return f"A config or data file is malformed: {msg}"
    if isinstance(e, KeyError):
        return f"A required setting or field is missing: {msg}."
    return f"Something went wrong: {type(e).__name__}: {msg}"


def _save_trace(path: Path) -> Path:
    import traceback
    try:
        with open(path, "w") as f:
            traceback.print_exc(file=f)
    except OSError:
        pass
    return path


def _partial_report(state, pack) -> None:
    """A stopped or paused run still gets a report of the cases that finished; the rest show as not run."""
    if not state.data.get("results"):
        return
    from .report.builder import build_report
    try:
        out = _safe("building the partial report", build_report, state, pack)
    except KeyboardInterrupt:                 # a second Ctrl-C skips the report, the run stays resumable
        print("Report skipped. Build it later with: python -m agent_tester report " + str(state.dir))
        return
    if out:
        print(f"Partial report (finished cases only): {state.data.get('report_pdf') or out}")


def _safe(what: str, fn, *a, **kw):
    """Run an after-run chore; a failure prints one line instead of losing the finished run."""
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        if os.environ.get("AGENT_DEBUG"):
            raise
        print(f"Warning: problem {what}: {friendly_error(e)}")
        return None


def default_project(settings) -> str | None:
    """QA_PROJECT, or the only pack in packs/ (the agent is generic; no project is built in)."""
    if os.environ.get("QA_PROJECT"):
        return os.environ["QA_PROJECT"]
    try:
        packs = [d.name for d in Path(settings.packs_dir).iterdir()
                 if d.is_dir() and not d.name.startswith((".", "_")) and (d / "pack.yaml").exists()]
    except OSError:
        return None
    return packs[0] if len(packs) == 1 else None


def main(argv=None) -> int:
    from . import logsetup
    logsetup.setup()
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except Exception as e:  # noqa: BLE001
        if not model_unreachable(e):
            if os.environ.get("AGENT_DEBUG"):
                raise
            logging.getLogger("agent_tester").error("command failed", exc_info=e)
            log = _save_trace(Path("agent_error.log"))
            print(f"\n{friendly_error(e)}\n(Details saved to {log}; rerun with AGENT_DEBUG=1 to see the traceback.)")
            return 1
        from .config import Settings
        print("\n" + unreachable_msg(Settings.load(), e))
        return 4


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agent_tester")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    t = sub.add_parser("test")
    t.add_argument("task_id", type=int)
    t.add_argument("--cases", "--case", dest="case", help="comma-separated case ids, e.g. TC1,TC3")
    t.add_argument("--retest-bug", type=int, help="known bug number from facts.md, or a Redmine issue posted from a draft")
    t.add_argument("--env", default=None, help="staging, pre-prod, production or local (asked if not given)")
    t.add_argument("--headed", action="store_true", help="show the testers' browser windows (debugging)")
    t.add_argument("--dry-run", action="store_true", help="plan and questions only")
    t.add_argument("--cases-file", type=Path, default=None,
                   help="Markdown/text file of test cases to use instead of the ticket's Redmine Test Cases task")
    t.add_argument("--fresh-plan", action="store_true", help="plan again even if a plan-only draft exists")
    t.add_argument("--from-draft", dest="draft_run", default=None, help="run folder of a plan-only run to execute")
    t.add_argument("--no-subtasks", dest="subtasks", action="store_false",
                   help="test only this ticket, not its subtasks and related tickets")
    t.add_argument("--email", action="store_true", help="email the report to REPORT_EMAIL_TO")
    t.add_argument("--email-progress", action="store_true", help="also email start, halfway, pause and finish")
    rr = sub.add_parser("rerun")
    rr.add_argument("task_id", type=int)
    rr.add_argument("--cases", default=None, help="default: all cases of the plan")
    rr.add_argument("--from", dest="src_run", default=None, help="run folder whose plan to rerun (default: latest approved)")
    rr.add_argument("--failed", action="store_true", help="only the cases that did not PASS in --from")
    rr.add_argument("--headed", action="store_true", help="show the testers' browser windows")
    wp = sub.add_parser("web", help="open the web UI (runs, live steps, reports)")
    wp.add_argument("--port", type=int, default=8765)
    wp.add_argument("--host", default="127.0.0.1")
    rp = sub.add_parser("report")
    rp.add_argument("run_dir", type=Path)
    rp.add_argument("--project")
    bg = sub.add_parser("bugs")
    bg.add_argument("run_dir", type=Path)
    bg.add_argument("--post", action="store_true")
    bg.add_argument("--project")
    r = sub.add_parser("resume")
    r.add_argument("run_dir", type=Path)
    r.add_argument("--headed", action="store_true", help="show the testers' browser windows")
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
    m.add_argument("--project")
    ev = sub.add_parser("eval", help="replay self-test against past reports")
    ev.add_argument("--project")
    ev.add_argument("--live", action="store_true", help="use the real judge (needs the LLM)")
    ev.add_argument("--rebuild", action="store_true", help="re-extract known answers from evals/source/*.pdf")
    ev.add_argument("--cases", help="only these case ids")
    hp = sub.add_parser("helpers", help="list helpers, or recipes worth turning into helpers")
    hp.add_argument("action", choices=["list", "candidates"])
    hp.add_argument("--project")
    ob = sub.add_parser("onboard", help="draft a pack for a new project from Redmine")
    ob.add_argument("ref", help="Redmine issue id, project id or project identifier")
    ob.add_argument("--slug")
    pk = sub.add_parser("pack", help="create a pack from the template, or validate one")
    pk.add_argument("action", choices=["new", "validate"])
    pk.add_argument("slug")
    pk.add_argument("--name")
    pk.add_argument("--project-id", type=int, action="append", default=[])
    cs = sub.add_parser("cases", help="show generated test cases of a run, or post them to Redmine")
    cs.add_argument("run_dir", type=Path)
    cs.add_argument("--post", action="store_true")
    cs.add_argument("--project")
    lo = sub.add_parser("leftovers")
    lo.add_argument("run_dir", type=Path)
    a = ap.parse_args(argv)
    settings = Settings.load()
    if getattr(a, "headed", False):
        os.environ["QA_HEADED"] = "1"
    if a.cmd in ("test", "rerun", "resume", "onboard") and not getattr(a, "dry_run", False):
        msg = model_host_ok(settings)
        if msg:
            print(msg)
            return 4
    if a.cmd == "check":
        return cmd_check(settings)
    if a.cmd == "test":
        if a.env is not None:
            from .preflight.gap_resolver import normalize_env
            env = normalize_env(a.env)
            if env is None:
                print(f"Unknown environment {a.env!r}: use staging, pre-prod, production or local.")
                return 2
            if env == "prod":
                from .gate import confirm_prod
                if not confirm_prod():
                    return 2
            a.env = env
        return cmd_test(settings, a.task_id, a.case, a.env, a.dry_run, a.retest_bug, a.email, a.email_progress,
                        fresh_plan=a.fresh_plan, draft_run=a.draft_run, subtasks=a.subtasks,
                        cases_file=a.cases_file)
    if a.cmd == "rerun":
        return cmd_rerun(settings, a.task_id, a.cases, a.src_run, a.failed)
    if a.cmd == "web":
        from .web.server import serve
        return serve(a.host, a.port)
    if hasattr(a, "project") and not a.project and a.cmd in ("memory", "eval", "helpers"):
        a.project = default_project(settings)
        if not a.project:
            print("Error: say which project with --project <pack> (or set QA_PROJECT).", file=sys.stderr)
            return 2
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
    if a.cmd == "onboard":
        return cmd_onboard(settings, a.ref, a.slug)
    if a.cmd == "pack":
        return cmd_pack(settings, a.action, a.slug, a.name, a.project_id)
    if a.cmd == "helpers":
        return cmd_helpers(settings, a.action, a.project)
    if a.cmd == "cases":
        return cmd_cases(settings, a.run_dir, a.post, a.project)
    if a.cmd == "eval":
        return cmd_eval(settings, a.project, a.live, a.rebuild, a.cases)
    return cmd_resume(settings, a.run_dir)


if __name__ == "__main__":
    sys.exit(main())
