"""CLI: python -m agent_tester <command>

  check                               connectivity to the LLM, Redmine and the pack's environments
  test <task_id> [--case TC3] [--env staging] [--dry-run]
  resume <run_dir>
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .config import ROOT, Settings


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


def cmd_test(settings: Settings, task_id: int, case: str | None, env: str, dry_run: bool) -> int:
    from . import gate
    from .agents.planner import plan_task
    from .memory.resources import ResourceMemory
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

    plan = asyncio.run(plan_task(settings, pack, bundle.all_text(), task_id, env, only_case=case))
    wiki_proj = pack.config["redmine"].get("wiki_project") or bundle.issue["project"]["id"]
    wiki = {t: (rm.wiki_page(wiki_proj, t) or "") for t in pack.config["redmine"].get("wiki_pages", [])}
    memory = ResourceMemory(ROOT / "memory" / "resources.json")
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
    return _execute(settings, pack, plan, state, policy)


def _execute(settings, pack, plan, state, policy) -> int:
    from .agents.judge import make_judge
    from .agents.tester import make_tester
    from .coordinator import RunPaused, run_plan
    from .report.builder import build_report

    tester = make_tester(settings, pack, plan.env, policy, state.dir)
    testers = {app: tester for app in pack.apps}
    try:
        results = run_plan(plan, state, testers, make_judge(settings, pack),
                           on_case_done=lambda r: print(f"  {r.case_id}: {r.verdict}" + (f" (still present: bug {r.known_bug})" if r.known_bug else "")))
    except RunPaused as e:
        print(f"\nPaused: {e}\nResume with: python -m agent_tester resume {state.dir}")
        return 3
    out = build_report(state)
    print(f"\nReport: {out}")
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
    return _execute(settings, pack, plan, state, policy)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agent_tester")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    t = sub.add_parser("test")
    t.add_argument("task_id", type=int)
    t.add_argument("--case")
    t.add_argument("--env", default="staging", choices=["staging", "prod"])
    t.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("resume")
    r.add_argument("run_dir", type=Path)
    a = ap.parse_args(argv)
    settings = Settings.load()
    if a.cmd == "check":
        return cmd_check(settings)
    if a.cmd == "test":
        if a.env == "prod":
            print("Prod runs are read-only; every write needs your approval.")
        return cmd_test(settings, a.task_id, a.case, a.env, a.dry_run)
    return cmd_resume(settings, a.run_dir)


if __name__ == "__main__":
    sys.exit(main())
