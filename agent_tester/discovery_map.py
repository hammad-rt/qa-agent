"""Verified discovery maps in the memory SQLite DB.

Per (area, app, checklist item) under a project scope, keyed by env fingerprint
(host + env; platform/version ride along as key fields, not lasting facts).

Routes are menu/label text only (scrubbed). An item is reusable for skipping live
discovery when verified (or covered by a completed discovery pass) under the
reuse rules. Deploy-marker changes trigger a code-driven spot check, not blind reuse.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

from .memory.env_guard import EnvDetailError, assert_safe_lasting, scrub_env_details
from .memory.store import now as _now

# ---- schema --------------------------------------------------------------------

MAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS discovery_maps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL,
  area TEXT,
  app TEXT NOT NULL,
  checklist_item TEXT NOT NULL,
  item_key TEXT NOT NULL,
  env_fingerprint TEXT NOT NULL,
  platform TEXT,
  platform_version TEXT,
  route_text TEXT NOT NULL,
  key_labels TEXT,
  deploy_marker TEXT,
  status TEXT NOT NULL DEFAULT 'proposed',
  source TEXT,
  verified_at TEXT,
  last_success_at TEXT,
  success_count INTEGER NOT NULL DEFAULT 0,
  fail_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(scope, area, app, item_key, env_fingerprint)
);
CREATE INDEX IF NOT EXISTS ix_disco_map_lookup
  ON discovery_maps(scope, area, app, env_fingerprint, status);
"""


def _ensure_map_columns(db: sqlite3.Connection) -> None:
    """Add columns introduced after the first schema (idempotent)."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(discovery_maps)")}
    if "source" not in cols:
        db.execute("ALTER TABLE discovery_maps ADD COLUMN source TEXT")
        db.commit()

STATUSES = {"proposed", "verified", "partial", "stale"}

# Default age window for verified reuse (days).
DEFAULT_REUSE_MAX_DAYS = 14

# Estimated discovery tool calls saved per reused item (matches coordinator budget math).
EST_CALLS_BASE = 30
EST_CALLS_PER_ITEM = 5

# UI-change words that force rediscovery when they mention the item's screen/module.
_CHANGE_WORDS = (
    r"new|changed|renamed|moved|added|removed|redesign(?:ed)?|redesign|"
    r"updated|relabel(?:led|ed)?|relocated"
)
_UI_NOUNS = (
    r"field|button|menu|screen|subpanel|layout|label|tab|panel|form|page|ui|UX"
)
CHANGE_SIGNAL = re.compile(
    rf"(?i)\b(?:{_CHANGE_WORDS})\b.{{0,40}}\b(?:{_UI_NOUNS})\b"
    rf"|\b(?:{_UI_NOUNS})\b.{{0,40}}\b(?:{_CHANGE_WORDS})\b"
)

_ASSET_SRC = re.compile(
    r"""(?is)<(?:script|link)\b[^>]*(?:src|href)\s*=\s*["']([^"']+)["']"""
)
_META_VER = re.compile(
    r"""(?is)<meta\b[^>]*(?:name|property)\s*=\s*["'][^"']*(?:version|build|generator)[^"']*["'][^>]*"""
    r"""content\s*=\s*["']([^"']+)["']"""
    r"""|content\s*=\s*["']([^"']+)["'][^>]*(?:name|property)\s*=\s*["'][^"']*(?:version|build|generator)[^"']*["']"""
)
_FOOTER_VER = re.compile(
    r"(?i)(?:version|build|rev(?:ision)?)\s*[:=]?\s*v?(\d+(?:\.\d+){0,3}(?:[-+][a-z0-9.]+)?)"
)
_CACHE_BUST = re.compile(r"[?&]v=|/[\w.-]*\.[a-f0-9]{6,}[\w.-]*\.(?:js|css)|-\d{8,}\.(?:js|css)", re.I)


