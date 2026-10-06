"""Create and validate project packs (phase 7). One pack per project; nothing project-specific lives in code."""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .pack_loader import Pack, load_pack

CORE_ALWAYS_ASK = {"change_settings", "redmine_post", "send_email", "prod_write"}
REQUIRED = ("slug", "name", "redmine", "apps", "allowlist", "always_ask_actions", "risky_patterns")
_PASSWORDISH = re.compile(r"(?i)^\s*(password|passwd|pwd|secret|api_?key|token)\s*:\s*['\"]?[^\s'\"#][^#\n]*$", re.M)
_SLUG = re.compile(r"^[a-z][a-z0-9-]{1,40}$")


def template_dir(packs_dir: Path) -> Path:
    return packs_dir / "_template"


def new_pack(packs_dir: Path, slug: str, name: str | None = None, project_ids: list[int] | None = None) -> Path:
    if not _SLUG.match(slug):
        raise ValueError("slug must be lower-case letters, digits and dashes")
    dest = packs_dir / slug
    if dest.exists():
        raise FileExistsError(f"{dest} already exists")
    shutil.copytree(template_dir(packs_dir), dest)
    for f in (dest / "pack.yaml", dest / "facts.md"):
        f.write_text(f.read_text().replace("__SLUG__", slug).replace("__NAME__", name or slug.title()))
    if project_ids:
        cfg = yaml.safe_load((dest / "pack.yaml").read_text())
        cfg["redmine"]["project_ids"] = project_ids
        write_config(dest, cfg)
    return dest


def write_config(dest: Path, cfg: dict) -> None:
    head = "# Project pack. No secrets here: passwords are referenced by env var name only.\n"
    (dest / "pack.yaml").write_text(head + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))


def validate(pack: Pack, packs_dir: Path | None = None) -> list[str]:
    """Problems that would make the pack unsafe or unusable. Empty list = valid."""
    cfg, probs = pack.config, []
    for k in REQUIRED:
        if k not in cfg:
            probs.append(f"missing key: {k}")
    if cfg.get("slug") != pack.root.name:
        probs.append(f"slug '{cfg.get('slug')}' does not match folder '{pack.root.name}'")
    ids = (cfg.get("redmine") or {}).get("project_ids") or []
    if not ids:
        probs.append("redmine.project_ids is empty, so no task will select this pack")
    raw = (pack.root / "pack.yaml").read_text()
    if _PASSWORDISH.search(raw):
        probs.append("pack.yaml looks like it contains a literal secret; use password_env names only")
    for app, a in (cfg.get("apps") or {}).items():
        for env, url in (a.get("envs") or {}).items():
            host = urlparse(url or "").hostname
            if url and not host:
                probs.append(f"{app}.{env}: not a URL: {url}")
            elif host and host not in (cfg.get("allowlist") or {}).get(env, []):
                probs.append(f"{app}.{env}: host {host} is not in allowlist.{env}")
        for acc, d in (a.get("accounts") or {}).items():
            if "password" in d:
                probs.append(f"{app}.accounts.{acc}: has a password field; use password_env")
            if d.get("password_env") and not re.match(r"^[A-Z][A-Z0-9_]+$", d["password_env"]):
                probs.append(f"{app}.accounts.{acc}: password_env must be an UPPER_CASE env var name")
    for p in cfg.get("risky_patterns") or []:
        try:
            re.compile(p)
        except re.error as e:
            probs.append(f"risky pattern does not compile: {p} ({e})")
    pre, always = set(cfg.get("preapprovable_actions") or []), set(cfg.get("always_ask_actions") or [])
    if pre & always:
        probs.append(f"actions both pre-approvable and always-ask: {', '.join(sorted(pre & always))}")
    if CORE_ALWAYS_ASK - always:
        probs.append(f"always_ask_actions must include: {', '.join(sorted(CORE_ALWAYS_ASK - always))}")
    if packs_dir:
        for other in sorted(packs_dir.iterdir()):
            if other == pack.root or other.name.startswith("_") or not (other / "pack.yaml").exists():
                continue
            o = load_pack(other)
            shared = set(ids) & set((o.config.get("redmine") or {}).get("project_ids") or [])
            if shared:
                probs.append(f"project id(s) {sorted(shared)} also claimed by pack '{o.slug}'")
    return probs
