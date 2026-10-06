"""Gap resolution (RDP FR-40..46, tech spec section 16).

For every requirement: pack -> memory -> Redmine wiki -> task text and comments -> ask the user.
Found values are checked (URL in allowlist and reachable, secret present). Everything still open
is asked in ONE batch at the plan gate. Secrets go through a masked prompt into .env; memory keeps
only the env var name. Confirmed answers are saved as resource memory with their source.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from ..models import Requirement
from ..redact import redact
from .redmine import find_urls


@dataclass
class GapContext:
    pack: object                       # Pack
    env: str
    memory: object                     # ResourceMemory
    wiki_texts: dict[str, str]         # page title -> text (only URLs are ever taken from it)
    task_text: str
    url_checker: Callable[[str], bool] | None = None    # reachable?
    allow_hosts: list[str] | None = None


def scope(pack) -> str:
    return f"project:{pack.slug}"


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _in_allowlist(url: str, hosts: list[str]) -> bool:
    h = _host(url)
    return any(h == a or h.endswith("." + a) for a in hosts)


def _urls_near(text: str, words: list[str]) -> list[str]:
    """URLs on lines (or the line before) that mention one of the words."""
    lines = (text or "").splitlines()
    hits = []
    for i, line in enumerate(lines):
        window = " ".join(lines[max(0, i - 1): i + 1]).lower()
        if any(w.lower() in window for w in words if w):
            hits += find_urls(line)
    return list(dict.fromkeys(hits))


def _check_url(req: Requirement, url: str, ctx: GapContext) -> None:
    hosts = ctx.allow_hosts if ctx.allow_hosts is not None else ctx.pack.allowlist(ctx.env)
    if not _in_allowlist(url, hosts):
        req.status = "unverified"
        req.check_note = f"{_host(url)} is not in the {ctx.env} allowlist"
        return
    if ctx.url_checker and not ctx.url_checker(url):
        req.status = "failed_check"
        req.check_note = f"{url} did not respond"
        return
    req.status = "resolved"


def resolve_url(req: Requirement, app: str, ctx: GapContext) -> Requirement:
    sc = scope(ctx.pack)
    label = ctx.pack.apps.get(app, {}).get("label", app)

    url = ctx.pack.url(app, ctx.env)
    req.sources_checked.append("pack")
    if url:
        req.value, req.source = url, "pack"
        _check_url(req, url, ctx)
        if req.status == "resolved":
            return req

    req.sources_checked.append("memory")
    row = ctx.memory.get(sc, req.key)
    if row:
        req.value, req.source = row["value"], f"memory ({row['source']})"
        _check_url(req, row["value"], ctx)
        if req.status == "resolved":
            return req

    words = [ctx.env, app, label] + label.split()
    for title, text in ctx.wiki_texts.items():
        req.sources_checked.append(f"wiki:{title}")
        cands = _urls_near(text, [w for w in words if w and len(w) > 2])
        if cands:
            req.value, req.source = cands[0], f"wiki:{title}"
            req.status = "unverified"
            req.question = f"I found {label} {ctx.env} at {cands[0]} on the wiki page {title}. Use it? (yes / paste the right URL)"
            return req

    req.sources_checked.append("task")
    cands = _urls_near(ctx.task_text, [w for w in words if w and len(w) > 2])
    if cands:
        req.value, req.source = cands[0], "task"
        req.status = "unverified"
        req.question = f"The task mentions {cands[0]} for {label} {ctx.env}. Use it? (yes / paste the right URL)"
        return req

    if req.status == "missing" or not req.value:
        req.status = "missing"
        req.question = f"Where is {label} on {ctx.env}? I checked {', '.join(req.sources_checked)} and found nothing. (paste the URL)"
    elif req.status in ("unverified", "failed_check"):
        req.question = f"{label} {ctx.env} is {req.value} but {req.check_note}. Use it anyway? (yes / paste another URL)"
    return req


def resolve_account(req: Requirement, app: str, role: str, ctx: GapContext) -> Requirement:
    sc = scope(ctx.pack)
    acct = ctx.pack.accounts(app).get(role)
    req.sources_checked.append("pack")
    if not acct:
        row = ctx.memory.get(sc, req.key)
        req.sources_checked.append("memory")
        if row:
            acct = {"username": row["value"], "password_env": row.get("secret_env")}
            req.source = f"memory ({row['source']})"
    else:
        req.source = "pack"
    label = ctx.pack.apps.get(app, {}).get("label", app)
    if not acct:
        req.status = "missing"
        req.question = f"Which {role} account should I use on {label} ({ctx.env})? (username; I'll ask for the password in a masked prompt)"
        req.secret_env = f"{ctx.pack.slug.upper()}_{app.upper()}_{role.upper()}_PASSWORD"
        return req
    req.value = acct.get("username")
    req.secret_env = acct.get("password_env")
    req.must_have = req.must_have or list(acct.get("role_markers", []))
    if req.secret_env and os.environ.get(req.secret_env):
        req.status = "resolved"          # login and role are checked live by the tester's first step
        req.check_note = "login and role verified at the start of the run"
    else:
        req.status = "missing"
        req.question = f"I need the password for {req.value} on {label}. I'll ask in a masked prompt and keep it in .env as {req.secret_env}."
    return req


def resolve_other(req: Requirement, ctx: GapContext) -> Requirement:
    row = ctx.memory.get(scope(ctx.pack), req.key)
    req.sources_checked += ["pack", "memory"]
    if row:
        req.value, req.source, req.status = row["value"], f"memory ({row['source']})", "resolved"
        return req
    req.status = "missing"
    req.question = req.question or f"I need {req.key.split('.', 1)[-1].replace('_', ' ')} for this run. What should I use?"
    return req


def resolve(reqs: list[Requirement], ctx: GapContext) -> list[Requirement]:
    for r in reqs:
        parts = r.key.split(".")
        if parts[0] == "env" and len(parts) >= 3:
            r.kind = "url"
            resolve_url(r, parts[2], ctx)
        elif parts[0] == "account" and len(parts) >= 3:
            r.kind = "account"
            resolve_account(r, parts[1], parts[2], ctx)
        else:
            resolve_other(r, ctx)
    return reqs


def open_questions(reqs: list[Requirement]) -> list[Requirement]:
    return [r for r in reqs if r.status != "resolved"]


def apply_answers(reqs: list[Requirement], answers: dict[str, str], ctx: GapContext,
                  secret_prompt: Callable[[str], str] | None, write_secret: Callable[[str, str], None]) -> list[Requirement]:
    """answers: requirement key -> user's reply. Secrets are prompted separately and never seen here."""
    sc = scope(ctx.pack)
    for r in open_questions(reqs):
        a = (answers.get(r.key) or "").strip()
        if r.kind == "url":
            if a.lower() in ("y", "yes") and r.value:
                url = r.value
            elif find_urls(a):
                url = find_urls(a)[0]
            else:
                continue
            r.value, r.status, r.source = url, "resolved", f"user (was {r.source or 'none'})"
            if ctx.allow_hosts is not None and _host(url) not in ctx.allow_hosts:
                ctx.allow_hosts.append(_host(url))
            ctx.memory.put(sc, r.key, url, source=r.source)
        elif r.kind == "account":
            if a and not r.value:
                r.value = a
            if r.secret_env and not os.environ.get(r.secret_env) and secret_prompt:
                secret = secret_prompt(f"Password for {r.value} ({r.secret_env}): ")
                if secret:
                    write_secret(r.secret_env, secret)
            if r.value and r.secret_env and os.environ.get(r.secret_env):
                r.status, r.source = "resolved", r.source or "user"
                ctx.memory.put(sc, r.key, r.value, source="user", secret_env=r.secret_env)
        else:
            if a:
                r.value, r.status, r.source = redact(a), "resolved", "user"
                ctx.memory.put(sc, r.key, r.value, source="user")
    return reqs


def http_url_checker(timeout: int = 8) -> Callable[[str], bool]:
    import requests

    def check(url: str) -> bool:
        try:
            return requests.get(url, timeout=timeout, allow_redirects=True).status_code < 500
        except requests.RequestException:
            return False
    return check
