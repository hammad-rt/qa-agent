"""Stuck lookup ladder: memory → code → platform docs → web → (caller) explore → ask_user.

Providers are optional and off when unset. Fetched text is untrusted: short snippets only,
cached by query, kept inside the memory token budget. Never approves or changes data.
"""
from __future__ import annotations

import logging
import hashlib
import os
import re
import time
from pathlib import Path
from urllib.parse import quote_plus, urlencode

log = logging.getLogger(__name__)

# Caps (also documented in .env.example)
DEFAULT_TIMEOUT = float(os.environ.get("STUCK_LOOKUP_TIMEOUT_SEC", "8"))
DEFAULT_MAX_SNIPPETS = int(os.environ.get("STUCK_LOOKUP_MAX_SNIPPETS", "3"))
SNIPPET_CHARS = 220
# Stay under MEMORY_TURN_TOKEN_BUDGET (~1200 tokens ≈ 4800 chars).
from .memory.retrieve import DEFAULT_TOKEN_BUDGET, tokens_of  # noqa: E402

_CACHE: dict[str, dict] = {}


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def code_root_for(pack) -> Path | None:
    cfg = (getattr(pack, "config", None) or {}) if pack is not None else {}
    raw = cfg.get("code_root") or _env("QA_CODE_ROOT") or _env("CODE_ROOT")
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_dir() else None


def build_query(*, goal: str = "", page: str = "", tried: list[str] | None = None,
                area: str = "", app: str = "") -> str:
    parts = [goal, area, app, page[:400]]
    if tried:
        parts.append("tried: " + "; ".join(tried[:6]))
    q = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
    return q[:300]


def _cache_key(step: str, query: str) -> str:
    return hashlib.sha1(f"{step}|{query}".encode()).hexdigest()[:20]


def _trim(text: str, n: int = SNIPPET_CHARS) -> str:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def search_code(root: Path, query: str, *, limit: int = 3, deadline: float | None = None) -> list[dict]:
    """Read-only Python walk + regex; return short file:line snippets. No ripgrep."""
    terms = [t for t in re.findall(r"[a-zA-Z0-9_]{3,}", query) if t.lower() not in {
        "the", "and", "for", "with", "tried", "page", "goal", "from", "this",
    }][:8]
    if not terms or not root.is_dir():
        return []
    pat = re.compile("|".join(re.escape(t) for t in terms), re.I)
    skip_dirs = {".git", "node_modules", "vendor", ".venv", "venv", "__pycache__", "dist", "build"}
    hits: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if deadline and time.monotonic() > deadline:
            break
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for name in filenames:
            if deadline and time.monotonic() > deadline:
                break
            if not name.endswith((".php", ".js", ".ts", ".tsx", ".py", ".tpl", ".html", ".twig", ".json", ".md")):
                continue
            path = Path(dirpath) / name
            try:
                if path.stat().st_size > 400_000:
                    continue
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if pat.search(line):
                    rel = str(path.relative_to(root))
                    hits.append({
                        "source": "code",
                        "path": f"{rel}:{i}",
                        "excerpt": _trim(line),
                        "trust": "untrusted",
                    })
                    if len(hits) >= limit:
                        return hits
                    break
    return hits


def _http_get(url: str, *, timeout: float = 5.0):
    import requests
    return requests.get(url, timeout=timeout, headers={"User-Agent": "qa-agent-stuck-lookup/1.0"})


