"""Planner + reviewer (LLM). Returns a validated TestPlan; the fixed checks run after."""
from __future__ import annotations

import asyncio
import os
import time

from ..config import ROOT, Settings
from ..models import TestPlan
from ..redact import redact
from . import prompts
from .llm import LLMOutputError, model, run_json


def build_planner(settings: Settings, pack, env: str, *, temperature: float | None = None):
    from google.adk.agents import LlmAgent
    core = (ROOT / "core" / "core-testing-spec.md").read_text()
    instr = prompts.PLANNER.format(
        apps=", ".join(pack.apps), action_types=", ".join(pack.config.get("preapprovable_actions", []) + pack.config.get("always_ask_actions", [])),
        env=env, facts=pack.facts, core=core)
    return LlmAgent(name="planner", model=model(settings, json_mode=True, job="planner", temperature=temperature),
                    instruction=_static(instr))


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
# Empty/{} planner replies: retry (with split) this many times per leaf batch, then give up.
PLAN_EMPTY_RETRIES = int(os.environ.get("PLAN_EMPTY_RETRIES", "2"))
# Hard cap across all batch attempts (wall clock) and total split attempts.
PLAN_MAX_SECONDS = int(os.environ.get("PLAN_MAX_SECONDS", "600"))
PLAN_MAX_ATTEMPTS = int(os.environ.get("PLAN_MAX_ATTEMPTS", "12"))
PLAN_RETRY_TEMPERATURE = float(os.environ.get("PLAN_RETRY_TEMPERATURE", "0.4"))


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


