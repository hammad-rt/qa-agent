"""Gemma sometimes writes its tool call as text (`<|tool_call>call:name({k:<|"|>v<|"|>})<tool_call|>`)
instead of a real function call (vLLM's parser misses it). Turn that text back into function calls."""
from __future__ import annotations

import re
import uuid

Q = '<|"|>'
CALL = re.compile(r"call:(\w+)\(")


class _P:
    def __init__(self, s): self.s, self.i = s, 0

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in " \n\t,": self.i += 1

    def value(self):
        self.ws(); s = self.s
        if s.startswith(Q, self.i):
            j = s.index(Q, self.i + len(Q)); v = s[self.i + len(Q):j]; self.i = j + len(Q); return v
        if s[self.i] == "{": return self.obj()
        if s[self.i] == "[":
            self.i += 1; out = []
            while True:
                self.ws()
                if s[self.i] == "]": self.i += 1; return out
                out.append(self.value())
        m = re.compile(r"[^,}\])]+").match(s, self.i); tok = m.group(0).strip(); self.i = m.end()
        if tok in ("true", "false"): return tok == "true"
        if tok in ("null", "None"): return None
        try: return int(tok)
        except ValueError:
            try: return float(tok)
            except ValueError: return tok.strip('"')

    def obj(self):
        self.i += 1; out = {}
        while True:
            self.ws()
            if self.s[self.i] == "}": self.i += 1; return out
            m = re.compile(r'"?(\w+)"?\s*:').match(self.s, self.i)
            if not m: raise ValueError("bad key")
            self.i = m.end(); out[m.group(1)] = self.value()


def parse(text: str) -> list[tuple[str, dict]]:
    calls = []
    for m in CALL.finditer(text or ""):
        try:
            p = _P(text); p.i = m.end(); p.ws()
            args = p.obj() if p.s[p.i] == "{" else {}
            calls.append((m.group(1), args))
        except (ValueError, IndexError):
            continue
    return calls


def after_model(callback_context, llm_response):
    """ADK after_model_callback: replace a text-only response holding raw call syntax by real calls."""
    from google.genai import types
    c = getattr(llm_response, "content", None)
    if not c or not c.parts or any(getattr(p, "function_call", None) for p in c.parts):
        return None
    text = "".join(p.text or "" for p in c.parts if getattr(p, "text", None))
    if "call:" not in text or ("tool_call" not in text and Q not in text):
        return None
    calls = parse(text)
    if not calls:
        return None
    c.parts = [types.Part(function_call=types.FunctionCall(id=f"raw-{uuid.uuid4().hex[:8]}", name=n, args=a)) for n, a in calls]
    return llm_response
