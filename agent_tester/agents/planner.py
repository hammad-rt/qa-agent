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


async def plan_task(settings: Settings, pack, task_text: str, task_id: int, env: str, only_case: str | None = None,
                    max_rounds: int = 3, lessons: str = "", trace_path=None, generate: bool = False,
                    answers: dict | None = None, preapprovable: list[str] | None = None) -> TestPlan:
    task_text = redact(task_text)
    planner = build_planner(settings, pack, env)
    reviewer = build_reviewer(settings, task_text)
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
        plan = add_repair_case(plan, pack)
        fixed_issues = [i for i in check_plan(plan) if not i.startswith("requirement ")]
        problems = ([] if review.get("ok") else list(review.get("problems", []))) + fixed_issues
        if not problems:
            return plan
        feedback = "\n\nProblems to fix (reviewer and plan checks):\n- " + "\n- ".join(problems)
    return plan
