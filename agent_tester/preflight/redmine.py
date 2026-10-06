"""Deterministic Redmine client: issue, test-case child task, journals, wiki pages."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import requests


class RedmineError(RuntimeError):
    pass


@dataclass
class TaskBundle:
    issue: dict
    test_case_task: dict | None
    parent: dict | None = None
    comments: list[str] = field(default_factory=list)

    @property
    def project_ids(self) -> list[int]:
        ids = [self.issue.get("project", {}).get("id")]
        if self.parent:
            ids.append(self.parent.get("project", {}).get("id"))
        if self.test_case_task:
            ids.append(self.test_case_task.get("project", {}).get("id"))
        return [i for i in ids if i]

    def all_text(self) -> str:
        parts = [self.issue.get("subject", ""), self.issue.get("description") or ""]
        if self.test_case_task:
            parts += [self.test_case_task.get("subject", ""), self.test_case_task.get("description") or ""]
        if self.parent:
            parts += [self.parent.get("description") or ""]
        parts += self.comments
        return "\n\n".join(parts)


class Redmine:
    def __init__(self, base_url: str, api_key: str, session: requests.Session | None = None, timeout: int = 20):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.s = session or requests.Session()
        self.timeout = timeout

    def _get(self, path: str, **params) -> dict:
        if not self.key:
            raise RedmineError("REDMINE_API_KEY is not set")
        r = self.s.get(f"{self.base}{path}", params=params, headers={"X-Redmine-API-Key": self.key}, timeout=self.timeout)
        if r.status_code == 404:
            raise RedmineError(f"not found: {path}")
        if r.status_code >= 400:
            raise RedmineError(f"Redmine {r.status_code} on {path}")
        return r.json()

    def issue(self, issue_id: int) -> dict:
        return self._get(f"/issues/{issue_id}.json", include="children,journals,attachments,relations")["issue"]

    def wiki_page(self, project: str | int, title: str) -> str | None:
        try:
            return self._get(f"/projects/{project}/wiki/{title}.json")["wiki_page"].get("text", "")
        except RedmineError:
            return None

    def fetch_task(self, issue_id: int, test_case_prefix: str = "Test Cases - ") -> TaskBundle:
        issue = self.issue(issue_id)
        parent = self.issue(issue["parent"]["id"]) if issue.get("parent") else None
        tc = None
        if issue.get("subject", "").startswith(test_case_prefix):
            tc = issue
        else:
            for child in issue.get("children", []):
                if child.get("subject", "").startswith(test_case_prefix):
                    tc = self.issue(child["id"])
                    break
        comments = []
        for src in (issue, tc, parent):
            if src:
                comments += [j["notes"] for j in src.get("journals", []) if j.get("notes")]
        return TaskBundle(issue=issue, test_case_task=tc if tc is not issue else None, parent=parent, comments=comments)


_URL = re.compile(r"https?://[^\s)\]|>\"']+")


def find_urls(text: str) -> list[str]:
    return list(dict.fromkeys(u.rstrip(".,;") for u in _URL.findall(text or "")))
