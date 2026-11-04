"""Planner + reviewer (LLM). Returns a validated TestPlan; the fixed checks run after."""
from __future__ import annotations

from ..config import ROOT, Settings
from ..models import TestPlan
from ..redact import redact
from . import prompts
from .llm import model, run_json


def build_planner(settings: Settings, pack, env: str):
    from google.adk.agents import LlmAgent
    core = (ROOT / "core" / "core-testing-spec.md").read_text()
    instr = prompts.PLANNER.format(
        apps=", ".join(pack.apps), action_types=", ".join(pack.config.get("preapprovable_actions", []) + pack.config.get("always_ask_actions", [])),
        env=env, facts=pack.facts, core=core)
    return LlmAgent(name="planner", model=model(settings, json_mode=True, job="planner"), instruction=_static(instr))


def build_reviewer(settings: Settings, task_text: str):
    from google.adk.agents import LlmAgent
    return LlmAgent(name="reviewer", model=model(settings, json_mode=True, job="planner"), instruction=_static(prompts.REVIEWER.format(task=task_text)))


def _static(s: str):
    """A callable instruction skips ADK's {state} templating, so braces in docs stay literal."""
    return lambda _ctx: s


PLAN_BATCH = int(__import__("os").environ.get("PLAN_BATCH", "5"))   # cases per planner answer for long lists


async def plan_in_batches(settings, pack, task_text: str, task_id: int, env: str, ids: list[str], repair: bool = True,
                          **kw) -> TestPlan:
    """A long case list (a cases file) is planned a few cases at a time, so one answer never has to hold them all;
    ids and depends_on are kept as written, then the parts are joined in order and Quick Repair is added once."""
    parts = []
    for i in range(0, len(ids), PLAN_BATCH):
        batch = ids[i:i + PLAN_BATCH]
        print(f"Planning cases {', '.join(batch)} ({i + len(batch)}/{len(ids)})")
        parts.append(await plan_task(settings, pack, task_text, task_id, env, only_case=",".join(batch),
                                     repair=False, **kw))
    cases, seen, reqs, rk = [], set(), [], set()
    conflicts, data, pre, base = [], [], [], []
    for p in parts:
        for c in p.cases:
            if c.id not in seen:
                seen.add(c.id)
                cases.append(c)
        for r in p.requirements:
            if r.key not in rk:
                rk.add(r.key)
                reqs.append(r)
        conflicts += [x for x in p.global_conflicts if x not in conflicts]
        data += [x for x in p.data_to_create if x not in data]
        pre += [a for a in p.preapprovable_actions if a not in pre]
        base += [b for b in p.baseline_steps if b not in base]
    plan = TestPlan(task_id=task_id, env=env, cases_source="task", cases=cases, requirements=reqs,
                    global_conflicts=conflicts, data_to_create=data, preapprovable_actions=pre, baseline_steps=base)
    if repair:
        from ..casegen import add_repair_case
        plan = add_repair_case(plan, pack)
    return plan


async def plan_task(settings: Settings, pack, task_text: str, task_id: int, env: str, only_case: str | None = None,
                    max_rounds: int = 3, lessons: str = "", trace_path=None, generate: bool = False,
                    answers: dict | None = None, preapprovable: list[str] | None = None,
                    repair: bool = True, batch_ids: list[str] | None = None) -> TestPlan:
    if batch_ids and len(batch_ids) > PLAN_BATCH:
        return await plan_in_batches(settings, pack, task_text, task_id, env, batch_ids, max_rounds=max_rounds,
                                     lessons=lessons, trace_path=trace_path, answers=answers,
                                     preapprovable=preapprovable, repair=repair)
    task_text = redact(task_text)
    planner = build_planner(settings, pack, env)
    reviewer = build_reviewer(settings, (f"(This plan covers ONLY cases {only_case}. Do not report the task's other "
                                         f"cases as missing.)\n\n" if only_case else "") + task_text)
    ask = f"Task #{task_id}:\n{task_text}\n\n" + (f"Saved lessons for this project (apply them, cite ids in inferred_source):\n{lessons}\n\n" if lessons else "") + (f"Plan ONLY these cases: {only_case}.\n" if only_case else "") + (prompts.GENERATE if generate else "") + (
        "\nUser answers at the plan gate (binding):\n" + "\n".join(f"- {k}: {v}" for k, v in answers.items()) + "\n"
        if answers else "")
    feedback = ""
    for _ in range(max_rounds):
        def to_plan(data: dict) -> TestPlan:
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            data.setdefault("task_id", task_id)
            data["env"] = env
            return TestPlan.model_validate(data)

        raw_dir = trace_path.parent if trace_path else None
        plan = await run_json(planner, ask + feedback, settings, validate=to_plan, label="planner",
                              raw_dir=raw_dir, trace_path=trace_path)
        if generate:                           # code marks generated cases, whatever the model said
            from ..casegen import mark_generated
            plan = mark_generated(plan)
        review = await run_json(reviewer, plan.model_dump_json(indent=1), settings, label="reviewer",
                                raw_dir=raw_dir, trace_path=trace_path)
        from ..plan_checks import autofix, check_plan
        plan = autofix(plan, preapprovable)
        from ..casegen import add_repair_case  # SuiteCRM/SugarCRM projects always start with QRR
        if repair:                             # feature mode adds it once for the merged plan instead
            plan = add_repair_case(plan, pack)
        fixed_issues = [i for i in check_plan(plan) if not i.startswith("requirement ")]
        problems = ([] if review.get("ok") else list(review.get("problems", []))) + fixed_issues
        if not problems:
            return plan
        feedback = "\n\nProblems to fix (reviewer and plan checks):\n- " + "\n- ".join(problems)
    return plan