def _plan_progress(run_dir, line: str) -> None:
    """Console + progress.log so the web UI shows planning status while status is still 'new'."""
    print(line)
    if not run_dir:
        return
    try:
        from datetime import datetime
        from pathlib import Path
        p = Path(run_dir) / "progress.log"
        with p.open("a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def _empty_plan_reason(plan: TestPlan | None, want: list[str]) -> str | None:
    if plan is None:
        return "no plan"
    got = [c.id for c in (plan.cases or [])]
    if not got:
        return "empty cases array"
    missing = [i for i in want if i not in set(got)]
    if missing:
        return f"missing cases {', '.join(missing[:8])}{'…' if len(missing) > 8 else ''}"
    return None


async def _plan_batch_resilient(settings, pack, task_text: str, task_id: int, env: str, batch: list[str],
                                *, batch_label: str, deadline: float, attempt_budget: list[int],
                                budget_lock: asyncio.Lock, repair: bool = False, run_dir=None, **kw) -> TestPlan:
    """Plan one batch; on empty/{} / missing cases, retry up to PLAN_EMPTY_RETRIES and split in half."""
    queue: list[tuple[list[str], int]] = [(list(batch), 0)]  # (ids, empty_retry_n)
    parts: list[TestPlan] = []
    while queue:
        if time.monotonic() > deadline:
            raise LLMOutputError(
                f"Planning hit the {PLAN_MAX_SECONDS}s time cap while working on {batch_label}. "
                f"Resume with: python -m agent_tester test {task_id} --env {env}")
        async with budget_lock:
            if attempt_budget[0] >= PLAN_MAX_ATTEMPTS:
                raise LLMOutputError(
                    f"Planning stopped after {PLAN_MAX_ATTEMPTS} batch attempts ({batch_label}). "
                    f"Resume with: python -m agent_tester test {task_id} --env {env}")
            attempt_budget[0] += 1
            n = attempt_budget[0]
        cur, empty_n = queue.pop(0)
        ids_txt = ", ".join(cur)
        _plan_progress(run_dir,
                       f"{batch_label}: planning {len(cur)} case(s) "
                       f"(global attempt {n}/{PLAN_MAX_ATTEMPTS}): {ids_txt}")
        nudge = ""
        temp = None
        if empty_n:
            nudge = (f"\nReturn JSON with a cases array for exactly these ids: {ids_txt}. "
                     "Do not return {} or an empty cases list.")
            temp = PLAN_RETRY_TEMPERATURE
        try:
            plan = await plan_task(settings, pack, task_text + nudge, task_id, env,
                                   only_case=",".join(cur), repair=False, temperature=temp,
                                   run_dir=run_dir, **kw)
            why = _empty_plan_reason(plan, cur)
            if why:
                raise LLMOutputError(f"planner returned {why}")
            parts.append(plan)
            _plan_progress(run_dir, f"{batch_label}: finished {len(cur)} case(s)")
            continue
        except LLMOutputError as e:
            why = str(e)
            if empty_n >= PLAN_EMPTY_RETRIES or len(cur) <= 1:
                _plan_progress(run_dir, f"{batch_label}: giving up on {ids_txt} ({why})")
                raise LLMOutputError(
                    f"{batch_label}: the model kept returning an empty or unusable plan for "
                    f"{ids_txt} ({why}). Resume with: python -m agent_tester test {task_id} --env {env}") from e
            mid = max(1, len(cur) // 2)
            left, right = cur[:mid], cur[mid:]
            _plan_progress(
                run_dir,
                f"{batch_label}: the model returned an empty plan (attempt {empty_n + 1}/"
                f"{PLAN_EMPTY_RETRIES + 1}); retrying with {len(left)} and {len(right)} cases")
            # Prefer smaller batches next; keep order stable.
            queue = [(left, empty_n + 1), (right, empty_n + 1)] + queue
    return _merge_plans(parts, task_id, env)


async def plan_in_batches(settings, pack, task_text: str, task_id: int, env: str, ids: list[str], repair: bool = True,
                          run_dir=None, **kw) -> TestPlan:
    """Plan case batches sized by token budget; run independent batches concurrently (bounded).

    Empty/`{}`/no-text planner output is a failed attempt: retry (with a nudge + slightly higher
    temperature) and split the batch in half down to 1 case. A wall-clock and attempt cap stop
    silent multi-minute loops. Per-batch status lines go to the console and progress.log."""
    batches = size_batches(task_text, ids)
    sem = asyncio.Semaphore(max(1, PLAN_CONCURRENCY))
    parts: list[TestPlan | None] = [None] * len(batches)
    status = ["pending"] * len(batches)
    deadline = time.monotonic() + max(30, PLAN_MAX_SECONDS)
    attempt_budget = [0]
    lock = asyncio.Lock()

    def _status_line() -> str:
        bits = []
        for i, st in enumerate(status):
            bits.append(f"{i + 1}/{len(batches)}={st}")
        return "Planning status: " + ", ".join(bits)

    async def one(i: int, batch: list[str]):
        label = f"Batch {i + 1}/{len(batches)}"
        async with sem:
            async with lock:
                status[i] = "running"
                _plan_progress(run_dir, f"{label} starting ({len(batch)} cases). {_status_line()}")
            try:
                parts[i] = await _plan_batch_resilient(
                    settings, pack, task_text, task_id, env, batch,
                    batch_label=label, deadline=deadline, attempt_budget=attempt_budget,
                    budget_lock=lock, repair=False, run_dir=run_dir, **kw)
                async with lock:
                    status[i] = "done"
                    _plan_progress(run_dir, f"{label} done. {_status_line()}")
            except Exception:
                async with lock:
                    status[i] = "failed"
                    _plan_progress(run_dir, f"{label} failed. {_status_line()}")
                raise

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
                    run_dir=None, reuse_plan: bool = True, temperature: float | None = None) -> TestPlan:
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
                                         preapprovable=preapprovable, repair=repair, run_dir=run_dir)
            if reuse_plan and run_dir is not None:
                from ..plan_cache import plan_fingerprint, save_plan_cache
                try:
                    save_plan_cache(run_dir, plan_fingerprint(task_text, batch_ids, pack), plan)
                except OSError:
                    pass
            return plan

    task_text = redact(task_text)
    planner = build_planner(settings, pack, env, temperature=temperature)
    reviewer = build_reviewer(settings, (f"(This plan covers ONLY cases {only_case}. Do not report the task's other "
                                         f"cases as missing.)\n\n" if only_case else "") + task_text)
    want = [x.strip() for x in (only_case or "").split(",") if x.strip()]
    ask = f"Task #{task_id}:\n{task_text}\n\n" + (f"Saved lessons for this project (apply them, cite ids in inferred_source):\n{lessons}\n\n" if lessons else "") + (f"Plan ONLY these cases: {only_case}.\n" if only_case else "") + (prompts.GENERATE if generate else "") + (
        "\nUser answers at the plan gate (binding):\n" + "\n".join(f"- {k}: {v}" for k, v in answers.items()) + "\n"
        if answers else "")
    if want:
        ask += f"Reply with a JSON object whose cases array covers exactly: {', '.join(want)}.\n"
    feedback = ""
    for _ in range(max_rounds):
        def to_plan(data: dict) -> TestPlan:
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            if data == {}:
                raise ValueError("the reply was an empty object {}; fill in every field of the schema")
            data.setdefault("task_id", task_id)
            data["env"] = env
            plan = TestPlan.model_validate(data)
            # When planning a named batch (only_case), an empty/missing cases list is a failed attempt.
            if want:
                why = _empty_plan_reason(plan, want)
                if why:
                    raise ValueError(f"planner returned {why}")
            return plan

        raw_dir = trace_path.parent if trace_path else None

        def _on_attempt(n, attempts, err, raw):
            empty = "empty" in (err or "") or "no text" in (err or "") or not (raw or "").strip() or (raw or "").strip() == "{}"
            _plan_progress(run_dir, f"planner: attempt {n}/{attempts} failed"
                                    f"{' (empty plan)' if empty else ''}: {err}")

        plan = await run_json(planner, ask + feedback, settings, validate=to_plan, label="planner",
                              raw_dir=raw_dir, trace_path=trace_path, on_attempt=_on_attempt)
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
