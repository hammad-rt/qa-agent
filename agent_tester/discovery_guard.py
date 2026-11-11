"""Code-enforced read-only rules for discovery + admin/system action gates.

Discovery may map where an admin/system page or link is (read labels/URLs) but must never
click the action that runs it, and must never request_approval / check_repair_sql for
admin kinds. Only a selected case whose own steps explicitly ask for that admin action
may do it — behind the normal approval gate — and never on preprod/production unless the
pack/env config allows it.
"""
from __future__ import annotations

import os
import re

# Action types that are admin/system (never during discovery; gated in cases).
ADMIN_ACTION_TYPES = frozenset({
    "admin_repair",
    "execute_repair_sql",
    "repair_sql_destructive",
    "admin_system",
    "cache_clear",
    "module_loader",
    "studio_deploy",
    "scheduler_run",
    "execute_other",  # Execute on repair/admin pages
})

# Labels/buttons that RUN an admin/system action (click denied in discovery).
# Navigating Admin / Repair menus to *see* the link is fine; clicking these runs it.
ADMIN_ACTION_CLICK = re.compile(
    r"(?i)\b("
    r"quick\s*repair\s*(?:and\s*)?rebuild|"
    r"rebuild\s+extensions?|"
    r"repair\s+and\s+rebuild|"
    r"execute(?:\s+sql)?|"
    r"run\s+quick\s*repair|"
    r"clear\s+(?:the\s+)?(?:cache|vardefs?|opcache)|"
    r"rebuild\s+(?:js(?:\s+files)?|sprites?|relationships?|dashlets|config(?:\s+file)?|"
    r"\.htaccess|schedulers?|workflows?|cache)|"
    r"module\s+loader|"
    r"install\s+(?:module|package)|"
    r"(?:studio|module\s+builder).{0,20}deploy|"
    r"deploy\s+(?:module|package|studio)|"
    r"run\s+schedulers?|"
    r"execute\s+(?:job|scheduler)|"
    r"sync\s+vardefs?|"
    r"repair\s+roles|"
    r"rebuild\s+roles"
    r")\b"
)

# Checklist / step phrases that name an admin action (seed only when a case step says so).
ADMIN_CHECKLIST_HINT = re.compile(
    r"(?i)\b("
    r"quick\s*repair|repair\s+and\s+rebuild|rebuild\s+extensions?|"
    r"clear\s+cache|module\s+loader|studio.{0,12}deploy|run\s+schedulers?|"
    r"execute\s+sql|admin_repair|check_repair_sql"
    r")\b"
)

# Default envs that may run admin repair when a case asks (preprod/prod refused).
_DEFAULT_ADMIN_REPAIR_ENVS = ("staging", "local")


def admin_repair_envs(pack_cfg: dict | None = None) -> set[str]:
    """Envs where case-asked admin repair / execute SQL is allowed."""
    cfg = pack_cfg or {}
    raw = cfg.get("admin_repair_envs")
    if raw is None:
        env_s = os.environ.get("ALLOW_ADMIN_REPAIR_ENVS", "")
        raw = [x.strip() for x in env_s.split(",") if x.strip()] if env_s else list(_DEFAULT_ADMIN_REPAIR_ENVS)
    out = set()
    for x in raw:
        x = str(x).strip().lower().replace("_", "-")
        if x in ("pre-prod", "pre_prod"):
            x = "preprod"
        if x in ("production",):
            x = "prod"
        out.add(x)
    return out


def normalize_env_name(env: str | None) -> str:
    e = (env or "").strip().lower().replace("_", "-")
    if e in ("pre-prod", "pre_prod"):
        return "preprod"
    if e in ("production",):
        return "prod"
    return e


def is_admin_action_type(action_type: str | None) -> bool:
    return bool(action_type) and action_type in ADMIN_ACTION_TYPES


def is_admin_action_label(label: str | None) -> bool:
    return bool(label) and bool(ADMIN_ACTION_CLICK.search(label or ""))


def is_admin_checklist_item(text: str | None) -> bool:
    return bool(text) and bool(ADMIN_CHECKLIST_HINT.search(text or ""))


# Real submits only (app-agnostic). Packs add product copy via ``discovery_submit_labels``.
# Intentionally NOT matching bare create / apply / send / confirm — discovery must open Create
# menus and use filter Apply/Search. "confirm payment" is a real submit; bare "Confirm" is not.
DISCOVERY_SUBMIT_CLICK = re.compile(
    r"(?i)\b("
    r"submit|save|"
    r"pay(?:\s+now)?|place\s+order|checkout|"
    r"confirm\s+payment|request\s+quote|"
    r"update\s+record"
    r")\b"
)


def discovery_submit_extras(pack_cfg: dict | None) -> list[str]:
    """Product-specific submit labels from pack.yaml ``discovery_submit_labels``."""
    raw = (pack_cfg or {}).get("discovery_submit_labels") or []
    return [str(x).strip() for x in raw if str(x).strip()]


def is_discovery_submit_label(label: str | None, pack_cfg: dict | None = None) -> bool:
    """True when the click would submit/pay/request — not open a form or run a filter."""
    if not label:
        return False
    low = label.lower()
    # Pack product copy first (e.g. Savostore "request a purchase quote").
    for x in discovery_submit_extras(pack_cfg):
        if x.lower() in low:
            return True
    return bool(DISCOVERY_SUBMIT_CLICK.search(label))


