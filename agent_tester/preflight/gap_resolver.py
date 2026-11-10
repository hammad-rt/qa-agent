"""Gap resolution (RDP FR-40..46, tech spec section 16).

For every requirement: pack -> Redmine wiki -> task text and comments -> ask the user.
Env values (URLs, logins, record ids) come from pack.yaml / .env / this run's answers — never from
lasting SQLite memory. A local gitignored prefs file may remember last-used account names as a
default for the ask-each-run prompt. Secrets go through a masked prompt into .env; lasting memory
may keep only the env var *name*.
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


def _prefs():
    from ..memory.prefs import RunPrefs
    return RunPrefs()


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
    label = ctx.pack.apps.get(app, {}).get("label", app)

    url = ctx.pack.url(app, ctx.env)
    req.sources_checked.append("pack")
    if url:
        req.value, req.source = url, "pack"
        _check_url(req, url, ctx)
        if req.status == "resolved":
            return req

    # URLs are never lasting memory — find them fresh from wiki / task / user each run.
    req.sources_checked.append("memory(skipped:env)")

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


ENVS = ("staging", "preprod", "prod", "local")
ENV_ALIASES = {"staging": "staging", "stage": "staging", "stg": "staging",
               "preprod": "preprod", "pre-prod": "preprod", "pre prod": "preprod", "pre-production": "preprod",
               "uat": "preprod", "prod": "prod", "production": "prod", "live": "prod",
               "local": "local", "localhost": "local", "dev": "local", "development": "local"}


def normalize_env(text: str | None) -> str | None:
    t = (text or "").strip().lower()
    if t in ("1", "2", "3", "4"):
        return ENVS[int(t) - 1]
    return ENV_ALIASES.get(t)


def password_env_for(base: str, username: str, env: str = "staging") -> str:
    """One stored password per user and environment, so switching users or environments
    never reuses another account's password."""
    slug = re.sub(r"[^A-Z0-9]+", "_", (username or "").upper()).strip("_")
    parts = [base] + ([env.upper()] if env != "staging" else []) + ([slug] if slug else [])
    return "__".join(parts)


def ask_accounts_each_run(pack) -> bool:
    return bool(pack.config.get("ask_accounts_each_run", True))


def resolve_account(req: Requirement, app: str, role: str, ctx: GapContext) -> Requirement:
    sc = scope(ctx.pack)
    prefs = _prefs()
    if ask_accounts_each_run(ctx.pack):
        acct = ctx.pack.accounts(app).get(role) or {}
        last = prefs.get(sc, f"{req.key}.{ctx.env}.last_used")
        label = ctx.pack.apps.get(app, {}).get("label", app)
        req.sources_checked += ["asked each run"]
        req.default = last or (acct.get("username") if ctx.env == "staging" else None)
        req.value, req.status = None, "missing"
        req.secret_env = acct.get("password_env") or f"{ctx.pack.slug.upper()}_{app.upper()}_{role.upper()}_PASSWORD"
        req.must_have = req.must_have or list(acct.get("role_markers", []))
        req.question = (f"Which {role} user should I use on {label} ({ctx.env})?"
                        + (f" [Enter = {req.default}]" if req.default else "")
                        + " I'll ask for the password in a masked prompt if I don't have it for that user.")
        return req
    acct = ctx.pack.accounts(app).get(role)
    req.sources_checked.append("pack")
    if not acct:
        # Pointer only: which secret_env name was used — username still comes from the user this run.
        row = ctx.memory.get(sc, req.key) if ctx.memory else None
        req.sources_checked.append("memory(pointer)")
        if row and row.get("secret_env"):
            req.secret_env = row["secret_env"]
            req.source = f"memory ({row['source']})"
    else:
        req.source = "pack"
    label = ctx.pack.apps.get(app, {}).get("label", app)
    if not acct:
        req.status = "missing"
        req.question = f"Which {role} account should I use on {label} ({ctx.env})? (username; I'll ask for the password in a masked prompt)"
        req.secret_env = req.secret_env or f"{ctx.pack.slug.upper()}_{app.upper()}_{role.upper()}_PASSWORD"
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
    """Non-URL/account gaps: prefer this run's prefs, never lasting env values in SQLite."""
    sc = scope(ctx.pack)
    prefs = _prefs()
    req.sources_checked += ["pack", "prefs"]
    val = prefs.get(sc, req.key)
    if val:
        req.value, req.source, req.status = val, "prefs", "resolved"
        return req
    req.status = "missing"
    req.question = req.question or f"I need {req.key.split('.', 1)[-1].replace('_', ' ')} for this run. What should I use?"
    return req


