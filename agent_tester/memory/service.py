"""Memory service used by the run: preload, recall, corrections, proposals, staleness, known-bug matching.

It also implements the small get/put/mark_stale interface the gap resolver uses, so confirmed
gap answers land here as `resource` rows.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .embed import cosine
from .store import MemoryStore, public

KIND_ORDER = {"lesson": 0, "recipe": 1, "known_bug": 2, "open_bug_found": 3, "fact": 4, "resource": 5}


def _words(t: str) -> set[str]:
    stop = {"the", "and", "for", "with", "not", "but", "are", "was", "when", "that", "this", "from", "after", "shows"}
    return {w for w in re.findall(r"\$?[a-z0-9]+(?:\.[0-9]+)?", (t or "").lower()) if len(w) > 2 and w not in stop}


def jaccard(a: str, b: str) -> float:
    x, y = _words(a), _words(b)
    return len(x & y) / len(x | y) if x and y else 0.0


class Memory:
    def __init__(self, store: MemoryStore, pack_slug: str):
        self.store = store
        self.slug = pack_slug
        self.project = f"project:{pack_slug}"
        self.scopes = ["core", self.project]       # never load another project's memories

    # ---- gap resolver interface (resources) ---------------------------------------
    def get(self, scope: str, key: str) -> dict | None:
        rows = self.store.query([scope], ["resource"], key=key)
        if not rows:
            return None
        r = rows[0]
        return {"value": r["value_or_ref"], "source": r["source"], "secret_env": r["secret_env"], "id": r["id"]}

    def put(self, scope: str, key: str, value: str, source: str, *, secret_env: str | None = None) -> int:
        for r in self.store.query([scope], ["resource"], key=key, statuses=("active", "needs_recheck")):
            self.store.set_status(r["id"], "retired")
        return self.store.add("resource", scope, f"{key} = {value}", source, key=key, value_or_ref=value, secret_env=secret_env)

    def mark_stale(self, scope: str, key: str) -> None:
        for r in self.store.query([scope], ["resource"], key=key):
            self.store.set_status(r["id"], "needs_recheck")

    # ---- loading for prompts ------------------------------------------------------
    def preload(self, areas: list[str] | None = None, char_cap: int = 6000) -> list[dict]:
        """Active lessons, recipes and bugs for the case areas, capped (~1.5K tokens)."""
        rows = self.store.query(self.scopes, ["lesson", "recipe", "known_bug", "open_bug_found", "fact"], areas=areas)
        rows.sort(key=lambda r: (KIND_ORDER[r["kind"]], r["scope"] != self.project, -r["success_count"], -r["id"]))
        out, used = [], 0
        for r in rows:
            n = len(r["text"]) + 40
            if used + n > char_cap:
                break
            out.append(public(r))
            used += n
        return out

    def recall(self, query: str, k: int = 5) -> list[dict]:
        return [public(r) for r in self.store.search(self.scopes, query, k=k, kinds=["lesson", "recipe", "known_bug", "open_bug_found", "fact"])]

    @staticmethod
    def as_prompt(rows: list[dict]) -> str:
        if not rows:
            return "(no saved lessons yet)"
        return "\n".join(f"[{r['id']}] ({r['kind']}{', ' + r['area'] if r.get('area') else ''}) {r['text']}" for r in rows)

    # ---- writing ------------------------------------------------------------------
    def correction(self, case_id: str, given: str, correct: str, why: str, *, area: str | None = None,
                   run_id: str | None = None, linked_issue: int | None = None, general: bool = False) -> int:
        """User corrected a verdict: saved as an active lesson immediately (tech spec 7.3.1)."""
        text = f"{why.strip()} (verdict should be {correct}, not {given}; case {case_id})"
        return self.store.add("lesson", self.project, text, "user_correction", area=area,
                              applies_when="general" if general else None, linked_issue=linked_issue,
                              evidence=run_id)

    def propose(self, kind: str, text: str, *, area: str | None = None, applies_when: str | None = None,
                evidence: str | None = None, linked_issue: int | None = None) -> int:
        """Agent proposal: waits as `proposed` until the user approves it at the end of the run."""
        if kind not in ("lesson", "recipe", "fact"):
            raise ValueError("agents may only propose lessons, recipes or facts")
        dupes = [r for r in self.store.query(self.scopes, [kind], statuses=("active", "proposed")) if jaccard(r["text"], text) > 0.8]
        if dupes:
            return dupes[0]["id"]
        return self.store.add(kind, self.project, text, "agent_proposal", area=area, applies_when=applies_when,
                              evidence=evidence, linked_issue=linked_issue, status="proposed")

    def pending(self) -> list[dict]:
        return sorted((public(r) for r in self.store.query(self.scopes, statuses=("proposed",))), key=lambda r: r["id"])

    def approve(self, ids: list[int]) -> None:
        for i in ids:
            self.store.set_status(i, "active")

    def reject(self, ids: list[int]) -> None:
        for i in ids:
            self.store.set_status(i, "retired")

    def recipe_result(self, mid: int, ok: bool) -> dict:
        """A recipe that fails twice goes to needs_recheck (tech spec 7.5)."""
        r = self.store.bump(mid, ok)
        if not ok and r["failure_count"] >= 2 and r["status"] == "active":
            self.store.set_status(mid, "needs_recheck")
            r["status"] = "needs_recheck"
        return r

    def record_open_bugs(self, results: list, run_id: str, task_id: int) -> list[int]:
        """End of run: new FAILs (not matching a known bug) saved as open_bug_found."""
        ids = []
        for r in results:
            if r is None or r.verdict != "FAIL" or r.known_bug:
                continue
            text = f"{r.case_id} on #{task_id}: expected {r.expected}; actual {r.actual}".strip()
            ids.append(self.store.add("open_bug_found", self.project, text, "run_end", linked_issue=task_id, evidence=run_id))
        return ids

    def recheck_linked(self, issue_is_closed) -> list[int]:
        """Rows linked to a Redmine issue that is now closed: needs_recheck, so the agent re-verifies."""
        flagged = []
        for r in self.store.query(self.scopes, ["known_bug", "open_bug_found", "lesson", "recipe"]):
            if r["linked_issue"] and issue_is_closed(r["linked_issue"]):
                self.store.set_status(r["id"], "needs_recheck")
                flagged.append(r["id"])
        return flagged

    # ---- known-bug matching -------------------------------------------------------
    def known_bug_candidates(self, failure_text: str, pack_bugs: list[dict], k: int = 3, min_score: float = 0.12) -> list[dict]:
        """Code-side shortlist of known bugs that look like this failure; the judge makes the call."""
        cands = [{"n": b["n"], "text": b["text"], "source": "facts.md"} for b in pack_bugs]
        for r in self.store.query(self.scopes, ["known_bug", "open_bug_found"]):
            cands.append({"n": None, "memory_id": r["id"], "text": r["text"], "source": r["source"], "_vec": r["_vec"]})
        qv = self.store.embedder.embed(failure_text) if self.store.embedder else None
        scored = []
        for c in cands:
            s = cosine(qv, c.get("_vec")) if qv and c.get("_vec") else jaccard(failure_text, c["text"])
            scored.append((s, {k2: v for k2, v in c.items() if k2 != "_vec"}))
        scored.sort(key=lambda x: -x[0])
        return [dict(c, score=round(s, 3)) for s, c in scored[:k] if s >= min_score]

    # ---- migration ----------------------------------------------------------------
    def import_resources_json(self, path: Path) -> int:
        """Move phase 1 resources.json rows into the database once."""
        if not path.exists():
            return 0
        n = 0
        for row in json.loads(path.read_text()):
            if row.get("stale"):
                continue
            self.put(row["scope"], row["key"], row["value"], source=row.get("source", "user"), secret_env=row.get("secret_env"))
            n += 1
        path.rename(path.with_suffix(".json.imported"))
        return n
