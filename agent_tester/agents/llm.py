"""Model factory and a small helper to run one ADK agent turn. ADK is imported lazily so the
deterministic core works (and is tested) without it."""
from __future__ import annotations

import asyncio
import json
import re
import uuid

from ..config import Settings

_sem: asyncio.Semaphore | None = None


def model(settings: Settings):
    from google.adk.models.lite_llm import LiteLlm
    return LiteLlm(model=f"openai/{settings.llm_model}", api_base=settings.llm_base_url, api_key=settings.llm_api_key)


async def run_agent(agent, prompt: str, settings: Settings, state: dict | None = None, app_name: str = "qa-agent") -> str:
    """Run one agent to completion and return its final text."""
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
            if ev.content and ev.content.parts:
                text = "".join(p.text or "" for p in ev.content.parts if getattr(p, "text", None))
                if text and ev.is_final_response():
                    final = text
    return final


def extract_json(text: str) -> dict:
    """Models sometimes wrap JSON in prose or fences; take the first JSON object."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(raw)
