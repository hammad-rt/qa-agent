"""Deterministic Redmine client: issue, test-case child task, journals, wiki pages."""
from __future__ import annotations

import re
import time
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
    via: str = "task"                 # feature mode: feature, subtask or related

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
        r, err = None, None
        for attempt in range(3):                       # 5xx / network hiccups: retry a couple of times
            try:
                r = self.s.get(f"{self.base}{path}", params=params, headers={"X-Redmine-API-Key": self.key},
                               timeout=self.timeout)
                err = None
            except requests.RequestException as e:
                r, err = None, e
            if r is not None and r.status_code < 500:
                break
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
        if r is None:
            raise RedmineError(f"couldn't reach Redmine at {self.base} ({type(err).__name__}). Check your connection or VPN and try again.")
        if r.status_code == 404:
            raise RedmineError(f"not found: {path}. Check the task number.")
        if r.status_code in (401, 403):
            raise RedmineError(f"Redmine refused access ({r.status_code}) on {path}. Check REDMINE_API_KEY and that you can see this issue.")
        if r.status_code >= 500:
            raise RedmineError(f"Redmine is not responding ({r.status_code}) on {path}, even after 3 tries. "
                               "It may be down for a moment; try again shortly, and check the task number.")
        if r.status_code >= 400:
            raise RedmineError(f"Redmine {r.status_code} on {path}")
        try:
            return r.json()
        except ValueError:
            raise RedmineError(f"Redmine sent something that isn't JSON for {path} (maybe a login or maintenance page).")

    def _post(self, path: str, body: dict) -> dict:
        if not self.key:
            raise RedmineError("REDMINE_API_KEY is not set")
        try:   # no automatic retry: a POST that timed out may already have been created
            r = self.s.post(f"{self.base}{path}", json=body, headers={"X-Redmine-API-Key": self.key}, timeout=self.timeout)
        except requests.RequestException as e:
            raise RedmineError(f"couldn't reach Redmine to save {path} ({type(e).__name__}). Check Redmine before "
                               "retrying, in case it was saved anyway.")
        if r.status_code in (401, 403):
            raise RedmineError(f"Redmine refused to save {path} ({r.status_code}). Check REDMINE_API_KEY permissions.")
        if r.status_code == 422:
            raise RedmineError(f"Redmine rejected the data for {path}: {r.text[:200]}")
        if r.status_code >= 400:
            raise RedmineError(f"Redmine {r.status_code} on saving {path}; nothing was saved. Try again shortly.")
        return r.json() if r.content else {}

    def create_issue(self, project_id: int, subject: str, description: str, parent_id: int | None = None,
                     relates_to: int | None = None) -> int:
        """Only called after an approved gate (bug drafts). Returns the new issue id."""
        body = {"project_id": project_id, "subject": subject, "description": description}
        if parent_id:
            body["parent_issue_id"] = parent_id
        new_id = self._post("/issues.json", {"issue": body})["issue"]["id"]
        if relates_to:
            try:
                self._post(f"/issues/{new_id}/relations.json", {"relation": {"issue_to_id": relates_to, "relation_type": "relates"}})
            except RedmineError:
                pass
        return new_id

    def project(self, project: str | int) -> dict:
        return self._get(f"/projects/{project}.json")["project"]

    def issues(self, project: str | int, limit: int = 50, subject_contains: str | None = None) -> list[dict]:
        params = {"project_id": project, "limit": limit, "status_id": "*", "sort": "updated_on:desc"}
        if subject_contains:
            params["subject"] = f"~{subject_contains}"
        return self._get("/issues.json", **params).get("issues", [])

    def wiki_index(self, project: str | int) -> list[str]:
        try:
            return [p["title"] for p in self._get(f"/projects/{project}/wiki/index.json").get("wiki_pages", [])]
        except RedmineError:
            return []

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


    def feature_tasks(self, issue_id: int, test_case_prefix: str = "Test Cases - ", max_depth: int = 3,
                      max_tasks: int = 30) -> list[TaskBundle]:
        """Feature mode: every work item under a feature, each with its own Test Cases task (if any).
        Walks subtasks (grandchildren included, up to max_depth) and "related to" tickets of the feature and its
        subtasks (one hop). A subtask that only groups other subtasks and has no Test Cases of its own is not
        planned itself; its children are. Test Cases tasks are never planned as work items."""
        prefix = test_case_prefix.strip()
        out, seen, queue = [], set(), [(issue_id, 0, "feature")]
        while queue and len(out) < max_tasks:
            iid, depth, why = queue.pop(0)
            if iid in seen:
                continue
            seen.add(iid)
            b = self.fetch_task(iid, test_case_prefix)
            if b.issue.get("subject", "").startswith(prefix) and iid != issue_id:
                continue
            kids = [c for c in b.issue.get("children", []) if not c.get("subject", "").startswith(prefix)]
            if depth < max_depth:
                queue += [(c["id"], depth + 1, "subtask") for c in kids]
            if depth <= 1:
                for r in b.issue.get("relations", []) or []:
                    if r.get("relation_type") == "relates":
                        other = r["issue_to_id"] if r.get("issue_id") == iid else r.get("issue_id")
                        if other:
                            queue.append((other, max_depth, "related"))   # related: no walking further down
            if b.test_case_task is not None or not kids:
                b.via = why
                out.append(b)
        return out


_URL = re.compile(r"https?://[^\s)\]|>\"']+")


def find_urls(text: str) -> list[str]:
    return list(dict.fromkeys(u.rstrip(".,;") for u in _URL.findall(text or "")))
