"""Load a project pack and pick it from a Redmine project ID."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


class NoPackFound(LookupError):
    pass


@dataclass
class Pack:
    slug: str
    root: Path
    config: dict
    facts: str = ""
    known_bugs: list[dict] = field(default_factory=list)

    @property
    def apps(self) -> dict:
        return self.config.get("apps", {})

    def allowlist(self, env: str) -> list[str]:
        return list(self.config.get("allowlist", {}).get(env, []))

    def url(self, app: str, env: str) -> str | None:
        return self.apps.get(app, {}).get("envs", {}).get(env)

    def accounts(self, app: str) -> dict:
        return self.apps.get(app, {}).get("accounts", {})


def _parse_known_bugs(facts: str) -> list[dict]:
    import re
    bugs, in_section = [], False
    for line in facts.splitlines():
        if line.startswith("## "):
            in_section = "known" in line.lower() and "bug" in line.lower()
            continue
        m = re.match(r"\s*(\d+)\.\s*\[(\w+)\]\s*(.+)", line) if in_section else None
        if m:
            bugs.append({"n": int(m.group(1)), "severity": m.group(2), "text": m.group(3).strip()})
    return bugs


def load_pack(root: Path) -> Pack:
    cfg = yaml.safe_load((root / "pack.yaml").read_text())
    facts_path = root / "facts.md"
    facts = facts_path.read_text() if facts_path.exists() else ""
    return Pack(slug=cfg["slug"], root=root, config=cfg, facts=facts, known_bugs=_parse_known_bugs(facts))


def find_pack(packs_dir: Path, project_ids: list[int]) -> Pack:
    for p in sorted(packs_dir.iterdir()):
        if p.name.startswith("_") or not (p / "pack.yaml").exists():
            continue
        pack = load_pack(p)
        ids = set(pack.config.get("redmine", {}).get("project_ids", []))
        if ids & set(project_ids):
            return pack
    raise NoPackFound(f"No pack covers Redmine project(s) {project_ids}. Run `qa-agent onboard` to create one.")
