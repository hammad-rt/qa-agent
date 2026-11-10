"""Memory service: lean preload/recall, corrections, proposals, trust, known-bug matching.

SQLite is the source of truth for lasting memory (fact / lesson / recipe / bugs).
learned.md holds only pack-local navigation notes (routes / stuck / discovery), scrubbed.
Env values (URLs, logins, record ids) are never lasting — see prefs.py and env_guard.py.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .embed import cosine
from .env_guard import EnvDetailError, assert_safe_lasting, is_env_resource_key
from .store import MemoryStore, public

KIND_ORDER = {"lesson": 0, "recipe": 1, "known_bug": 2, "open_bug_found": 3, "fact": 4, "resource": 5}
STALE_DAYS = int(os.environ.get("MEMORY_STALE_DAYS", "30"))
# Legacy default; turn injection uses retrieve.TurnRetriever with MEMORY_TURN_TOKEN_BUDGET.
PRELOAD_CHAR_CAP = int(os.environ.get("MEMORY_PRELOAD_CHAR_CAP", "4000"))


def _words(t: str) -> set[str]:
    stop = {"the", "and", "for", "with", "not", "but", "are", "was", "when", "that", "this", "from", "after", "shows"}
    return {w for w in re.findall(r"\$?[a-z0-9]+(?:\.[0-9]+)?", (t or "").lower()) if len(w) > 2 and w not in stop}


def jaccard(a: str, b: str) -> float:
    x, y = _words(a), _words(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class Memory:
    def __init__(self, store: MemoryStore, pack_slug: str):
        self.store = store
        self.slug = pack_slug
        self.project = f"project:{pack_slug}"
        self.scopes = ["core", self.project]       # never load another project's memories
        self._retriever = None

    def retriever(self, state=None, token_budget: int | None = None):
        from .retrieve import TurnRetriever
        if state is not None:
            return TurnRetriever(self, token_budget=token_budget, state=state)
        if self._retriever is None:
            self._retriever = TurnRetriever(self, token_budget=token_budget)
        return self._retriever

    # ---- gap resolver interface (resources = env-var pointers only) ---------------
    def get(self, scope: str, key: str) -> dict | None:
        """Return a resource pointer if present. value is never an env detail from lasting memory."""
        rows = self.store.query([scope], ["resource"], key=key)
        if not rows:
            return None
        r = rows[0]
        # Lasting resources no longer carry values; callers must use pack/prefs/run settings.
        return {"value": None, "source": r["source"], "secret_env": r["secret_env"], "id": r["id"],
                "key": r.get("key")}

    def put(self, scope: str, key: str, value: str, source: str, *, secret_env: str | None = None) -> int:
        """Record that a gap key exists / which secret env var holds it. Never stores the value."""
        for r in self.store.query([scope], ["resource"], key=key, statuses=("active", "needs_recheck")):
            self.store.set_status(r["id"], "retired")
        # Deliberately drop value — env details come from run settings each run.
        _ = value
        return self.store.add(
            "resource", scope, f"{key} (see run settings)", source,
            key=key, value_or_ref=None, secret_env=secret_env,
        )

    def mark_stale(self, scope: str, key: str) -> None:
        for r in self.store.query([scope], ["resource"], key=key):
            self.store.set_status(r["id"], "needs_recheck")

    # ---- trust / verify-before-trust ---------------------------------------------
    def trust_level(self, row: dict, env_fp: str | None = None) -> str:
        """trusted | hint | recheck | untrusted — stale or post-deploy rows are hints only."""
        if row.get("status") == "needs_recheck":
            return "recheck"
        if row.get("status") != "active":
            return "untrusted"
        if env_fp and row.get("env_at_confirm") and row["env_at_confirm"] != env_fp:
            return "hint"
        ts = _parse_ts(row.get("last_confirmed_at"))
        if ts is not None:
            age = (datetime.now(timezone.utc) - ts).days
            if age >= STALE_DAYS:
                return "hint"
        return "trusted"

    # ---- loading for prompts (lean) ----------------------------------------------
    def preload(self, areas: list[str] | None = None, char_cap: int | None = None,
                query: str = "", app: str = "", state=None) -> list[dict]:
        """Small ranked set for the current area — never a dump. Prefer retrieve() for turns."""
        cap = char_cap if char_cap is not None else PRELOAD_CHAR_CAP
        from .retrieve import DEFAULT_TOKEN_BUDGET, TurnRetriever, tokens_of
        budget = max(200, cap // 4)
        tr = TurnRetriever(self, token_budget=min(budget, DEFAULT_TOKEN_BUDGET), state=state)
        area = areas[0] if areas else None
        res = tr.retrieve(area=area, app=app, query=query or " ".join(areas or []), include_learned=False)
        # Keep API: list of public rows (judge still expects list[dict]).
        _ = tokens_of  # documented pairing with budget tests
        return res.rows

    def recall(self, query: str, k: int = 5, *, area: str | None = None, app: str = "",
               state=None, char_cap: int | None = None) -> list[dict]:
        """On-demand search (active + needs_recheck), still budgeted. Agent tool path."""
        # Search includes needs_recheck so the agent can re-verify flagged rows deliberately.
        rows = self.store.search(
            self.scopes, query, k=max(k, 8),
            kinds=["lesson", "recipe", "known_bug", "open_bug_found", "fact"],
            statuses=("active", "needs_recheck"),
        )
        for r in rows:
            r["trust"] = self.trust_level(r)
        # Keep under a small char budget even for on-demand recall.
        from .retrieve import truncate_row_text
        out, used, cap = [], 0, char_cap or PRELOAD_CHAR_CAP
        for r in rows:
            text = truncate_row_text(r.get("text") or "")
            n = len(text) + 40
            if used + n > cap:
                break
            out.append(public({**r, "text": text}))
            used += n
            if len(out) >= k:
                break
        return out

    @staticmethod
    def as_prompt(rows: list[dict]) -> str:
        if not rows:
            return "(no saved lessons yet)"
        from .retrieve import truncate_row_text
        return "\n".join(
            f"[{r['id']}] ({r['kind']}{', ' + r['area'] if r.get('area') else ''}) {truncate_row_text(r['text'])}"
            for r in rows
        )

    # ---- writing ------------------------------------------------------------------
    def correction(self, case_id: str, given: str, correct: str, why: str, *, area: str | None = None,
                   run_id: str | None = None, linked_issue: int | None = None, general: bool = False,
                   decided_by: str = "user") -> int:
        """User corrected a verdict: saved as an active lesson immediately (tech spec 7.3.1)."""
        text = assert_safe_lasting(
            f"{why.strip()} (verdict should be {correct}, not {given}; case {case_id})",
            context="correction",
        )
        return self.store.add(
            "lesson", self.project, text, "user_correction", area=area,
            applies_when="general" if general else None, linked_issue=linked_issue,
            evidence=run_id, status="active", decided_by=decided_by, confirm_count=1,
        )

    def propose(self, kind: str, text: str, *, area: str | None = None, applies_when: str | None = None,
                evidence: str | None = None, linked_issue: int | None = None,
                env_fp: str | None = None) -> int:
        """Agent proposal: waits as `proposed` until user approval or (≥2) run confirmations for lessons/recipes.

        Facts (lasting rules) always stay proposed until the user approves.
        """
        if kind not in ("lesson", "recipe", "fact"):
            raise ValueError("agents may only propose lessons, recipes or facts")
        text = assert_safe_lasting(text, context=f"propose.{kind}")
        dupes = [r for r in self.store.query(self.scopes, [kind], statuses=("active", "proposed", "needs_recheck"))
                 if jaccard(r["text"], text) > 0.8]
        if dupes:
            existing = dupes[0]
            # Confirm across different runs only (same-run re-propose is just dedupe).
            prev = (existing.get("evidence") or "").split("/")[0]
            cur = (evidence or "").split("/")[0]
            if cur and prev and cur != prev:
                return self.store.bump_confirm(existing["id"], env_fp=env_fp)["id"]
            return existing["id"]
        return self.store.add(
            kind, self.project, text, "agent_proposal", area=area, applies_when=applies_when,
            evidence=evidence, linked_issue=linked_issue, status="proposed", confirm_count=1,
            env_at_confirm=env_fp,
        )

    def add_fact(self, text: str, source: str, *, area: str | None = None, decided_by: str | None = None,
                 status: str = "proposed") -> int:
        """Stable project rule. Agent-proposed facts need approval; user/source may set active."""
        text = assert_safe_lasting(text, context="fact")
        return self.store.add(
            "fact", self.project, text, source, area=area, status=status,
            decided_by=decided_by or source, confirm_count=1 if status == "active" else 0,
        )

    def pending(self) -> list[dict]:
        return sorted((public(r) for r in self.store.query(self.scopes, statuses=("proposed",))), key=lambda r: r["id"])

    def approve(self, ids: list[int], *, env_fp: str | None = None) -> None:
        for i in ids:
            self.store.set_status(i, "active", env_at_confirm=env_fp)
            row = self.store.get(i)
            if row and (row.get("confirm_count") or 0) < 1:
                self.store.bump_confirm(i, env_fp=env_fp)

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
            try:
                text = assert_safe_lasting(text, context="open_bug")
            except EnvDetailError:
                from .env_guard import scrub_env_details
                text = scrub_env_details(text)[:400]
            ids.append(self.store.add("open_bug_found", self.project, text, "run_end",
                                      linked_issue=task_id, evidence=run_id))
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

    # ---- maintenance --------------------------------------------------------------
    def prune(self) -> dict:
        retired = self.store.prune_duplicates(self.scopes)
        scrubbed = self.store.scrub_env_resources()
        return {"retired_dupes": retired, "scrubbed_resources": scrubbed}

    # ---- migration ----------------------------------------------------------------
    def import_resources_json(self, path: Path) -> int:
        """Move phase 1 resources.json rows into the database once (values dropped; secret_env kept)."""
        if not path.exists():
            return 0
        n = 0
        for row in json.loads(path.read_text()):
            if row.get("stale"):
                continue
            self.put(row["scope"], row["key"], row.get("value") or "", source=row.get("source", "user"),
                     secret_env=row.get("secret_env"))
            n += 1
        path.rename(path.with_suffix(".json.imported"))
        return n
