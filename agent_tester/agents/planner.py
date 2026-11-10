"""Planner + reviewer (LLM). Returns a validated TestPlan; the fixed checks run after."""
from __future__ import annotations

import asyncio
import os

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


# Legacy fixed count (overridden by token-budget sizing when PLAN_BATCH_TOKENS is set).
PLAN_BATCH = int(os.environ.get("PLAN_BATCH", "8"))
PLAN_BATCH_TOKENS = int(os.environ.get("PLAN_BATCH_TOKENS", "6000"))  # ~case texts packed per call
PLAN_CONCURRENCY = int(os.environ.get("PLAN_CONCURRENCY", "3"))


def _case_blocks(task_text: str, ids: list[str]) -> list[tuple[str, str]]:
    """Best-effort extract per-case text blobs from the task markdown for sizing."""
    import re
    blocks = []
    for cid in ids:
        # Match headings / bold ids like **E1** or ### E1 or E1:
        m = re.search(rf"(?is)(?:^|\n)[^\n]*\b{re.escape(cid)}\b[^\n]*\n(.*?)(?=\n[^\n]*\b(?:TC\d+|[A-Z]\d+(?:-\d+)?)\b|\Z)",
                      task_text)
        blob = m.group(0) if m else cid
        blocks.append((cid, blob[:4000]))
    return blocks


def size_batches(task_text: str, ids: list[str], *, token_budget: int | None = None,
                 max_count: int | None = None) -> list[list[str]]:
    """Split case ids into batches sized by approximate token budget (not a fixed count)."""
    budget = token_budget if token_budget is not None else PLAN_BATCH_TOKENS
    cap = max_count if max_count is not None else max(PLAN_BATCH, 12)
    blocks = dict(_case_blocks(task_text, ids))
    batches: list[list[str]] = []
    cur: list[str] = []
    used = 0
    for cid in ids:
        # ~4 chars/token
        cost = max(80, (len(blocks.get(cid, cid)) + 3) // 4)
        if cur and (used + cost > budget or len(cur) >= cap):
            batches.append(cur)
            cur, used = [], 0
        cur.append(cid)
        used += cost
    if cur:
        batches.append(cur)
    return batches or [ids]


def _merge_plans(parts: list[TestPlan], task_id: int, env: str) -> TestPlan:
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
    return TestPlan(task_id=task_id, env=env, cases_source="task", cases=cases, requirements=reqs,
                    global_conflicts=conflicts, data_to_create=data, preapprovable_actions=pre, baseline_steps=base)


async def plan_in_batches(settings, pack, task_text: str, task_id: int, env: str, ids: list[str], repair: bool = True,
                          **kw) -> TestPlan:
    """Plan case batches sized by token budget; run independent batches concurrently (bounded)."""
    batches = size_batches(task_text, ids)
    sem = asyncio.Semaphore(max(1, PLAN_CONCURRENCY))
    parts: list[TestPlan | None] = [None] * len(batches)

    async def one(i: int, batch: list[str]):
        async with sem:
            print(f"Planning cases {', '.join(batch)} (batch {i + 1}/{len(batches)})")
            parts[i] = await plan_task(settings, pack, task_text, task_id, env, only_case=",".join(batch),
                                       repair=False, **kw)

    await asyncio.gather(*(one(i, b) for i, b in enumerate(batches)))
    plan = _merge_plans([p for p in parts if p is not None], task_id, env)
    if repair:
        from ..casegen import add_repair_case
        plan = add_repair_case(plan, pack)
    return plan


async def plan_task(settings: Settings, pack, task_text: str, task_id: int, env: str, only_case: str | None = None,
                    max_rounds: int = 3, lessons: str = "", trace_path=None, generate: bool = False,
                    answers: dict | None = None, preapprovable: list[str] | None = None,
                    repair: bool = True, batch_ids: list[str] | None = None,
                    run_dir=None, reuse_plan: bool = True) -> TestPlan:
    # Reuse an approved plan when ticket + cases + pack hash match.
    if reuse_plan and batch_ids and run_dir is not None and not only_case and not generate:
        from ..plan_cache import find_reusable_plan, plan_fingerprint, save_plan_cache
        fp = plan_fingerprint(task_text, batch_ids, pack)
        hit = find_reusable_plan(settings.runs_dir, fp, exclude=run_dir)
        if hit is not None:
            cached, src = hit
            print(f"Reusing approved plan from {src.name} (unchanged ticket/cases/pack)")
            if repair:
                from ..casegen import add_repair_case
                cached = add_repair_case(cached, pack)
            try:
                save_plan_cache(run_dir, fp, cached)
            except OSError:
                pass
            return cached

    if batch_ids and len(batch_ids) > 1 and not only_case:
        # Token-budget batches whenever there is more than one case to plan.
        batches = size_batches(task_text, batch_ids)
        if len(batches) > 1 or (len(batch_ids) > PLAN_BATCH):
            plan = await plan_in_batches(settings, pack, task_text, task_id, env, batch_ids, max_rounds=max_rounds,
                                         lessons=lessons, trace_path=trace_path, answers=answers,
                                         preapprovable=preapprovable, repair=repair)
            if reuse_plan and run_dir is not None:
                from ..plan_cache import plan_fingerprint, save_plan_cache
                try:
                    save_plan_cache(run_dir, plan_fingerprint(task_text, batch_ids, pack), plan)
                except OSError:
                    pass
            return plan

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
            if reuse_plan and run_dir is not None and batch_ids:
                from ..plan_cache import plan_fingerprint, save_plan_cache
                try:
                    save_plan_cache(run_dir, plan_fingerprint(task_text, batch_ids, pack), plan)
                except OSError:
                    pass
            return plan
        feedback = "\n\nProblems to fix (reviewer and plan checks):\n- " + "\n- ".join(problems)
    return plan