def reuse_enabled() -> bool:
    """DISCOVERY_REUSE=0 disables reuse (always live discovery)."""
    v = (os.environ.get("DISCOVERY_REUSE") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def reuse_max_days() -> int:
    try:
        return max(1, int(os.environ.get("DISCOVERY_REUSE_MAX_DAYS") or DEFAULT_REUSE_MAX_DAYS))
    except ValueError:
        return DEFAULT_REUSE_MAX_DAYS


_STUCK_ROUTE = re.compile(
    r"(?i)\bstuck\s*\(|\bkept repeating\b|\bwithout progress\b|\balready tried:\b|"
    r"\bnext time:\s*do not repeat\b|\bdiscovery failed\b|\bBLOCKED:\b|"
    r"\bagent incomplete\b|\bmaximum repeated\b|\bloop_detected\b"
)
_PREP_BLOB = re.compile(
    r"^(?:WALLET|ORDER|CUSTOMER|PREP|CONTACT)\b.*\bcreate:\s*|"
    r"\bcreate:\s*.{40,}",
    re.I,
)
_GARBAGE_LABEL = re.compile(
    r"(?i)(?:\*\*|Path:\s*`|Rewards\*\*|```|\{[\"']content)"
)


def screen_name(checklist_item: str, limit: int = 60) -> str:
    """Normalized checklist screen name — never a PREP create instruction blob."""
    t = scrub_env_details(checklist_item or "")
    t = re.sub(r"\s+", " ", t).strip()
    if _PREP_BLOB.search(t) or len(t) > 80:
        # Prefer leading screen-ish phrase before create/instruction prose.
        m = re.match(r"(?i)^(?:WALLET|ORDER|CUSTOMER|PREP|CONTACT)\s+(\w[\w /&-]{2,40})", t)
        if m:
            t = m.group(1).strip(" -/")
        else:
            m = re.match(r"^([^:]{3,50})", t)
            t = (m.group(1) if m else t[:limit]).strip(" -")
    t = re.sub(r"\s+", " ", t).strip(" -:.")
    return (t or "screen")[:limit]


def is_prep_instruction(text: str | None) -> bool:
    return bool(text) and bool(_PREP_BLOB.search(text or ""))


def is_stuck_or_failed_route(text: str | None) -> bool:
    """True for stuck/loop/failed traces that must never be stored as routes."""
    t = text or ""
    if not t.strip():
        return True
    if _STUCK_ROUTE.search(t):
        return True
    if re.search(r"(?i)^stuck\b", t.strip()):
        return True
    return False


def route_is_savable(route_text: str | None, key_labels: list[str] | None = None) -> bool:
    """Concrete menu/click path only — refuse stuck prose and garbage label blobs."""
    if is_stuck_or_failed_route(route_text):
        return False
    t = (route_text or "").strip()
    if len(t) < 8:
        return False
    # click / menu arrows, helper runs, or pack ## Routes breadcrumb paths (Home > Login).
    if not re.search(r"(?i)(click|menu|→|->|open\s+/|run helper|\s>\s)", t):
        return False
    for lab in key_labels or []:
        if _GARBAGE_LABEL.search(str(lab or "")):
            return False
    return True


def item_key(checklist_item: str) -> str:
    """Stable key for a checklist screen (normalized screen name, no env / PREP noise)."""
    t = screen_name(checklist_item)
    t = re.sub(r"\s+", " ", t).strip().lower()
    t = re.sub(r"[^a-z0-9 :._/-]+", "", t)
    return hashlib.sha1(t.encode()).hexdigest()[:16] if t else "empty"


def _safe_route(text: str) -> str:
    """Menu/label route only; refuse env-bound identity."""
    from .memory.env_guard import find_env_details
    original = re.sub(r"\s+", " ", (text or "").strip())
    had_env = bool(find_env_details(original))
    raw = scrub_env_details(original)
    # Drop bare record-looking tokens left after scrub.
    raw = re.sub(r"\b[A-Z]{1,3}\d{3,}\b", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" -.:;")
    if not raw or len(raw) < 4:
        raise EnvDetailError("discovery_map: empty route after scrub")
    cleaned = assert_safe_lasting(raw, context="discovery_map.route")
    # If the original was mostly env noise, refuse even when placeholders remain readable.
    if had_env:
        substance = re.sub(r"\[(?:email|url|host|login|masked)\]|<a record>|<id>", " ", cleaned)
        substance = re.sub(r"\s+", " ", substance).strip(" -.:;,").strip()
        if len(substance) < 20 or not re.search(r"(?i)click|menu|→|->|button|link|tab|open ['\"]", substance):
            raise EnvDetailError(
                f"discovery_map: refused env-bound route (no lasting menu path): {original[:120]}"
            )
    return cleaned


def _safe_labels(labels: list[str] | None) -> list[str]:
    out: list[str] = []
    for lab in labels or []:
        try:
            s = _safe_route(str(lab))
        except EnvDetailError:
            continue
        if s and s.lower() not in {x.lower() for x in out}:
            out.append(s[:80])
        if len(out) >= 12:
            break
    return out


def labels_from_route(route_text: str) -> list[str]:
    """Pull clickable labels from a 'click X -> click Y' / 'Menu > Sub' route string."""
    t = route_text or ""
    found: list[str] = []
    for m in re.finditer(r"click\s+'([^']+)'|click\s+\"([^\"]+)\"", t, re.I):
        found.append(m.group(1) or m.group(2))
    if not found:
        # Menu > Sub > Action or A -> B hops
        for part in re.split(r"\s*(?:->|→|>|/)\s*", t):
            part = re.sub(r"(?i)^(open|route:|when|go to|menu:?)\s*", "", part).strip(" .;:")
            part = re.sub(r"^\d{4}-\d\d-\d\d.*", "", part).strip()
            if 2 < len(part) < 60 and not part.lower().startswith(("http", "index.php")):
                found.append(part)
    return _safe_labels(found)


# ---- deploy marker -------------------------------------------------------------

def extract_asset_urls(html: str, base_url: str = "") -> list[str]:
    """Script/css URLs that typically change on deploy (cache-busting names / ?v=)."""
    out: list[str] = []
    for m in _ASSET_SRC.finditer(html or ""):
        u = (m.group(1) or "").strip()
        if not u or u.startswith("data:"):
            continue
        low = u.lower()
        if not any(x in low for x in (".js", ".css", "stylesheet")) and "rel=" not in (m.group(0) or "").lower():
            # Keep only script src and stylesheet-looking links.
            if "<link" in m.group(0).lower() and "stylesheet" not in m.group(0).lower():
                continue
        if base_url and not u.startswith(("http://", "https://", "//")):
            u = urljoin(base_url, u)
        out.append(u)
    # Prefer cache-busted assets; fall back to all if none.
    busted = [u for u in out if _CACHE_BUST.search(u)]
    return busted or out


def extract_version_meta(html: str) -> list[str]:
    bits: list[str] = []
    for m in _META_VER.finditer(html or ""):
        bits.append((m.group(1) or m.group(2) or "").strip())
    for m in _FOOTER_VER.finditer((html or "")[-8000:]):
        bits.append(m.group(1))
    return [b for b in bits if b]


def compute_deploy_marker(
    *,
    url: str = "",
    html: str = "",
    platform_version: str | None = None,
    fetch: bool = True,
    timeout: float = 5.0,
) -> str:
    """Cheap code-only deploy marker: asset URL hash + version/meta + platform version."""
    page = html or ""
    if not page and fetch and url and url.startswith("http"):
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "qa-agent-deploy-marker/1"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — run-start marker only
                page = resp.read(400_000).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            page = ""
    assets = extract_asset_urls(page, url)
    versions = extract_version_meta(page)
    if platform_version:
        versions.append(str(platform_version))
    host = urlparse(url).netloc if url else ""
    blob = "\n".join([host, *sorted(set(assets))[:40], *sorted(set(versions))[:10]])
    if not blob.strip():
        return ""
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def markers_for_plan(plan, state, *, fetch: bool = True) -> dict[str, str]:
    """Compute per-app deploy markers at run start; store under state.data['deploy_markers']."""
    from urllib.parse import urlparse as _up
    markers: dict[str, str] = dict(state.data.get("deploy_markers") or {})
    plat_ver = state.data.get("detected_platform_version")
    urls: dict[str, str] = {}
    for r in getattr(plan, "requirements", None) or []:
        key = getattr(r, "key", None) or (r.get("key") if isinstance(r, dict) else None)
        val = getattr(r, "value", None) or (r.get("value") if isinstance(r, dict) else None)
        if not key or not val:
            continue
        parts = str(key).split(".")
        if len(parts) >= 3 and parts[0] == "env" and str(val).startswith("http"):
            urls[parts[-1]] = str(val)
    for app, url in urls.items():
        if app in markers and markers[app]:
            continue
        markers[app] = compute_deploy_marker(url=url, platform_version=plat_ver, fetch=fetch)
    state.data["deploy_markers"] = markers
    try:
        state.save()
    except Exception:  # noqa: BLE001
        pass
    return markers


# ---- change signal -------------------------------------------------------------

def change_signal_text(*parts: str) -> str:
    return "\n".join(p for p in parts if p)


def has_change_signal(text: str, checklist_item: str) -> bool:
    """True when ticket/case/step text signals a UI change for this item's screen/module."""
    blob = text or ""
    if not CHANGE_SIGNAL.search(blob):
        return False
    # Require the change wording to sit near distinctive words from the checklist item.
    words = [w for w in re.findall(r"[a-z0-9]{4,}", (checklist_item or "").lower())
             if w not in {"from", "with", "their", "this", "that", "open", "list", "and", "create", "find"}]
    if not words:
        return bool(CHANGE_SIGNAL.search(blob))
    # Windowed: change phrase and an item word within ~80 chars.
    for m in CHANGE_SIGNAL.finditer(blob):
        start, end = max(0, m.start() - 80), min(len(blob), m.end() + 80)
        window = blob[start:end].lower()
        if any(w in window for w in words):
            return True
    # Also: item word then change words nearby.
    low = blob.lower()
    for w in words:
        for m in re.finditer(re.escape(w), low):
            window = blob[max(0, m.start() - 40): m.end() + 80]
            if CHANGE_SIGNAL.search(window):
                return True
    return False


def ticket_case_blob(plan, case) -> str:
    """Deterministic text used for change-signal regex (ticket + case + steps)."""
    from .field_text import field_text

    parts: list[str] = []
    for attr in ("description", "subject", "title", "summary"):
        v = field_text(getattr(plan, attr, None))
        if v:
            parts.append(v)
    notes = field_text(getattr(plan, "notes", None) or getattr(plan, "ticket_text", None))
    if notes:
        parts.append(notes)
    # Feature mode may stash ticket texts on the plan/state elsewhere; include case fields.
    # Fields may be list/dict after resume or raw JSON — coerce via field_text.
    if case is not None:
        for attr in ("title", "expected", "preconditions", "notes", "acceptance"):
            v = field_text(getattr(case, attr, None))
            if v:
                parts.append(v)
        for s in getattr(case, "steps", None) or []:
            v = field_text(s)
            if v:
                parts.append(v)
    return "\n".join(parts)


# ---- store API -----------------------------------------------------------------

class DiscoveryMapStore:
    """CRUD over discovery_maps in the same SQLite file as MemoryStore."""

    def __init__(self, db: sqlite3.Connection | Path | str):
        if isinstance(db, (str, Path)):
            self.path = Path(db)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path)
            self.db.row_factory = sqlite3.Row
            self._owns = True
        else:
            self.db = db
            self.db.row_factory = sqlite3.Row
            self.path = None
            self._owns = False
        self.db.executescript(MAP_SCHEMA)
        self.db.commit()
        _ensure_map_columns(self.db)

    @classmethod
    def from_memory(cls, memory) -> "DiscoveryMapStore | None":
        store = getattr(memory, "store", None)
        db = getattr(store, "db", None) if store is not None else None
        if db is None:
            return None
        return cls(db)

    def close(self) -> None:
        if self._owns:
            self.db.close()

    def upsert_route(
        self,
        *,
        scope: str,
        area: str | None,
        app: str,
        checklist_item: str,
        env_fingerprint: str,
        route_text: str,
        key_labels: list[str] | None = None,
        deploy_marker: str | None = None,
        platform: str | None = None,
        platform_version: str | None = None,
        status: str = "proposed",
        source: str | None = None,
        last_success_at: str | None = None,
        success_count: int | None = None,
    ) -> dict:
        """Atomic write of a scrubbed route. Never stores env-bound identity or stuck traces."""
        assert status in STATUSES, status
        if is_stuck_or_failed_route(route_text) or is_prep_instruction(checklist_item):
            raise EnvDetailError("discovery_map: refused stuck/failed or PREP-instruction route")
        route = _safe_route(route_text)
        labels = _safe_labels(key_labels) or labels_from_route(route)
        if not route_is_savable(route, labels):
            raise EnvDetailError("discovery_map: refused non-route or garbage labels")
        # Always key and store under the normalized screen name.
        item = screen_name(checklist_item)
        ik = item_key(item)
        area_s = area or "general"
        ts = _now()
        existing = self.get(scope, area_s, app, item, env_fingerprint)
        if existing:
            self.db.execute(
                """UPDATE discovery_maps SET route_text=?, key_labels=?, deploy_marker=COALESCE(?, deploy_marker),
                   platform=COALESCE(?, platform), platform_version=COALESCE(?, platform_version),
                   status=?, source=COALESCE(?, source), updated_at=?,
                   checklist_item=?
                   WHERE id=?""",
                (route, json.dumps(labels), deploy_marker or None, platform, platform_version,
                 status if existing["status"] != "verified" or status == "verified" else existing["status"],
                 source, ts, scrub_env_details(item)[:200], existing["id"]),
            )
            # Keep verified if already verified unless explicitly stale.
            if existing["status"] == "verified" and status == "proposed":
                self.db.execute("UPDATE discovery_maps SET status='verified' WHERE id=?", (existing["id"],))
            if status == "verified" or last_success_at or success_count is not None:
                sc = success_count if success_count is not None else (1 if status == "verified" else None)
                self.db.execute(
                    """UPDATE discovery_maps SET verified_at=COALESCE(verified_at, ?),
                       last_success_at=COALESCE(?, last_success_at),
                       success_count=CASE
                         WHEN ? IS NOT NULL THEN MAX(success_count, ?)
                         WHEN success_count < 1 AND ? = 'verified' THEN 1
                         ELSE success_count END
                       WHERE id=?""",
                    (last_success_at or ts, last_success_at or (ts if status == "verified" else None),
                     sc, int(sc or 0), status, existing["id"]),
                )
            self.db.commit()
            return self.get_by_id(existing["id"])  # type: ignore[return-value]
        verified_at = (last_success_at or ts) if status == "verified" else None
        success_at = verified_at  # stamp pack/verified imports so the 14-day reuse window applies
        sc = int(success_count if success_count is not None else (1 if status == "verified" else 0))
        cur = self.db.execute(
            """INSERT INTO discovery_maps(
                 scope, area, app, checklist_item, item_key, env_fingerprint, platform, platform_version,
                 route_text, key_labels, deploy_marker, status, source, verified_at, last_success_at,
                 success_count, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (scope, area_s, app, scrub_env_details(item)[:200], ik, env_fingerprint,
             platform, platform_version, route, json.dumps(labels), deploy_marker or None,
             status, source, verified_at, success_at,
             sc, ts, ts),
        )
        self.db.commit()
        return self.get_by_id(cur.lastrowid)  # type: ignore[return-value]

    def get(self, scope: str, area: str | None, app: str, checklist_item: str,
            env_fingerprint: str) -> dict | None:
        ik = item_key(checklist_item)
        r = self.db.execute(
            """SELECT * FROM discovery_maps WHERE scope=? AND area=? AND app=? AND item_key=?
               AND env_fingerprint=?""",
            (scope, area or "general", app, ik, env_fingerprint),
        ).fetchone()
        return _map_row(r)

    def get_by_id(self, mid: int) -> dict | None:
        r = self.db.execute("SELECT * FROM discovery_maps WHERE id=?", (mid,)).fetchone()
        return _map_row(r)

    def list_for(self, scope: str, area: str | None, app: str, env_fingerprint: str,
                 statuses: tuple[str, ...] | None = None) -> list[dict]:
        sql = """SELECT * FROM discovery_maps WHERE scope=? AND area=? AND app=? AND env_fingerprint=?"""
        args: list = [scope, area or "general", app, env_fingerprint]
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args.extend(statuses)
        sql += " ORDER BY id"
        return [_map_row(r) for r in self.db.execute(sql, args)]

    def mark_verified(self, mid: int, *, deploy_marker: str | None = None) -> dict | None:
        ts = _now()
        self.db.execute(
            """UPDATE discovery_maps SET status='verified', verified_at=COALESCE(verified_at, ?),
               last_success_at=?, success_count=success_count+1, updated_at=?,
               deploy_marker=COALESCE(?, deploy_marker),
               fail_count=CASE WHEN fail_count > 0 AND last_success_at IS NOT NULL THEN fail_count ELSE fail_count END
               WHERE id=?""",
            (ts, ts, ts, deploy_marker, mid),
        )
        # Clear fail_count after a fresh success (no failure since last success).
        self.db.execute(
            "UPDATE discovery_maps SET fail_count=0 WHERE id=? AND last_success_at=?",
            (mid, ts),
        )
        self.db.commit()
        return self.get_by_id(mid)

    def mark_success(self, mid: int, *, deploy_marker: str | None = None) -> dict | None:
        """Bump success after a spot-check or case use; sets verified."""
        return self.mark_verified(mid, deploy_marker=deploy_marker)

    def mark_stale(self, mid: int, why: str = "") -> dict | None:
        ts = _now()
        self.db.execute(
            """UPDATE discovery_maps SET status='stale', fail_count=fail_count+1, updated_at=? WHERE id=?""",
            (ts, mid),
        )
        self.db.commit()
        row = self.get_by_id(mid)
        if row and why:
            row = dict(row)
            row["stale_why"] = why
        return row

    def mark_bad_rows_stale(self, scope: str, env_fingerprint: str | None = None) -> int:
        """Mark stuck/garbage/PREP-blob rows stale so they are never reused. Returns count marked."""
        sql = "SELECT id, route_text, key_labels, checklist_item, status FROM discovery_maps WHERE scope=?"
        args: list = [scope]
        if env_fingerprint is not None:
            sql += " AND env_fingerprint=?"
            args.append(env_fingerprint)
        n = 0
        for r in self.db.execute(sql, args):
            d = dict(r)
            if d.get("status") == "stale":
                continue
            labels = []
            try:
                labels = json.loads(d.get("key_labels") or "[]")
            except (TypeError, ValueError):
                labels = []
            bad = (
                is_stuck_or_failed_route(d.get("route_text"))
                or is_prep_instruction(d.get("checklist_item"))
                or not route_is_savable(d.get("route_text"), labels)
            )
            if bad:
                self.mark_stale(d["id"], "bad route (stuck/garbage/PREP)")
                n += 1
        return n

    def mark_partial_coverage(self, scope: str, area: str | None, app: str, env_fingerprint: str,
                              covered_item_keys: list[str]) -> None:
        """Tag the area:app map as partial: covered items stay; missing ones will be discovered live."""
        # Status on covered proposed rows stays proposed/verified; we record partial on a sentinel note
        # via updated_at bump — callers track covered keys in state.
        ts = _now()
        for ik in covered_item_keys:
            self.db.execute(
                """UPDATE discovery_maps SET updated_at=? WHERE scope=? AND area=? AND app=?
                   AND env_fingerprint=? AND item_key=? AND status IN ('proposed','verified','partial')""",
                (ts, scope, area or "general", app, env_fingerprint, ik),
            )
        self.db.commit()

    def refresh_marker(self, mid: int, deploy_marker: str) -> None:
        self.db.execute(
            "UPDATE discovery_maps SET deploy_marker=?, updated_at=? WHERE id=?",
            (deploy_marker, _now(), mid),
        )
        self.db.commit()


def _map_row(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    try:
        d["key_labels"] = json.loads(d.get("key_labels") or "[]")
    except (TypeError, ValueError):
        d["key_labels"] = []
    return d


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_days(iso: str | None) -> float | None:
    ts = _parse_ts(iso)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0


def is_reusable(row: dict, *, force: bool = False, change_text: str = "",
                current_platform_version: str | None = None,
                current_marker: str | None = None) -> str:
    """Return 'reuse' | 'spot_check' | 'discover'.

    - reuse: same fingerprint already keyed; verified (or proposed covered) with rules.
    - spot_check: deploy marker changed — do not blind reuse, do not full discover.
    - discover: not reusable.
    """
    if force or not reuse_enabled() or not row:
        return "discover"
    if row.get("status") == "stale":
        return "discover"
    if has_change_signal(change_text, row.get("checklist_item") or ""):
        return "discover"
    stored_ver = row.get("platform_version") or ""
    if current_platform_version and stored_ver and current_platform_version != stored_ver:
        return "discover"
    # Failure since last success → rediscover.
    if int(row.get("fail_count") or 0) > 0 and row.get("last_success_at"):
        # fail after success
        return "discover"
    if int(row.get("fail_count") or 0) > 0 and row.get("status") != "verified":
        return "discover"

    marker = current_marker or ""
    stored_marker = row.get("deploy_marker") or ""
    if marker and stored_marker and marker != stored_marker:
        # Deploy changed: spot-check, don't full discover yet.
        if row.get("route_text") and (row.get("key_labels") or labels_from_route(row["route_text"])):
            return "spot_check"
        return "discover"

    status = row.get("status") or ""
    # Stuck/garbage never reuse (route_is_savable). Verified successes skip live discovery.
    # Savable proposed/partial rows from a mid-discovery stop also reuse so the next run
    # only discovers the remaining checklist items (lazy / incremental).
    if not route_is_savable(row.get("route_text"), row.get("key_labels")):
        return "discover"
    if status == "verified" and int(row.get("success_count") or 0) >= 1:
        age = age_days(row.get("last_success_at") or row.get("verified_at"))
        # No success stamp → not reusable forever; keep the DEFAULT_REUSE_MAX_DAYS window.
        if age is not None and age <= reuse_max_days():
            return "reuse"
        return "discover"
    if status in ("proposed", "partial"):
        return "reuse"
    return "discover"


# ---- spot check ----------------------------------------------------------------

def labels_present(snapshot: str, labels: list[str]) -> list[str]:
    """Return labels NOT found in the snapshot text (case-insensitive)."""
    blob = (snapshot or "").lower()
    missing = []
    for lab in labels:
        if lab.lower() not in blob:
            missing.append(lab)
    return missing


def spot_check_route(
    *,
    route_text: str,
    key_labels: list[str],
    snapshot_text: str = "",
    navigate_and_snapshot: Callable[[str, list[str]], str] | None = None,
) -> tuple[bool, list[str], str]:
    """Code-driven spot check. Returns (ok, missing_labels, snapshot_used).

    If navigate_and_snapshot is provided, it receives (route_text, labels) and must
    return a page snapshot string after navigating the saved route. Otherwise only
    the provided snapshot_text is checked (for unit tests).
    """
    labels = list(key_labels) or labels_from_route(route_text)
    if not labels:
        return False, ["(no key labels)"], snapshot_text or ""
    snap = snapshot_text or ""
    if navigate_and_snapshot is not None:
        try:
            snap = navigate_and_snapshot(route_text, labels) or ""
        except Exception as e:  # noqa: BLE001
            return False, [f"spot-check navigate failed: {type(e).__name__}"], snap
    missing = labels_present(snap, labels)
    return (not missing), missing, snap


def estimate_calls_saved(n_reused: int) -> int:
    if n_reused <= 0:
        return 0
    return min(60, EST_CALLS_BASE + EST_CALLS_PER_ITEM * max(0, n_reused - 1))


# ---- pending flush buffer (state.data) ----------------------------------------

def buffer_pending(state, entry: dict) -> None:
    """Queue a map write in state for signal/finally flush (also written immediately by callers)."""
    buf = state.data.setdefault("_discovery_map_pending", [])
    buf.append(entry)
    try:
        state.save()
    except Exception:  # noqa: BLE001
        pass


def flush_pending(state, memory) -> int:
    """Write any buffered discovery map rows (idempotent upsert). Returns count written."""
    buf = list(state.data.get("_discovery_map_pending") or [])
    if not buf:
        return 0
    store = DiscoveryMapStore.from_memory(memory) if memory is not None else None
    if store is None:
        return 0
    n = 0
    for e in buf:
        try:
            store.upsert_route(
                scope=e["scope"], area=e.get("area"), app=e["app"],
                checklist_item=e["checklist_item"], env_fingerprint=e["env_fingerprint"],
                route_text=e["route_text"], key_labels=e.get("key_labels"),
                deploy_marker=e.get("deploy_marker"), platform=e.get("platform"),
                platform_version=e.get("platform_version"),
                status=e.get("status") or "proposed",
                source=e.get("source"),
            )
            n += 1
        except EnvDetailError:
            continue
        except Exception:  # noqa: BLE001
            continue
    state.data["_discovery_map_pending"] = []
    try:
        state.save()
    except Exception:  # noqa: BLE001
        pass
    return n


def scope_for(memory) -> str:
    slug = getattr(memory, "slug", None) or "unknown"
    return f"project:{slug}"


_STOP_MATCH = frozenset({
    "from", "with", "their", "this", "that", "open", "list", "and", "the", "for",
    "create", "find", "prep", "into", "onto", "then", "when", "how", "via",
})

# App-agnostic phrase aliases. Product labels (menu names, module titles) stay in the pack.
CORE_ROUTE_ALIAS_GROUPS: list[list[str]] = [
    ["login", "log in", "sign in", "signin", "sign-in"],
    ["move status", "order status", "change status", "status"],
    ["wallet", "rewards", "open wallet", "open wallet / rewards"],
    ["quote", "quotes", "request a quote"],
    ["invoice", "invoices"],
    ["detail view", "record detail", "detail", "subpanel"],
    ["list search", "search", "lookup"],
    ["record payment", "payment", "payments"],
    ["receive package", "receive", "package", "parcel"],
]


def _word_tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if w not in _STOP_MATCH}


def _normalize_match_text(text: str) -> str:
    t = (text or "").lower().replace("&", " and ")
    t = re.sub(r"[/|]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _phrases_present(text: str, phrases: list[str]) -> list[str]:
    """Phrases found in text: multi-word substrings, or single tokens of length >= 5."""
    low = _normalize_match_text(text)
    hits: list[str] = []
    if not low:
        return hits
    for p in phrases:
        pn = _normalize_match_text(str(p))
        if not pn:
            continue
        if " " in pn:
            if pn in low:
                hits.append(pn)
        elif len(pn) >= 5 and re.search(rf"\b{re.escape(pn)}\b", low):
            hits.append(pn)
    return hits


def _alias_phrase_hit(a: str, b: str, synonym_groups: list[list[str]] | None) -> bool:
    for group in synonym_groups or []:
        phrases = [str(x) for x in group if x]
        if _phrases_present(a, phrases) and _phrases_present(b, phrases):
            return True
    return False


def _load_yaml_synonyms(path: Path, _add) -> None:
    if not path.is_file():
        return
    try:
        import yaml
        cfg = yaml.safe_load(path.read_text()) or {}
        _add(cfg.get("route_synonyms"))
    except Exception:  # noqa: BLE001
        pass


def load_route_synonym_groups(
    pack_cfg: dict | None = None,
    *,
    platform_id: str | None = None,
    platforms_dir: Path | None = None,
    pack_root=None,
) -> list[list[str]]:
    """Load synonym groups: generic core aliases, then pack.yaml, then platform.yaml.

    Core aliases are app-agnostic (login, status, wallet, …). Product-specific
    labels belong in pack ``route_synonyms``. If ``pack_cfg`` has no synonym
    table, ``pack_root/pack.yaml`` is read as a fallback (live runs historically
    stored a trimmed pack_cfg without this key).
    """
    groups: list[list[str]] = [list(g) for g in CORE_ROUTE_ALIAS_GROUPS]

    def _add(raw):
        if not isinstance(raw, list):
            return
        for g in raw:
            if isinstance(g, (list, tuple)) and g:
                phrases = [str(x).strip() for x in g if str(x).strip()]
                if phrases:
                    groups.append(phrases)

    cfg_syn = (pack_cfg or {}).get("route_synonyms")
    _add(cfg_syn)
    if not cfg_syn and pack_root:
        _load_yaml_synonyms(Path(pack_root) / "pack.yaml", _add)
    if platform_id:
        from .config import ROOT
        root = platforms_dir or (ROOT / "packs" / "_platforms")
        _load_yaml_synonyms(root / platform_id / "platform.yaml", _add)
    return groups


def expand_match_tokens(text: str, synonym_groups: list[list[str]] | None = None) -> set[str]:
    """Token set for a screen/checklist string, expanded by core/pack/platform synonym groups."""
    low = _normalize_match_text(text)
    out = _word_tokens(low)
    for group in synonym_groups or []:
        phrases = [str(p).lower().strip() for p in group if p]
        if not phrases:
            continue
        hit = False
        for p in phrases:
            if not p:
                continue
            pn = _normalize_match_text(p)
            if pn and pn in low:
                hit = True
                break
            pt = _word_tokens(pn or p)
            if pt and pt <= out:
                hit = True
                break
        if hit:
            for p in phrases:
                out |= _word_tokens(_normalize_match_text(p) or p)
    return out


def route_match_score(
    screen: str,
    checklist_item: str,
    synonym_groups: list[list[str]] | None = None,
    route_text: str | None = None,
) -> float:
    """0..1 overlap after synonym expansion, or a phrase-alias hit.

    Pack route screens rarely equal checklist text word-for-word. When a menu
    path is provided, the best of screen / path / combined is used so a path
    like ``Orders > Shipping Only Orders`` can still inform matching.
    """
    groups = synonym_groups or []
    sides = [screen or ""]
    if route_text:
        sides.append(route_text)
        sides.append(f"{screen or ''} {route_text}")
    best = 0.0
    item = checklist_item or ""
    for side in sides:
        a = expand_match_tokens(side, groups)
        b = expand_match_tokens(item, groups)
        sc = 0.0
        if a and b:
            inter = len(a & b)
            if inter > 0:
                sc = inter / float(min(len(a), len(b)))
        if _alias_phrase_hit(side, item, groups):
            sc = max(sc, 0.85)
        if sc > best:
            best = sc
    return best


def backfill_from_learned(
    pack_root,
    store: "DiscoveryMapStore | None",
    *,
    scope: str,
    env_fingerprint: str,
    platform: str | None = None,
    platform_version: str | None = None,
    warn=None,
) -> list[dict]:
    """Import confirmed ``## Routes`` lines from pack learned.md as verified map rows.

    Source is ``pack``. Every line is env-filtered (host/URL/email/id → skip + warn).
    Does not edit learned.md. Returns the upserted rows.
    """
    from . import learned as _learned

    warn = warn or (lambda _m: None)
    if store is None or not pack_root:
        return []
    routes = _learned.parse_confirmed_routes(pack_root, warn=warn)
    written: list[dict] = []
    for r in routes:
        try:
            row = store.upsert_route(
                scope=scope,
                area="general",
                app=r["app"],
                checklist_item=r["screen"],
                env_fingerprint=env_fingerprint or "pack",
                route_text=r["path"],
                key_labels=labels_from_route(r["path"]),
                platform=platform,
                platform_version=platform_version,
                status="verified",
                source="pack",
                last_success_at=_now(),
                success_count=1,
            )
            written.append(row)
        except EnvDetailError as e:
            warn(f"skipping Routes import ({e}): {r.get('screen')}")
        except Exception as e:  # noqa: BLE001
            warn(f"skipping Routes import ({type(e).__name__}): {r.get('screen')}")
    return written


def match_item_to_rows(
    rows: list[dict],
    checklist_item: str,
    *,
    synonym_groups: list[list[str]] | None = None,
    min_score: float = 0.5,
) -> dict | None:
    """Find a discovery_maps row for a checklist item.

    Exact item_key first, then synonym-aware overlap (core aliases + pack/platform
    ``route_synonyms``) against the screen name and the saved menu path.
    """
    ik = item_key(checklist_item)
    for r in rows:
        if r.get("item_key") == ik:
            return r
    best, score = None, 0.0
    words = {w for w in re.findall(r"[a-z0-9]{4,}", (checklist_item or "").lower()) if len(w) > 3}
    for r in rows:
        screen = r.get("checklist_item") or ""
        path = r.get("route_text") or ""
        sc = route_match_score(screen, checklist_item, synonym_groups, route_text=path)
        if sc <= 0 and not synonym_groups:
            # Legacy distinctive-word overlap when no synonym table is in play.
            rw = {w for w in re.findall(r"[a-z0-9]{4,}", screen.lower())}
            raw = len(words & rw)
            if raw >= 1:
                sc = raw / float(max(1, min(len(words) or 1, len(rw) or 1)))
                sc = max(sc, 0.5)
        if sc > score:
            best, score = r, sc
    return best if best is not None and score >= min_score else None


def route_hints_from_rows(rows: list[dict], *, limit: int = 16) -> list[str]:
    """Menu paths to hand the discovery tester as hints (even when a row is not reusable)."""
    out: list[str] = []
    seen: set[str] = set()
    for r in rows or []:
        path = (r.get("route_text") or "").strip()
        if not path or not route_is_savable(path, r.get("key_labels")):
            continue
        screen = (r.get("checklist_item") or "").strip()
        line = f"{screen}: {path}" if screen else path
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= limit:
            break
    return out


def parse_routes_from_discovery_notes(notes: str, checklist: list[str]) -> dict[str, tuple[str, list[str]]]:
    """Best-effort map checklist item -> (route_text, labels) from discovery notes / recipes.

    Only returns items with a concrete menu/label path (not guesses / blocked notes).
    """
    notes = notes or ""
    if re.search(r"(?i)no screens were mapped|unable to|could not reach|discovery failed|BLOCKED", notes):
        # Still allow per-line routes if present.
        pass
    out: dict[str, tuple[str, list[str]]] = {}
    lines = [ln.strip(" -*") for ln in notes.splitlines() if ln.strip()]
    for item in checklist:
        words = [w for w in re.findall(r"[a-z0-9]{4,}", item.lower()) if w not in
                 {"from", "with", "open", "list", "and", "create", "find", "prep"}]
        hit = None
        for ln in lines:
            low = ln.lower()
            if words and not any(w in low for w in words):
                continue
            if re.search(r"(?i)(click|menu|→|->|>|route:)", ln) or "'" in ln:
                try:
                    route = _safe_route(ln[:400])
                except EnvDetailError:
                    continue
                hit = (route, labels_from_route(route))
                break
        if hit:
            out[item] = hit
    return out
