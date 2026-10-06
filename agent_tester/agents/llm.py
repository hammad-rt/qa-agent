"""Model factory and a small helper to run one ADK agent turn. ADK is imported lazily so the
deterministic core works (and is tested) without it."""
from __future__ import annotations

import asyncio
import json
import re
import uuid

from ..config import Settings

_sem: asyncio.Semaphore | None = None


class LLMOutputError(RuntimeError):
    """The model kept returning output that could not be parsed or validated."""


def model(settings: Settings, json_mode: bool = False):
    """json_mode asks the server for a syntactically valid JSON object (vLLM supports it). LLM_JSON_MODE=0 turns it off."""
    import os
    from google.adk.models.lite_llm import LiteLlm
    extra = {}
    if json_mode and os.environ.get("LLM_JSON_MODE", "1") != "0":
        extra["response_format"] = {"type": "json_object"}
    return LiteLlm(model=f"openai/{settings.llm_model}", api_base=settings.llm_base_url, api_key=settings.llm_api_key, **extra)


async def run_agent(agent, prompt: str, settings: Settings, state: dict | None = None, app_name: str = "qa-agent",
                    trace_path=None, retry: bool = False) -> str:
    """Run one agent to completion and return its final text. retry=True only for agents without side
    effects (planner, reviewer, judge); testers are never replayed automatically."""
    if retry:
        from ..reliability import with_retries
        return await with_retries(lambda: run_agent(agent, prompt, settings, state, app_name, trace_path, False))
    global _sem
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    if _sem is None:
        _sem = asyncio.Semaphore(settings.llm_max_concurrency)
    svc = InMemorySessionService()
    sid = uuid.uuid4().hex
    await svc.create_session(app_name=app_name, user_id="qa", session_id=sid, state=state or {})
    runner = Runner(agent=agent, app_name=app_name, session_service=svc)
    final = ""
    async with _sem:
        async for ev in runner.run_async(user_id="qa", session_id=sid,
                                         new_message=types.Content(role="user", parts=[types.Part(text=prompt)])):
            if trace_path:
                _trace(trace_path, ev)
            if ev.content and ev.content.parts:
                text = "".join(p.text or "" for p in ev.content.parts if getattr(p, "text", None))
                if text and ev.is_final_response():
                    final = text
    return final


def extract_json(text: str) -> dict:
    """Models sometimes wrap JSON in prose or fences, or slip a comma; take the first JSON object and
    repair the common slips (missing comma between items, trailing comma, comments)."""
    text = text or ""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    if not raw:
        raise json.JSONDecodeError("no JSON object in the reply", text, 0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as first:
        try:
            return json.loads(repair_json(raw))
        except json.JSONDecodeError:
            raise first


_END = r'(?:"|\d|true|false|null|\]|\})'


def repair_json(raw: str) -> str:
    s = re.sub(r"^\s*//.*$", "", raw, flags=re.M)                       # line comments
    s = re.sub(r",(\s*[\]}])", r"\1", s)                                # trailing commas
    s = re.sub(rf"({_END})(\s*\n\s*)(?=[\"{{\[])", r"\1,\2", s)          # missing comma at a line break
    s = re.sub(r'(\}|\])(\s*)(?=\{)', r"\1,\2", s)                      # }{ or ]{ on one line
    return s


async def run_json(agent, prompt: str, settings: Settings, *, validate=None, label: str = "llm",
                   raw_dir=None, trace_path=None, attempts: int = 3):
    """Run an agent that must answer with JSON. On a parse or validation error the raw reply is saved
    (masked) and the model is asked to correct it, up to `attempts` times. Returns validate(data) or data."""
    from pydantic import ValidationError

    from ..redact import redact, scrub_secrets
    ask, last_err = prompt, ""
    for n in range(1, attempts + 1):
        raw = await run_agent(agent, ask, settings, trace_path=trace_path, retry=True)
        try:
            data = extract_json(raw)
            return validate(data) if validate else data
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as e:
            last_err = str(e).splitlines()[0][:300] if isinstance(e, json.JSONDecodeError) else str(e)[:1500]
            if raw_dir:
                from pathlib import Path
                Path(raw_dir).mkdir(parents=True, exist_ok=True)
                (Path(raw_dir) / f"{label}_raw_{n}.txt").write_text(scrub_secrets(redact(raw or "")))
            ask = (f"{prompt}\n\nYour previous reply could not be used: {last_err}\n"
                   f"Previous reply:\n{(raw or '')[:20000]}\n\nReply again with ONLY the corrected JSON object, nothing else.")
    where = f" Raw replies are in {raw_dir}." if raw_dir else ""
    raise LLMOutputError(f"{label}: the model returned unusable JSON {attempts} times ({last_err}).{where}")


def _trace(path, ev) -> None:
    """Append a masked one-line summary of an ADK event to trace.jsonl."""
    from datetime import datetime

    from ..redact import redact, scrub_secrets
    rec = {"at": datetime.now().isoformat(timespec="seconds"), "author": getattr(ev, "author", None)}
    parts = (ev.content.parts if ev.content and ev.content.parts else [])
    calls = [{"name": p.function_call.name, "args": dict(p.function_call.args or {})} for p in parts if getattr(p, "function_call", None)]
    resps = [p.function_response.name for p in parts if getattr(p, "function_response", None)]
    text = "".join(p.text or "" for p in parts if getattr(p, "text", None))
    if calls:
        rec["calls"] = calls
    if resps:
        rec["responses"] = resps
    if text:
        rec["text"] = text[:500]
    line = scrub_secrets(redact(json.dumps(rec, default=str)))
    with open(path, "a") as f:
        f.write(line + "\n")
