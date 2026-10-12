"""Log in through a username/password form. The password is read from the env var named in pack.yaml and
typed by code; it never goes back to the model, the action log or the report."""
import asyncio
import json
import os
import re
from pathlib import Path

from agent_tester.helpers import HelperError, parse_snapshot

TEXT_ROLES = ("textbox", "searchbox")

HELPER = {"name": "login_form", "app": "any", "changes_data": False, "action_type": None,
          "description": "Log in to the current app with a pack account and check its role markers.",
          "params": {"account": "account key from pack.yaml, e.g. admin", "path": "optional login path"},
          "needs_config": []}

USER_LABELS = ["user_name", "Username", "User Name", "Email", "Email address", "Login", "User"]
PASS_LABELS = ["username_password", "Password"]
SUBMIT_LABELS = ["Log in", "Login", "Sign in", "Log In", "Sign In", "Submit"]


async def run(browser, ctx, account: str, path: str = ""):
    acc = ctx.account(account)
    env_name = acc.get("password_env")
    password = os.environ.get(env_name or "")
    if not password:
        raise HelperError(f"password not set: add {env_name} through the masked prompt")
    cfg = ctx.config or {}
    await browser.navigate(ctx.url(path or cfg.get("path", "")))
    await browser.snapshot()
    learned = _learned(ctx).get(ctx.app, {})
    user_el = await browser.find(([learned["user"]] if learned.get("user") else []) + list(cfg.get("user_label") or USER_LABELS), TEXT_ROLES, required=False)
    pass_el = await browser.find(([learned["password"]] if learned.get("password") else []) + list(cfg.get("password_label") or PASS_LABELS), TEXT_ROLES, required=False)
    if not (user_el and pass_el):
        # SuiteCRM-style forms (user_name / username_password / #bigbutton) often have unlabeled inputs:
        # fall back to the first two text boxes on the page.
        boxes = [e for e in parse_snapshot(browser.last_snapshot) if e.role in TEXT_ROLES]
        if len(boxes) < 2:
            raise HelperError("login form not found: no username/password fields on the page")
        user_el, pass_el = user_el or boxes[0], pass_el or boxes[1]
    await browser._do("browser_type", {"element": user_el.name or "username", "target": user_el.ref, "text": acc["username"]})
    await browser._do("browser_type", {"element": pass_el.name or "password", "target": pass_el.ref, "text": password,
                                       "_secret": True})
    btn = await browser.find(([learned["submit"]] if learned.get("submit") else []) + list(cfg.get("submit_label") or SUBMIT_LABELS), ("button", "link"), required=False)
    if btn:
        await browser._do("browser_click", {"element": btn.name, "target": btn.ref})
    else:                                     # no labelled button: submit with Enter
        await browser._do("browser_press_key", {"key": "Enter"})
    browser.last_snapshot = ""
    await asyncio.sleep(float(os.environ.get("LOGIN_WAIT", cfg.get("wait_seconds", 3))))
    page = await browser.snapshot()
    markers = acc.get("role_markers", [])
    still_on_form = (await browser.find(PASS_LABELS, ("textbox",), required=False) is not None
                     or "username_password" in page or "bigbutton" in page)
    role_ok = all(m.lower() in page.lower() for m in markers)
    shot = await browser.screenshot(f"{ctx.shot_prefix}login-{ctx.app}-{account}.png")
    if still_on_form:
        errs = [l.strip(" -") for l in page.splitlines() if re.search(r"(?i)invalid|incorrect|wrong|error|locked|failed", l)]
        seen = f"; page says: {errs[0][:150]}" if errs else "; no error text, the form just stayed (likely wrong fields)"
        raise HelperError(f"login failed for account {account} ({acc['username']}) using fields "
                          f"'{user_el.name}'/'{pass_el.name}'{seen}. Do not retry by hand.")
    _save_learned(ctx, {"user": user_el.name, "password": pass_el.name, "submit": btn.name if btn else ""})
    return {"values": {"logged_in": "yes" if not still_on_form else "no",
                       "role_ok": "yes" if role_ok else f"no (missing {', '.join(markers)})"},
            "screenshots": [shot], "notes": f"Logged in as {account}" if not still_on_form else "Login form still shown"}


def _learned_path(ctx) -> Path | None:
    d = getattr(getattr(ctx, "pack", None), "root", None)
    return Path(d) / "learned_logins.json" if d else None


def _learned(ctx) -> dict:
    p = _learned_path(ctx)
    try:
        return json.loads(p.read_text()) if p and p.exists() else {}
    except (OSError, ValueError):
        return {}


def _save_learned(ctx, fields: dict) -> None:
    """Remember which fields worked for this app so the next run uses them first (no secrets stored)."""
    p = _learned_path(ctx)
    if not p or not fields.get("user") or os.environ.get("LOGIN_LEARN") == "0":
        return
    data = _learned(ctx)
    if data.get(ctx.app) != fields:
        data[ctx.app] = fields
        try:
            p.write_text(json.dumps(data, indent=2))
        except OSError:
            pass