def discovery_blocks_click(label: str | None, url: str | None = None,
                           pack_cfg: dict | None = None) -> str | None:
    """If discovery must not click this control, return a short reason; else None.

    Mapping Admin / Repair menus is allowed. Clicking the action that *runs* repair /
    execute / cache clear / module loader / studio deploy / scheduler is denied.
    Real submits (save/pay/request quote) are denied; Create menus and filter Apply/Search are not.
    Pack ``discovery_submit_labels`` adds product-specific submit copy.
    """
    if is_discovery_submit_label(label, pack_cfg):
        return (
            f"discovery is read-only: do not click '{(label or '')[:80]}' "
            f"(submit/pay/request). Open the form or use filters to map labels if needed, "
            f"then go back — never submit."
        )
    if is_admin_action_label(label):
        return (
            f"discovery is read-only: do not click '{(label or '')[:80]}' "
            f"(admin/system action). Note where the link/button is, then finish. "
            f"Only a case whose steps ask for this action may run it later."
        )
    # Deep-link URLs that *invoke* repair/execute (not just the Admin index).
    u = (url or "").lower()
    if u and re.search(
        r"(?:action=)?(?:repair|quickrepair|rebuild|clearcache|moduleloader|"
        r"executerepair|runschedule)|entryPoint=export",
        u,
    ) and re.search(r"(?:repair|rebuild|clear|execute|moduleloader|scheduler)", u):
        # Allow reading Administration index; block action= endpoints that run work.
        if "action=index" in u or "module=administration&action=index" in u:
            return None
        if re.search(r"action=(?:repair|quickrepair|clear|rebuild|moduleloader|run)", u):
            return (
                "discovery is read-only: do not navigate to an admin action URL that runs "
                "repair/rebuild/cache/module-loader. Note the menu path only."
            )
    return None


def discovery_blocks_tool(tool: str, args: dict | None = None) -> str | None:
    """Block discovery from admin tools that mutate or prepare mutation."""
    args = args or {}
    if tool == "check_repair_sql":
        return ("discovery is read-only: check_repair_sql is for a case that runs Quick Repair; "
                "map where Repair lives, do not classify or execute SQL.")
    if tool == "request_approval":
        at = args.get("action_type") or ""
        if is_admin_action_type(at) or is_admin_action_label(str(args.get("description") or "")):
            return (f"discovery is read-only: cannot request_approval for admin/system action "
                    f"{at!r}. Only a case step that asks for it may approve and run it.")
        # Any approval during discovery is suspicious; deny admin-ish descriptions.
        desc = str(args.get("description") or "")
        if ADMIN_CHECKLIST_HINT.search(desc):
            return ("discovery is read-only: cannot request_approval for repair/admin actions. "
                    "Note the path only.")
    return None


def case_allows_admin_action(brief: dict, action_type: str | None = None,
                             label: str | None = None) -> bool:
    """True when the current case's own steps explicitly ask for this admin action."""
    if brief.get("discovery") or brief.get("read_only"):
        return False
    steps = brief.get("steps") or []
    # Match action_type on a step, or step text naming the admin action.
    for st in steps:
        if not isinstance(st, dict):
            continue
        st_at = st.get("action_type") or ""
        text = st.get("text") or ""
        if action_type and st_at and st_at == action_type and is_admin_action_type(st_at):
            return True
        if action_type and is_admin_action_type(action_type) and ADMIN_CHECKLIST_HINT.search(text):
            return True
        if label and is_admin_action_label(label) and (
                is_admin_action_type(st_at) or ADMIN_CHECKLIST_HINT.search(text)
                or (label.lower()[:20] in text.lower() if label else False)):
            return True
    # Title-level: "Run Quick Repair and Rebuild..."
    title = brief.get("title") or ""
    if (action_type and is_admin_action_type(action_type) and ADMIN_CHECKLIST_HINT.search(title)) or (
            label and is_admin_action_label(label) and ADMIN_CHECKLIST_HINT.search(title)):
        return True
    return False


def admin_action_env_allowed(env: str | None, pack_cfg: dict | None = None,
                             action_type: str | None = None) -> bool:
    """Preprod/production refuse admin repair unless pack/env allowlist includes them."""
    e = normalize_env_name(env)
    allowed = admin_repair_envs(pack_cfg)
    if e in allowed:
        return True
    # Destructive repair never on preprod/prod even if somehow listed — still require allowlist.
    if action_type == "repair_sql_destructive" and e not in ("staging", "local"):
        return e in allowed
    return False


def refuse_admin_reason(env: str | None, action_type: str | None = None,
                        label: str | None = None) -> str:
    e = normalize_env_name(env) or "this environment"
    what = action_type or (label or "admin action")
    return (f"admin/system action {what!r} is not allowed on {e} "
            f"(only on {', '.join(sorted(admin_repair_envs()))} unless pack "
            f"admin_repair_envs / ALLOW_ADMIN_REPAIR_ENVS says otherwise)")


def filter_discovery_checklist(items: list[str]) -> list[str]:
    """Drop or rewrite checklist lines that would push discovery to *run* admin actions.

    Admin actions are only seeded when a case step names them — and even then discovery
    maps location ('locate, do not run') rather than performing the action.
    """
    out: list[str] = []
    for raw in items or []:
        if not is_admin_checklist_item(raw):
            out.append(raw)
            continue
        # Soften to a locate-only goal so discovery notes the path without clicking run.
        soft = re.sub(r"(?i)\b(click|run|execute|perform|start)\b", "locate", raw)
        if not soft.lower().startswith("locate"):
            soft = f"locate (do not run): {raw}"
        else:
            soft = f"locate (do not run): {soft}"
        if soft not in out:
            out.append(soft[:120])
    return out