def ensure_account_requirements(plan, pack, env: str | None = None) -> list[str]:
    """Make sure every app the plan touches has its URL for this environment and (when accounts are
    asked each run) its accounts listed, even if the planner forgot them. Returns the keys it added."""
    env = env or plan.env
    have = {r.key for r in plan.requirements}
    text = " ".join([r.key for r in plan.requirements] + [
        f"{c.title} " + " ".join(f"{s.app} {s.text}" for s in c.steps) for c in plan.cases]).lower()
    added = []
    for app, cfg in pack.apps.items():
        names = {app.lower(), str(cfg.get("label", "")).lower().split(" (")[0]} - {""}
        if not any(n in text for n in names):
            continue
        if f"env.{env}.{app}" not in have:
            plan.requirements.append(Requirement(key=f"env.{env}.{app}", kind="url"))
            added.append(f"env.{env}.{app}")
        if not ask_accounts_each_run(pack):
            continue
        for role in cfg.get("accounts", {}):
            key = f"account.{app}.{role}"
            if key not in have:
                plan.requirements.append(Requirement(key=key, kind="account"))
                added.append(key)
    return added


def resolve(reqs: list[Requirement], ctx: GapContext) -> list[Requirement]:
    for r in reqs:
        if r.status == "resolved" and (r.source or "").startswith("user"):
            continue                     # already answered by the user in this run
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
            # URL stays on the plan for this run only — not lasting memory.
        elif r.kind == "account":
            if a:
                r.value = a
            elif not r.value and r.default:
                r.value = r.default
            if r.value and ask_accounts_each_run(ctx.pack):
                base = (r.secret_env or f"{ctx.pack.slug.upper()}_ACCOUNT_PASSWORD").split("__")[0]
                pack_user = (ctx.pack.accounts(r.key.split(".")[1]).get(r.key.split(".")[2]) or {}).get("username") \
                    if r.key.count(".") >= 2 else None
                # the pack's own env var always held the pack user's password, so that user keeps it
                pack_env = (ctx.pack.accounts(r.key.split(".")[1]).get(r.key.split(".")[2]) or {}).get("password_env") \
                    if r.key.count(".") >= 2 else None
                r.secret_env = ((pack_env or base) if r.value == pack_user and ctx.env == "staging"
                                else password_env_for(base, r.value, ctx.env))
                _prefs().put(sc, f"{r.key}.{ctx.env}.last_used", r.value)
            if r.secret_env and not os.environ.get(r.secret_env) and secret_prompt:
                secret = secret_prompt(f"Password for {r.value} ({r.secret_env}): ")
                if secret:
                    write_secret(r.secret_env, secret)
            if r.value and r.secret_env and os.environ.get(r.secret_env):
                r.status, r.source = "resolved", r.source or "user"
                if not ask_accounts_each_run(ctx.pack):
                    # Lasting memory: secret env var name only (no username/password value).
                    ctx.memory.put(sc, r.key, "", source="user", secret_env=r.secret_env)
        else:
            if a:
                r.value, r.status, r.source = redact(a), "resolved", "user"
                # data.* and similar stay in local prefs for this machine, not lasting SQLite facts.
                _prefs().put(sc, r.key, r.value)
    return reqs


def http_url_checker(timeout: int = 8) -> Callable[[str], bool]:
    import requests

    def check(url: str) -> bool:
        try:
            return requests.get(url, timeout=timeout, allow_redirects=True).status_code < 500
        except requests.RequestException:
            return False
    return check
