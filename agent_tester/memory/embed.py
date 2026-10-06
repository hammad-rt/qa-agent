"""Embeddings through Ollama (nomic-embed-text). Optional: without it, memory falls back to keyword search."""
from __future__ import annotations

import math
import os
import struct

import requests


class Embedder:
    def __init__(self, base_url: str | None = None, model: str | None = None, timeout: int = 15):
        self.base = (base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
        self.model = model or os.environ.get("EMBED_MODEL", "nomic-embed-text")
        self.timeout = timeout
        self.available: bool | None = None

    def embed(self, text: str) -> list[float] | None:
        if self.available is False:
            return None
        try:
            r = requests.post(f"{self.base}/api/embeddings", json={"model": self.model, "prompt": text}, timeout=self.timeout)
            r.raise_for_status()
            v = r.json().get("embedding")
            self.available = bool(v)
            return v or None
        except (requests.RequestException, ValueError):
            self.available = False
            return None


def pack_vec(v: list[float] | None) -> bytes | None:
    return struct.pack(f"{len(v)}f", *v) if v else None


def unpack_vec(b: bytes | None) -> list[float] | None:
    return list(struct.unpack(f"{len(b) // 4}f", b)) if b else None


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
