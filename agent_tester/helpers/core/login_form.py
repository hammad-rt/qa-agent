"""Log in through a username/password form. The password is read from the env var named in pack.yaml and
typed by code; it never goes back to the model, the action log or the report."""
import asyncio
import os

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
    user_el = await browser.find(cfg.get("user_label") or USER_LABELS, TEXT_ROLES, required=False)
    pass_el = await browser.find(cfg.get("password_label") or PASS_LABELS, TEXT_ROLES, required=False)
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
    btn = await browser.find(cfg.get("submit_label") or SUBMIT_LABELS, ("button", "link"), required=False)
    if btn:
        await browser._do("browser_click", {"element": btn.name, "target": btn.ref})
    else:                                     # no labelled button: submit with Enter
        await browser._do("browser_press_key", {"key": "Enter"})
    browser.last_snapshot = ""
    await asyncio.sleep(float(cfg.get("wait_seconds", 3)))
    page = await browser.snapshot()
    markers = acc.get("role_markers", [])
    still_on_form = (await browser.find(PASS_LABELS, ("textbox",), required=False) is not None
                     or "username_password" in page or "bigbutton" in page)
    role_ok = all(m.lower() in page.lower() for m in markers)
    shot = await browser.screenshot(f"{ctx.shot_prefix}login-{ctx.app}-{account}.png")
    if still_on_form:
        raise HelperError(f"login failed for account {account} ({acc['username']}): the login form is still shown; "
                          "check the username/password, do not retry by hand")
    return {"values": {"logged_in": "yes" if not still_on_form else "no",
                       "role_ok": "yes" if role_ok else f"no (missing {', '.join(markers)})"},
            "screenshots": [shot], "notes": f"Logged in as {account}" if not still_on_form else "Login form still shown"}
