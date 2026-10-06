"""Planner + reviewer (LLM). Returns a validated TestPlan; the fixed checks run after."""
from __future__ import annotations

from ..config import ROOT, Settings
from ..models import TestPlan
from ..redact import redact
from . import prompts
from .llm import extract_json, model, run_agent


def build_planner(settings: Settings, pack, env: str):
    from google.adk.agents import LlmAgent
    core = (ROOT / "core" / "core-testing-spec.md").read_text()
    instr = prompts.PLANNER.format(
        apps=", ".join(pack.apps), action_types=", ".join(pack.config.get("preapprovable_actions", []) + pack.config.get("always_ask_actions", [])),
        env=env, facts=pack.facts, core=core)
    return LlmAgent(name="planner", model=model(settings), instruction=_static(instr))


def build_reviewer(settings: Settings, task_text: str):
    from google.adk.agents import LlmAgent
    return LlmAgent(name="reviewer", model=model(settings), instruction=_static(prompts.REVIEWER.format(task=task_text)))


def _static(s: str):
    """A callable instruction skips ADK's {state} templating, so braces in docs stay literal."""
    return lambda _ctx: s


async def plan_task(settings: Settings, pack, task_text: str, task_id: int, env: str, only_case: str | None = None,
                    max_rounds: int = 3, lessons: str = "", trace_path=None) -> TestPlan:
    task_text = redact(task_text)
    planner = build_planner(settings, pack, env)
    reviewer = build_reviewer(settings, task_text)
    ask = f"Task #{task_id}:\n{task_text}\n\n" + (f"Saved lessons for this project (apply them, cite ids in inferred_source):\n{lessons}\n\n" if lessons else "") + (f"Plan ONLY these cases: {only_case}.\n" if only_case else "")
    feedback = ""
    for _ in range(max_rounds):
        raw = await run_agent(planner, ask + feedback, settings, trace_path=trace_path, retry=True)
        data = extract_json(raw)
        data.setdefault("task_id", task_id)
        data["env"] = env
        plan = TestPlan.model_validate(data)
        review = extract_json(await run_agent(reviewer, plan.model_dump_json(indent=1), settings, trace_path=trace_path, retry=True))
        if review.get("ok"):
            return plan
        feedback = "\n\nReviewer problems to fix:\n- " + "\n- ".join(review.get("problems", []))
    return plan
