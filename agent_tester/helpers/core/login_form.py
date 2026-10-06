"""Log in through a username/password form. The password is read from the env var named in pack.yaml and
typed by code; it never goes back to the model, the action log or the report."""
import os

from agent_tester.helpers import HelperError

HELPER = {"name": "login_form", "app": "any", "changes_data": False, "action_type": None,
          "description": "Log in to the current app with a pack account and check its role markers.",
          "params": {"account": "account key from pack.yaml, e.g. admin", "path": "optional login path"},
          "needs_config": []}

USER_LABELS = ["Username", "User Name", "Email", "Email address", "Login", "User"]
PASS_LABELS = ["Password"]
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
    await browser.type(cfg.get("user_label") or USER_LABELS, acc["username"])
    await browser.type(cfg.get("password_label") or PASS_LABELS, password, secret=True)
    await browser.click(cfg.get("submit_label") or SUBMIT_LABELS, roles=("button", "link"))
    page = await browser.snapshot()
    markers = acc.get("role_markers", [])
    still_on_form = await browser.find(PASS_LABELS, ("textbox",), required=False) is not None
    role_ok = all(m.lower() in page.lower() for m in markers)
    shot = await browser.screenshot(f"{ctx.shot_prefix}login-{ctx.app}-{account}.png")
    return {"values": {"logged_in": "yes" if not still_on_form else "no",
                       "role_ok": "yes" if role_ok else f"no (missing {', '.join(markers)})"},
            "screenshots": [shot], "notes": f"Logged in as {account}" if not still_on_form else "Login form still shown"}
