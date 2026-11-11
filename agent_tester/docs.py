"""Stack documentation on demand: packs/<slug>/docs/<app>.md caches excerpts found for earlier questions.

pack.yaml lists each app's stack, e.g.
  stack:
    crm: {name: "SuiteCRM 7", docs: ["https://docs.suitecrm.com"]}
    portal: {name: "Next.js", docs: ["https://nextjs.org/docs"]}
Nothing is fetched until a tester asks; useful excerpts are kept so the next run reads them offline."""
from __future__ import annotations

import logging
import html as _html
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

log = logging.getLogger(__name__)

TIMEOUT = 15


def _words(s: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(w) > 2}


def stack_for(pack, app: str) -> dict:
    st = ((getattr(pack, "config", None) or {}).get("stack") or {}).get(app) or {}
    return st if isinstance(st, dict) else {"name": str(st), "docs": []}


def cache_path(pack, app: str) -> Path | None:
    root = getattr(pack, "root", None)
    return Path(root) / "docs" / f"{app}.md" if root else None


def _cached(pack, app: str, question: str) -> list[dict]:
    p = cache_path(pack, app)
    if not p or not p.is_file():
        return []
    want, out = _words(question), []
    for block in p.read_text().split("\n## ")[1:]:
        head, _, body = block.partition("\n")
        score = len(want & _words(head + " " + body[:600]))
        if score >= max(2, len(want) // 3):
            out.append((score, {"source": "cache", "question": head.strip(), "excerpt": body.strip()[:1500]}))
    return [x for _, x in sorted(out, key=lambda t: -t[0])[:2]]


def _text(page_html: str) -> str:
    t = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", page_html)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"[ \t]+", " ", _html.unescape(t))


def _best_paragraphs(text: str, question: str, limit: int = 1500) -> str:
    want = _words(question)
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if len(p.strip()) > 40]
    ranked = sorted(paras, key=lambda p: -len(want & _words(p)))
    out, n = [], 0
    for p in ranked[:6]:
        if not want & _words(p):
            break
        out.append(p)
        n += len(p)
        if n > limit:
            break
    return "\n".join(out)[:limit]


def _search(question: str, domains: list[str], get) -> list[str]:
    site = " OR ".join(f"site:{urlparse(d).netloc}{urlparse(d).path.rstrip('/')}" for d in domains)
    r = get("https://html.duckduckgo.com/html/", params={"q": f"{question} {site}"})
    links = []
    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', r.text):
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
        if any(urlparse(href).netloc == urlparse(d).netloc for d in domains) and href not in links:
            links.append(href)
    return links[:3]


def lookup(pack, app: str, question: str, get=None) -> dict:
    question = (question or "").strip()[:200]
    if not question:
        return {"error": "ask a question, e.g. 'SuiteCRM admin settings save validation'"}
    hits = _cached(pack, app, question)
    if hits:
        return {"results": hits}
    st = stack_for(pack, app)
    domains = [d for d in st.get("docs") or [] if d.startswith("http")]
    if not domains:
        return {"error": f"pack.yaml has no stack docs for {app}; decide from the page"}
    if get is None:
        import requests
        sess = requests.Session()
        sess.headers["User-Agent"] = "Mozilla/5.0 qa-agent docs lookup"
        get = lambda url, params=None: sess.get(url, params=params, timeout=TIMEOUT)  # noqa: E731
    try:
        links = _search(f"{st.get('name', '')} {question}", domains, get)
    except Exception as e:  # noqa: BLE001
        return {"error": f"docs search failed: {type(e).__name__}"}
    results = []
    for url in links:
        try:
            ex = _best_paragraphs(_text(get(url).text), question)
        except Exception:  # noqa: BLE001
            log.debug("docs fetch/parse for a URL failed", exc_info=True)
            continue
        if ex:
            results.append({"source": url, "excerpt": ex})
        if len(results) == 2:
            break
    if not results:
        return {"results": [], "note": "nothing relevant in the docs; decide from the page"}
    p = cache_path(pack, app)
    if p:
        p.parent.mkdir(parents=True, exist_ok=True)
        old = p.read_text() if p.is_file() else f"# {st.get('name', app)} docs excerpts\n\nCached by the agent; delete freely.\n"
        r0 = results[0]
        p.write_text(old.rstrip("\n") + f"\n\n## {question} ({date.today().isoformat()})\nSource: {r0['source']}\n\n{r0['excerpt']}\n")
    return {"results": results}
