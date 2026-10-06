"""Onboard a new project (phase 7, tech spec section 14): draft pack.yaml and facts.md from the project's
Redmine issues and wiki, ask one batch of questions, save only after approval.

Only addresses (scheme, host, port, path) are taken from Redmine text. Credentials in issues or the wiki
are never copied: accounts get a username from the user and a password env var name; the password itself
is asked through the masked prompt at the first plan gate."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..redact import redact

_URL = re.compile(r"https?://[^\s\"'<>()\[\]|]+")
_STAGING = re.compile(r"(?i)(staging|stage|stg|dev|test|qa|uat|sandbox|demo)")
_ENV_WIKI = re.compile(r"(?i)(staging|environment|env|server|credential|cardential|access|setup|url)")
PRIORITY_TO_SEVERITY = {"immediate": "Critical", "urgent": "Critical", "high": "High", "normal": "Medium", "low": "Low"}
SKIP_HOSTS = ("redmine.", "git.", "gitlab.", "github.com", "google.com", "docs.google", "drive.google", "fathom.",
              "zoom.us", "slack.com", "stripe.com", "youtube.com", "loom.com")


@dataclass
class Question:
    key: str
    text: str
    default: str = ""


@dataclass
class Draft:
    slug: str
    name: str
    project_id: int
    config: dict
    facts: str
    candidates: list[dict] = field(default_factory=list)   # [{"base": url, "count": n, "env": "staging"|"prod"}]
    questions: list[Question] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


def clean_url(u: str) -> str | None:
    p = urlparse(u.rstrip(".,;:"))
    if not p.hostname or any(s in p.hostname for s in SKIP_HOSTS):
        return None
    port = f":{p.port}" if p.port else ""
    return f"{p.scheme}://{p.hostname}{port}/"                # userinfo, path, query and fragment dropped


def find_addresses(texts: list[str], redmine_host: str = "") -> list[dict]:
    c = Counter()
    for t in texts:
        for u in _URL.findall(t or ""):
            base = clean_url(u)
            if base and (not redmine_host or redmine_host not in base):
                c[base] += 1
    return [{"base": b, "count": n, "env": "staging" if _STAGING.search(b) else "prod"} for b, n in c.most_common(12)]


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s if s[:1].isalpha() else f"p-{s}")[:40] or "project"


def resolve_project(rm, ref: str) -> int | str:
    ref = ref.strip().lstrip("#")
    if ref.isdigit():
        try:
            return rm.issue(int(ref))["project"]["id"]           # an issue id
        except Exception:  # noqa: BLE001 - fall back to a numeric project id
            return int(ref)
    return ref                                                 # a project identifier


def draft_pack(rm, ref: str, redmine_host: str = "") -> Draft:
    proj = rm.project(resolve_project(rm, ref))
    pid, name = proj["id"], proj.get("name", str(proj["id"]))
    slug = slugify(proj.get("identifier") or name)
    issues = rm.issues(pid, limit=50)
    texts, sources = [proj.get("description", "")], [f"project {name} (#{pid})"]
    texts += [f"{i.get('subject', '')}\n{i.get('description', '')}" for i in issues]
    sources.append(f"{len(issues)} recent issues")
    env_pages = [t for t in rm.wiki_index(pid) if _ENV_WIKI.search(t)][:5]
    for t in env_pages:
        texts.append(rm.wiki_page(pid, t) or "")
    if env_pages:
        sources.append("wiki: " + ", ".join(env_pages))
    cands = find_addresses(texts, redmine_host)
    tc_prefix = next((m.group(1) for i in issues if (m := re.match(r"^(Test Cases?\s*[-:]\s*)", i.get("subject", ""), re.I))),
                     "Test Cases - ")
    bugs = [i for i in issues if (i.get("tracker") or {}).get("name", "").lower() == "bug"
            and not (i.get("status") or {}).get("is_closed", False)
            and (i.get("status") or {}).get("name", "").lower() not in ("closed", "resolved", "rejected", "done")]
    known = [f"{n}. [{PRIORITY_TO_SEVERITY.get((b.get('priority') or {}).get('name', '').lower(), 'Medium')}] "
             f"{redact(b.get('subject', ''))} (Redmine #{b['id']})" for n, b in enumerate(bugs[:15], 1)]
    config = {
        "slug": slug, "name": name,
        "redmine": {"project_ids": [pid], "test_case_subject_prefix": tc_prefix, "wiki_project": None, "wiki_pages": env_pages},
        "apps": {}, "allowlist": {"staging": [], "prod": []},
        "preapprovable_actions": [], "always_ask_actions": ["change_settings", "redmine_post", "send_email", "prod_write"],
        "risky_patterns": ["(?i)\\bpay( now)?\\b", "(?i)\\bplace order\\b", "(?i)\\bsubmit\\b", "(?i)\\bconfirm\\b", "(?i)\\bdelete\\b"],
        "memory_areas": ["ui"], "helpers": {},
    }
    facts = "\n".join([
        f"# {name} project pack: facts", "", f"Drafted by onboard from: {'; '.join(sources)}. Review before relying on it.", "",
        "## Tracker", f"- Redmine project {pid}. Test cases: child tasks starting \"{tc_prefix}\".",
        *( [f"- Environment wiki pages: {', '.join(env_pages)}. They may hold plaintext secrets; never copy them into reports or memory."] if env_pages else []),
        "", "## Environments", "(filled from your answers)", "", "## Accounts (passwords never stored here)", "(filled from your answers)",
        "", "## Business rules", "(add as they are confirmed)", "", "## Known open bugs", *(known or ["(none found)"]), ""])
    qs = [Question(f"app:{c['base']}", f"Is {c['base']} ({c['env']}, seen {c['count']}x) an app to test? "
                                        f"Give it a short name like portal or crm, or leave blank to skip.") for c in cands]
    qs.append(Question("more_apps", "Any app address I missed? name=staging_url[,prod_url]; separate several with spaces."))
    return Draft(slug, name, pid, config, facts, cands, qs, sources)


def apply_answers(draft: Draft, answers: dict[str, str], account_answers: dict[str, str] | None = None) -> Draft:
    """answers: {"app:<url>": "portal", "more_apps": "crm=https://..,https://..", "prod:<app>": url}
    account_answers: {"<app>": "username[,role marker]"}. Builds apps, allowlist, env table, accounts."""
    cfg = draft.config
    apps: dict = {}
    for c in draft.candidates:
        nm = (answers.get(f"app:{c['base']}") or "").strip().lower()
        if nm:
            apps.setdefault(nm, {"label": nm.title(), "envs": {}, "accounts": {}})["envs"][c["env"]] = c["base"]
    for item in (answers.get("more_apps") or "").split():
        if "=" in item:
            nm, urls = item.split("=", 1)
            parts = [clean_url(u) for u in urls.split(",")]
            a = apps.setdefault(nm.strip().lower(), {"label": nm.strip().title(), "envs": {}, "accounts": {}})
            if parts and parts[0]:
                a["envs"]["staging"] = parts[0]
            if len(parts) > 1 and parts[1]:
                a["envs"]["prod"] = parts[1]
    for nm, a in apps.items():
        if answers.get(f"prod:{nm}"):
            u = clean_url(answers[f"prod:{nm}"])
            if u:
                a["envs"]["prod"] = u
        acc = (account_answers or {}).get(nm, "").strip()
        if acc:
            user, _, marker = acc.partition(",")
            env_name = re.sub(r"[^A-Z0-9]+", "_", f"{draft.slug}_{nm}_password".upper())
            a["accounts"]["tester"] = {"username": user.strip(), "password_env": env_name,
                                       **({"role_markers": [marker.strip()]} if marker.strip() else {})}
    cfg["apps"] = apps
    allow = {"staging": set(), "prod": set()}
    for a in apps.values():
        for env, u in a["envs"].items():
            allow.setdefault(env, set()).add(urlparse(u).hostname)
    cfg["allowlist"] = {k: sorted(v) for k, v in allow.items()}
    rows = ["| | Staging | Production |", "|---|---|---|"] + [
        f"| {a['label']} | {a['envs'].get('staging', '?')} | {a['envs'].get('prod', '?')} |" for a in apps.values()]
    accs = [f"- {a['label']}: `{d['username']}` (password in env {d['password_env']})"
            for a in apps.values() for d in a["accounts"].values()] or ["- None yet. Ask."]
    draft.facts = draft.facts.replace("## Environments\n(filled from your answers)", "## Environments\n" + "\n".join(rows))
    draft.facts = draft.facts.replace("## Accounts (passwords never stored here)\n(filled from your answers)",
                                      "## Accounts (passwords never stored here)\n" + "\n".join(accs))
    return draft


def save_draft(packs_dir, draft: Draft):
    from .packs import write_config
    dest = packs_dir / draft.slug
    if dest.exists():
        raise FileExistsError(f"{dest} already exists; pick another slug")
    (dest / "helpers").mkdir(parents=True)
    (dest / "evals" / "source").mkdir(parents=True)
    write_config(dest, draft.config)
    (dest / "facts.md").write_text(draft.facts)
    return dest
