"""Ask the vision job's model about an image (OpenAI-compatible chat API: vLLM gemma4 or a local Ollama model)."""
from __future__ import annotations

import json
import re
import urllib.request

from .config import Settings, job_model


def ask(image_b64: str, question: str, settings: Settings | None = None, timeout: int = 90) -> str:
    settings = settings or Settings.load()
    base, name, key = job_model(settings, "vision")
    body = {"model": name, "temperature": 0, "max_tokens": 300, "messages": [{"role": "user", "content": [
        {"type": "text", "text": question},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}]}]}
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"] or ""


def tile_numbers(answer: str, n_tiles: int) -> list[int]:
    """Pull tile numbers (1..n) out of the model's answer; a JSON list is preferred, bare numbers accepted."""
    m = re.search(r"\[[^\]]*\]", answer or "")
    nums = re.findall(r"\d+", m.group(0) if m else (answer or ""))
    out = []
    for x in nums:
        i = int(x)
        if 1 <= i <= n_tiles and i not in out:
            out.append(i)
    return out
