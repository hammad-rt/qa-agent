"""Settings from .env and the environment. Secrets are read by name, never logged."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def write_env_secret(name: str, value: str, path: Path | None = None) -> None:
    """Store a secret in .env (mode 600). Used after a masked prompt; the value is never printed."""
    path = path or ROOT / ".env"
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [l for l in lines if not l.startswith(f"{name}=")]
    lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    os.environ[name] = value


@dataclass
class Settings:
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_max_concurrency: int
    redmine_url: str
    redmine_api_key: str
    max_tool_calls_per_case: int
    max_minutes_per_case: int
    runs_dir: Path
    packs_dir: Path

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        e = os.environ.get
        return cls(
            llm_base_url=e("LLM_BASE_URL", "https://ai.rolustech.com/v1"),
            llm_model=e("LLM_MODEL", "gemma4"),
            llm_api_key=e("LLM_API_KEY", "none"),
            llm_max_concurrency=int(e("LLM_MAX_CONCURRENCY", "2")),
            redmine_url=e("REDMINE_URL", "https://redmine.rolustech.com").rstrip("/"),
            redmine_api_key=e("REDMINE_API_KEY", ""),
            max_tool_calls_per_case=int(e("MAX_TOOL_CALLS_PER_CASE", "40")),
            max_minutes_per_case=int(e("MAX_MINUTES_PER_CASE", "15")),
            runs_dir=Path(e("RUNS_DIR", str(ROOT / "runs"))),
            packs_dir=Path(e("PACKS_DIR", str(ROOT / "packs"))),
        )
