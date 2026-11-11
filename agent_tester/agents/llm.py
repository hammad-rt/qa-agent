"""Model factory and a small helper to run one ADK agent turn. ADK is imported lazily so the
deterministic core works (and is tested) without it."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from ..config import Settings, job_model

_sem: asyncio.Semaphore | None = None

# Hard caps so a hung provider cannot hold the LLM semaphore forever.
# LiteLLM: timeout = whole response; stream_timeout = wait for next chunk (docs.litellm.ai).
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", os.environ.get("LLM_CALL_TIMEOUT", "180")))
LLM_STREAM_TIMEOUT = float(os.environ.get("LLM_STREAM_TIMEOUT", "60"))
# No usable text from ADK events for this long → fail the attempt (empty keepalive events don't count).
LLM_IDLE_TIMEOUT = float(os.environ.get("LLM_IDLE_TIMEOUT", "90"))

# When the tester is blocked on a person (stuck ask / ask_user / approval), pause the idle clock
# so a long human answer does not fail the LLM turn with "no text for 90s".
_human_wait: ContextVar[bool] = ContextVar("llm_human_wait", default=False)


@contextmanager
def pause_idle_for_human():
    """Context manager: wrap stuck-ask / ask_user waits so LLM idle timeout does not fire."""
    token = _human_wait.set(True)
    try:
        yield
    finally:
        _human_wait.reset(token)


def human_wait_active() -> bool:
    return bool(_human_wait.get())


class LLMOutputError(RuntimeError):
    """The model kept returning output that could not be parsed or validated."""


def model(settings: Settings, json_mode: bool = False, max_tokens: int | None = None, job: str = "tester",
          temperature: float | None = None):
    """json_mode asks the server for a syntactically valid JSON object (vLLM supports it). LLM_JSON_MODE=0 turns it off."""
    from google.adk.models.lite_llm import LiteLlm
    extra = {"max_tokens": max_tokens or int(os.environ.get("LLM_MAX_TOKENS", "8192"))}   # the server's default is only 512
    if json_mode and os.environ.get("LLM_JSON_MODE", "1") != "0":
        extra["response_format"] = {"type": "json_object"}
    if temperature is not None:
        extra["temperature"] = float(temperature)
    # Pass through to litellm so hung HTTP streams are cancelled (not only checked between retries).
    if LLM_TIMEOUT > 0:
        extra["timeout"] = LLM_TIMEOUT
    if LLM_STREAM_TIMEOUT > 0:
        extra["stream_timeout"] = LLM_STREAM_TIMEOUT
    from ..redact import effective_api_key
    base, name, key = job_model(settings, job)
    # Placeholder keys (none/null/empty) become a dummy — vLLM often needs no real key.
    return LiteLlm(model=f"openai/{name}", api_base=base, api_key=effective_api_key(key), **extra)


async def _iter_events_with_idle(aiter, *, idle_sec: float, call_deadline: float | None = None):
    """Yield ADK events; fail when no event arrives within idle_sec, or past call_deadline.

    While ``pause_idle_for_human()`` is active (stuck ask / ask_user), keep waiting without
    counting that time as LLM idle.

    The pending ``__anext__`` is polled with ``asyncio.wait`` (which never cancels it), so a
    slow first token (> one poll slice) does not cancel the ADK runner. It is cancelled only
    when we actually give up (idle exceeded / deadline hit) or the consumer stops iterating.
    """
    nxt = None
    try:
        while True:
            # Poll in short slices so a human-wait that starts mid-timeout is noticed.
            slice_sec = min(5.0, idle_sec)
            waited = 0.0
            nxt = asyncio.ensure_future(aiter.__anext__())
            while True:
                if human_wait_active():
                    waited = 0.0
                left = slice_sec
                if call_deadline is not None and not human_wait_active():
                    left = min(left, max(0.05, call_deadline - time.monotonic()))
                done, _ = await asyncio.wait({nxt}, timeout=left)
                if done:
                    break
                if human_wait_active():
                    continue
                waited += left
                if call_deadline is not None and time.monotonic() >= call_deadline:
                    raise LLMOutputError(
                        f"LLM call hit the {LLM_TIMEOUT:.0f}s per-call timeout")
                if waited >= idle_sec:
                    raise LLMOutputError(
                        f"no text for {LLM_IDLE_TIMEOUT:.0f}s (stream idle / empty ADK events)")
            try:
                ev = nxt.result()
            except StopAsyncIteration:
                nxt = None
                return
            nxt = None
            yield ev
    finally:
        if nxt is not None and not nxt.done():
            nxt.cancel()


async def run_agent(agent, prompt: str, settings: Settings, state: dict | None = None, app_name: str = "qa-agent",
                    trace_path=None, retry: bool = False, follow_up=None, max_follow_ups: int = 2) -> str:
    """Run one agent to completion and return its final text. retry=True only for agents without side
    effects (planner, reviewer, judge); testers are never replayed automatically.

    Each turn is bounded by LLM_TIMEOUT (LiteLLM + asyncio) and LLM_IDLE_TIMEOUT (no text in events).
    Cancellation releases the shared LLM semaphore so retries are not starved behind hung calls.
    """
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
    msg, left = prompt, max_follow_ups
    idle = max(0.05, LLM_IDLE_TIMEOUT)
    call_cap = max(idle + 0.05, LLM_TIMEOUT)

    async def _one_turn(user_msg: str) -> str:
        turn_final = ""
        saw_text = False
        last_text_at = time.monotonic()
        deadline = time.monotonic() + call_cap
        stream = runner.run_async(
            user_id="qa", session_id=sid,
            new_message=types.Content(role="user", parts=[types.Part(text=user_msg)]))
        async for ev in _iter_events_with_idle(stream.__aiter__(), idle_sec=idle, call_deadline=deadline):
            if trace_path:
                _trace(trace_path, ev)
            text = ""
            if ev.content and ev.content.parts:
                text = "".join(p.text or "" for p in ev.content.parts if getattr(p, "text", None))
            if text:
                saw_text = True
                last_text_at = time.monotonic()
                if ev.is_final_response():
                    turn_final = text
            elif (not human_wait_active()) and time.monotonic() - last_text_at >= idle:
                # Empty keepalive ADK events must not reset the idle clock forever.
                # Human waits (stuck ask) pause this clock via pause_idle_for_human().
                raise LLMOutputError(
                    f"no text for {LLM_IDLE_TIMEOUT:.0f}s (empty ADK events)")
            elif human_wait_active():
                last_text_at = time.monotonic()
        if not saw_text and not (turn_final or "").strip():
            # Completed with only empty keepalive-style events.
            raise LLMOutputError("no text for the whole LLM call (empty ADK events)")
        return turn_final

    try:
        async with _sem:
            while msg:
                try:
                    final = await asyncio.wait_for(_one_turn(msg), timeout=call_cap)
                except asyncio.TimeoutError as e:
                    raise LLMOutputError(
                        f"LLM call hit the {LLM_TIMEOUT:.0f}s per-call timeout") from e
                except asyncio.CancelledError:
                    # Propagate so plan deadline / SIGINT can free the semaphore promptly.
                    raise
                # follow_up(final) -> a message to continue the same session (history kept), or None to stop.
                msg = follow_up(final) if (follow_up and left > 0) else None
                left -= 1
    except asyncio.CancelledError:
        raise
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


def _cut_off(raw: str) -> bool:
    t = (raw or "").rstrip().rstrip("`").rstrip()
    return bool(t) and t.count("{") > t.count("}")


_END = r'(?:"|\d|true|false|null|\]|\})'


def repair_json(raw: str) -> str:
    s = re.sub(r"^\s*//.*$", "", raw, flags=re.M)                       # line comments
    s = re.sub(r",(\s*[\]}])", r"\1", s)                                # trailing commas
    s = re.sub(rf"({_END})(\s*\n\s*)(?=[\"{{\[])", r"\1,\2", s)          # missing comma at a line break
    s = re.sub(r'(\}|\])(\s*)(?=\{)', r"\1,\2", s)                      # }{ or ]{ on one line
    return s


def _empty_llm_reply(raw: str, data: dict | None = None) -> str | None:
    """Plain-language reason when the model returned nothing usable (empty text or {})."""
    if data == {}:
        return "empty object {}"
    if not (raw or "").strip():
        return "no text"
    return None


async def run_json(agent, prompt: str, settings: Settings, *, validate=None, label: str = "llm",
                   raw_dir=None, trace_path=None, attempts: int = 3, on_attempt=None):
    """Run an agent that must answer with JSON. On a parse or validation error the raw reply is saved
    (masked) and the model is asked to correct it, up to `attempts` times. Returns validate(data) or data.

    ``on_attempt(n, attempts, err, raw)`` is called after each failed try (for console/progress logs)."""
    from pydantic import ValidationError

    from ..redact import redact, scrub_secrets
    ask, last_err = prompt, ""
    for n in range(1, attempts + 1):
        raw = await run_agent(agent, ask, settings, trace_path=trace_path, retry=True)
        try:
            data = extract_json(raw)
            empty = _empty_llm_reply(raw, data)
            if empty and validate:
                raise ValueError(f"the reply was an {empty}; fill in every field of the schema")
            return validate(data) if validate else data
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as e:
            last_err = str(e).splitlines()[0][:300] if isinstance(e, json.JSONDecodeError) else str(e)[:1500]
            if isinstance(e, json.JSONDecodeError) and _cut_off(raw):
                last_err += " - the reply looks cut off; keep it shorter (short step texts, no extra fields)"
            empty = _empty_llm_reply(raw, None if not (raw or "").strip() else ({} if (raw or "").strip() == "{}" else None))
            if on_attempt:
                try:
                    on_attempt(n, attempts, last_err, raw or "")
                except Exception:  # noqa: BLE001
                    pass
            elif empty or label == "planner":
                # Keep non-planner jobs quiet (old behaviour); planner empty/{} stalls must be visible.
                print(f"{label}: attempt {n}/{attempts} failed ({empty or last_err})")
            if raw_dir:
                from pathlib import Path
                Path(raw_dir).mkdir(parents=True, exist_ok=True)
                (Path(raw_dir) / f"{label}_raw_{n}.txt").write_text(scrub_secrets(redact(raw or "")))
            nudge = (" Return a JSON object with a non-empty cases array covering every requested id."
                     if empty or "empty object" in last_err or "no text" in last_err else "")
            ask = (f"{prompt}\n\nYour previous reply could not be used: {last_err}.{nudge}\n"
                   f"Previous reply:\n{(raw or '')[:20000]}\n\nReply again with ONLY the corrected JSON object, nothing else.")
    where = f" Raw replies are in {raw_dir}." if raw_dir else ""
    raise LLMOutputError(f"{label}: the model returned unusable JSON {attempts} times ({last_err}).{where}")


import contextvars

TRACE_CASE: contextvars.ContextVar[str | None] = contextvars.ContextVar("TRACE_CASE", default=None)


def _trace(path, ev) -> None:
    """Append a masked one-line summary of an ADK event to trace.jsonl (incl. LiteLLM usage when present)."""
    from datetime import datetime

    from ..redact import redact, scrub_secrets
    rec = {"at": datetime.now().isoformat(timespec="seconds"), "author": getattr(ev, "author", None)}
    if TRACE_CASE.get():
        rec["case_id"] = TRACE_CASE.get()           # the web UI groups steps by this, not by guessing the order
    parts = (ev.content.parts if ev.content and ev.content.parts else [])
    calls = [{"name": p.function_call.name, "args": dict(p.function_call.args or {})} for p in parts if getattr(p, "function_call", None)]
    resps = [p.function_response.name for p in parts if getattr(p, "function_response", None)]
    text = "".join(p.text or "" for p in parts if getattr(p, "text", None))
    if calls:
        rec["calls"] = calls
    if resps:
        rec["responses"] = resps
    if text:
        rec["text"] = text if len(text) <= 8000 else text[:8000] + " …[cut: over 8000 characters]"
    # LiteLLM / ADK usage (prompt + completion tokens) when the event carries it.
    usage = getattr(ev, "usage_metadata", None) or getattr(ev, "usage", None)
    if usage is None and getattr(ev, "content", None) is not None:
        usage = getattr(ev, "custom_metadata", None)
        if isinstance(usage, dict):
            usage = usage.get("usage")
    if usage is not None:
        if isinstance(usage, dict):
            rec["usage"] = {k: usage.get(k) for k in
                            ("prompt_tokens", "completion_tokens", "total_tokens",
                             "input_tokens", "output_tokens") if usage.get(k) is not None}
        else:
            rec["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_token_count", None) or getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "candidates_token_count", None) or getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_token_count", None) or getattr(usage, "total_tokens", None),
            }
    line = scrub_secrets(redact(json.dumps(rec, default=str)))
    with open(path, "a") as f:
        f.write(line + "\n")
