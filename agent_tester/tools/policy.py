"""Action policy enforced in code: domain allowlist, risky actions, approvals, run pre-approvals."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse


class PolicyViolation(PermissionError):
    pass


@dataclass
class Policy:
    allow_hosts: list[str]
    risky_patterns: list[str]
    preapprovable: list[str]
    always_ask: list[str]
    preapproved: set[str] = field(default_factory=set)
    env: str = "staging"
    approver: Callable[[str, str, dict], bool] | None = None   # (action, reason, details) -> approved?
    log: list[dict] = field(default_factory=list)
    auto: bool = False               # automatic mode: no approval prompts (staging runs; prod writes skipped)
    read_only: bool = False          # baseline/final reads and preflight: anything needing approval is refused

    def can_preapprove(self, action_type: str) -> bool:
        if self.env == "prod" or action_type in self.always_ask or action_type not in self.preapprovable:
            return False
        return not ("staging" in action_type and self.env != "staging")

    def preapprove(self, action_types: list[str]) -> None:
        bad = [a for a in action_types if not self.can_preapprove(a)]
        if bad:
            raise PolicyViolation(f"cannot pre-approve: {bad}")
        self.preapproved |= set(action_types)

    def allow_host(self, host: str) -> None:
        """Called only after the user confirms a URL found outside the pack."""
        if host not in self.allow_hosts:
            self.allow_hosts.append(host)

    def check_url(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return
        if not any(host == h or host.endswith("." + h) for h in self.allow_hosts):
            raise PolicyViolation(f"domain not allowed: {host}")

    def is_risky(self, label: str) -> bool:
        return any(re.search(p, label or "") for p in self.risky_patterns)

    def gate(self, action: str, reason: str, *, action_type: str | None = None,
             risky: bool = False, details: dict | None = None) -> str:
        """Return 'not_needed' | 'pre_approved' | 'approved'; raise if denied."""
        details = details or {}
        if self.env == "prod" and (risky or action_type):
            action_type = action_type or "prod_write"
        needs = risky or bool(action_type)
        if not needs:
            return "not_needed"
        if self.read_only:
            self.log.append({"action": action, "type": action_type, "approval": "denied", "why": "read-only"})
            raise PolicyViolation(f"read-only step, not allowed: {action}")
        if self.auto and action_type not in ("redmine_post", "send_email"):
            if self.env == "prod":
                self.log.append({"action": action, "type": action_type, "approval": "denied", "why": "prod write skipped (automatic mode)"})
                raise PolicyViolation(f"production: data-changing step skipped in automatic mode: {action}")
            self.log.append({"action": action, "type": action_type, "approval": "auto"})
            return "approved"
        if action_type and action_type in self.preapproved and self.can_preapprove(action_type):
            self.log.append({"action": action, "type": action_type, "approval": "pre_approved"})
            return "pre_approved"
        if self.approver is None:
            self.log.append({"action": action, "type": action_type, "approval": "denied", "why": "no approver"})
            raise PolicyViolation(f"approval needed and nobody to ask: {action}")
        ok = self.approver(action, reason, details)
        self.log.append({"action": action, "type": action_type, "approval": "approved" if ok else "denied"})
        if not ok:
            raise PolicyViolation(f"denied by user: {action}")
        return "approved"


def from_pack(pack, env: str, approver=None, auto: bool | None = None) -> Policy:
    from ..auto import is_auto
    c = pack.config
    return Policy(
        allow_hosts=list(c.get("allowlist", {}).get(env, [])),
        risky_patterns=list(c.get("risky_patterns", [])),
        preapprovable=list(c.get("preapprovable_actions", [])),
        always_ask=list(c.get("always_ask_actions", [])),
        env=env,
        approver=approver,
        auto=is_auto() if auto is None else auto,
    )