def search_docs_provider(query: str, *, site: str | None = None, limit: int = 3,
                         deadline: float | None = None) -> list[dict]:
    """Optional SearXNG-style or Context7 HTTP search. Off when not configured."""
    if deadline and time.monotonic() > deadline:
        return []
    out: list[dict] = []
    searx = _env("DOCS_SEARCH_URL")
    if searx:
        q = query if not site else f"{query} site:{site}"
        sep = "&" if "?" in searx else "?"
        url = f"{searx}{sep}{urlencode({'q': q, 'format': 'json'})}"
        try:
            r = _http_get(url, timeout=min(5.0, max(1.0, (deadline or (time.monotonic() + 5)) - time.monotonic())))
            data = r.json() if "json" in (r.headers.get("content-type") or "") or r.text.lstrip().startswith("{") else {}
            results = data.get("results") or data.get("items") or []
            for item in results[:limit]:
                title = item.get("title") or ""
                content = item.get("content") or item.get("snippet") or item.get("description") or ""
                link = item.get("url") or item.get("link") or ""
                out.append({
                    "source": "docs" if site else "web",
                    "path": link or title,
                    "excerpt": _trim(f"{title}: {content}" if title else content),
                    "trust": "untrusted",
                })
        except Exception:  # noqa: BLE001
            log.debug("stuck-lookup docs/web search failed", exc_info=True)
        if out:
            return out[:limit]

    c7 = _env("CONTEXT7_API_URL")
    if c7:
        key = _env("CONTEXT7_API_KEY")
        url = c7.rstrip("/") + "/search?q=" + quote_plus(query[:200])
        headers = {"User-Agent": "qa-agent-stuck-lookup/1.0"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            import requests
            r = requests.get(url, timeout=5, headers=headers)
            data = r.json()
            for item in (data.get("results") or data.get("snippets") or [])[:limit]:
                out.append({
                    "source": "docs",
                    "path": item.get("url") or item.get("id") or "context7",
                    "excerpt": _trim(item.get("text") or item.get("content") or str(item)),
                    "trust": "untrusted",
                })
        except Exception:  # noqa: BLE001
            log.debug("stuck-lookup context7 search failed", exc_info=True)
    return out[:limit]


def platform_docs_site(platform_id: str | None, packs_dir: Path | None = None) -> str | None:
    if not platform_id:
        return None
    from .config import ROOT
    root = packs_dir or (ROOT / "packs" / "_platforms")
    yml = root / platform_id / "platform.yaml"
    if not yml.is_file():
        return None
    try:
        import yaml
        cfg = yaml.safe_load(yml.read_text()) or {}
        docs = cfg.get("docs") or []
        if docs:
            from urllib.parse import urlparse
            return urlparse(docs[0]).netloc or None
    except Exception:  # noqa: BLE001
        log.debug("failed to read platform docs site from pack yaml", exc_info=True)
        return None
    return None


def run_ladder(*, query: str, memory=None, pack=None, platform_id: str | None = None,
               area: str | None = None, state=None, max_snippets: int | None = None,
               timeout_sec: float | None = None, packs_dir: Path | None = None,
               save_unconfirmed: bool = True) -> dict:
    """Run memory → code → platform docs → web. Explore/ask_user stay with the caller.

    Returns {snippets, steps_tried, chars, tokens, cached}.
    """
    max_snippets = max_snippets or DEFAULT_MAX_SNIPPETS
    timeout_sec = timeout_sec if timeout_sec is not None else DEFAULT_TIMEOUT
    deadline = time.monotonic() + max(1.0, timeout_sec)
    q = (query or "").strip()
    ck = _cache_key("ladder", q + "|" + (platform_id or ""))
    if ck in _CACHE:
        cached = dict(_CACHE[ck])
        cached["cached"] = True
        return cached

    snippets: list[dict] = []
    steps: list[str] = []
    budget_chars = DEFAULT_TOKEN_BUDGET * 4

    def _room() -> int:
        used = sum(len(s.get("excerpt") or "") for s in snippets)
        return max(0, budget_chars - used)

    # 1) Memory
    steps.append("memory")
    if memory is not None and q and _room() and time.monotonic() < deadline:
        try:
            rows = memory.recall(q, k=max_snippets, area=area, state=state)
            for r in rows[:max_snippets]:
                snippets.append({
                    "source": "memory",
                    "path": f"memory:{r.get('id')}",
                    "excerpt": _trim(r.get("text") or ""),
                    "trust": r.get("trust") or "hint",
                    "scope": r.get("scope"),
                })
                if len(snippets) >= max_snippets or _room() <= 0:
                    break
        except Exception:  # noqa: BLE001
            log.debug("stuck-lookup memory recall failed", exc_info=True)

    # 2) Code (optional)
    steps.append("code")
    root = code_root_for(pack)
    if root and len(snippets) < max_snippets and _room() and time.monotonic() < deadline:
        code_hits = search_code(root, q, limit=max_snippets - len(snippets), deadline=deadline)
        snippets.extend(code_hits)
        if save_unconfirmed and memory is not None and code_hits:
            _save_unconfirmed(memory, code_hits, scope_kind="project", platform_id=platform_id)

    # 3) Platform docs (only if detected + provider configured)
    steps.append("platform_docs")
    if platform_id and len(snippets) < max_snippets and _room() and time.monotonic() < deadline:
        if _env("DOCS_SEARCH_URL") or _env("CONTEXT7_API_URL"):
            site = platform_docs_site(platform_id, packs_dir=packs_dir)
            docs_hits = search_docs_provider(
                q, site=site, limit=max_snippets - len(snippets), deadline=deadline,
            )
            for h in docs_hits:
                h["source"] = "platform_docs"
            snippets.extend(docs_hits)
            if save_unconfirmed and memory is not None and docs_hits:
                _save_unconfirmed(memory, docs_hits, scope_kind="platform", platform_id=platform_id)

    # 4) Wider web (provider only)
    steps.append("web")
    if len(snippets) < max_snippets and _room() and time.monotonic() < deadline:
        if _env("DOCS_SEARCH_URL") or _env("CONTEXT7_API_URL"):
            web_hits = search_docs_provider(
                q, site=None, limit=max_snippets - len(snippets), deadline=deadline,
            )
            for h in web_hits:
                h["source"] = "web"
            snippets.extend(web_hits)
            if save_unconfirmed and memory is not None and web_hits and platform_id:
                _save_unconfirmed(memory, web_hits, scope_kind="platform", platform_id=platform_id)

    snippets = snippets[:max_snippets]
    prompt = format_snippets(snippets)
    # Enforce token budget
    while tokens_of(prompt) > DEFAULT_TOKEN_BUDGET and snippets:
        snippets.pop()
        prompt = format_snippets(snippets)

    result = {
        "snippets": snippets,
        "steps_tried": steps,
        "chars": len(prompt),
        "tokens": tokens_of(prompt),
        "prompt": prompt,
        "cached": False,
        "next": "explore",
    }
    _CACHE[ck] = {k: v for k, v in result.items() if k != "cached"}
    return result


def format_snippets(snippets: list[dict]) -> str:
    if not snippets:
        return ""
    lines = ["Untrusted stuck-lookup hints (verify on the live page; do not change data from these):"]
    for s in snippets:
        lines.append(f"- ({s.get('source')}, {s.get('path')}) {s.get('excerpt')}")
    return "\n".join(lines)


def _save_unconfirmed(memory, hits: list[dict], *, scope_kind: str, platform_id: str | None) -> None:
    """Save code/docs/web hits as proposed lessons at the right scope."""
    from .memory.env_guard import EnvDetailError, assert_safe_lasting, scrub_env_details
    from .memory.layers import platform_scope

    if scope_kind == "platform":
        scope = platform_scope(platform_id)
        if not scope:
            return
    else:
        scope = memory.project

    for h in hits[:2]:
        text = f"{h.get('excerpt')} (from {h.get('path')})"
        try:
            text = assert_safe_lasting(text, context="stuck_lookup")
        except EnvDetailError:
            try:
                text = assert_safe_lasting(scrub_env_details(text), context="stuck_lookup")
            except EnvDetailError:
                continue
        try:
            # Prefer Memory.propose for project; for platform write via store directly.
            if scope_kind == "project":
                memory.propose("lesson", text, evidence=str(h.get("path") or "stuck"))
            else:
                memory.store.add(
                    "lesson", scope, text, f"stuck_{h.get('source')}",
                    status="proposed", confirm_count=1, evidence=str(h.get("path") or ""),
                )
        except Exception:  # noqa: BLE001
            log.warning("failed to save unconfirmed stuck-lookup lesson to memory", exc_info=True)
            continue


def clear_cache() -> None:
    _CACHE.clear()
