"""Log in through a username/password form. The password is read from the env var named in pack.yaml and
typed by code; it never goes back to the model, the action log or the report."""
import asyncio
import getpass
import sys
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

# Read-only page analysis: finds the real login form from the DOM (input types, names, labels), never guesses.
# "idx" is the field's position among visible text-like inputs, which is the order the snapshot lists textboxes.
ANALYZE_JS = r"""() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const TEXTLIKE = ['', 'text', 'email', 'password', 'tel', 'search', 'url', 'number'];
  const boxes = [...document.querySelectorAll('input, textarea')].filter(e =>
      vis(e) && (e.tagName === 'TEXTAREA' || TEXTLIKE.includes((e.getAttribute('type') || '').toLowerCase())));
  const lab = e => ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') ||
      e.placeholder || e.value || e.innerText || '').trim().slice(0, 60);
  const desc = e => e && {idx: boxes.indexOf(e), name: e.name || '', id: e.id || '', type: e.type || '', label: lab(e)};
  const pw = boxes.find(e => (e.type || '').toLowerCase() === 'password');
  const errText = [...document.querySelectorAll('.error, .alert, [role=alert], #login_error, .login_error, .errors')]
      .filter(vis).map(e => e.innerText.trim()).filter(Boolean).join(' | ').slice(0, 200);
  if (!pw) return JSON.stringify({has_password: false, error: errText});
  const form = pw.form || document;
  const before = boxes.filter(e => e !== pw && (form === document || e.form === form) &&
      (e.compareDocumentPosition(pw) & Node.DOCUMENT_POSITION_FOLLOWING));
  const user = before[before.length - 1] || null;
  const btn = [...form.querySelectorAll('button, input[type=submit], input[type=button], input[type=image]')]
      .filter(vis).find(b => (b.type || '').toLowerCase() !== 'reset') || null;
  return JSON.stringify({has_password: true, user: desc(user), password: desc(pw),
      submit: btn && {id: btn.id || '', name: btn.name || '', label: lab(btn)}, error: errText});
}"""


CAPTCHA_JS = r"""async (page) => {
  const anchor = page.frames().find(f => /recaptcha\/(api2|enterprise)\/anchor/.test(f.url()));
  if (!anchor) return JSON.stringify({present: false});
  const box = anchor.locator('#recaptcha-anchor');
  if ((await box.getAttribute('aria-checked')) !== 'true') await box.click({timeout: 5000});
  let passed = false, challenge = false;
  for (let i = 0; i < 16 && !passed && !challenge; i++) {
    await page.waitForTimeout(500);
    passed = (await box.getAttribute('aria-checked')) === 'true';
    const b = page.frames().find(f => /recaptcha\/(api2|enterprise)\/bframe/.test(f.url()));
    if (b) { const el = await b.frameElement(); challenge = await el.isVisible(); }
  }
  return JSON.stringify({present: true, passed, challenge});
}"""


class CaptchaNeedsPerson(HelperError):
    """reCAPTCHA showed a picture puzzle (or the checkbox would not tick): only then does a person step in."""


async def solve_checkbox(browser) -> dict:
    """Tick the "I'm not a robot" box the way a person would. Returns {present, passed, challenge}."""
    if os.environ.get("CAPTCHA_AUTO", "1") == "0":
        return {}
    try:
        return parse_result(await browser._do("browser_run_code_unsafe", {"code": CAPTCHA_JS}))
    except Exception:  # noqa: BLE001  (tool missing / page changed: treat as "could not tick")
        return {"present": True, "passed": False, "challenge": False}


async def analyze(browser) -> dict:
    out = await browser._do("browser_evaluate", {"function": ANALYZE_JS})
    return parse_result(out)


def parse_result(out: str) -> dict:
    """Playwright MCP replies '### Result' + a JSON-encoded string, then echoes the code (which has braces too)."""
    text = out or ""
    m = re.search(r"### Result\s*\n(.+)", text)
    raw = (m.group(1) if m else text).strip()
    try:
        v = json.loads(raw)
        v = json.loads(v) if isinstance(v, str) else v
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _pick(boxes, field: dict):
    """Map a DOM field to its snapshot ref by its position among textboxes (same DOM order)."""
    i = (field or {}).get("idx", -1)
    return boxes[i] if 0 <= i < len(boxes) else None


class LoginRejected(HelperError):
    """The form was filled and submitted but the site kept showing it (wrong password, LDAP error, ...)."""


async def run(browser, ctx, account: str, path: str = ""):
    try:
        return await _attempt(browser, ctx, account, path)
    except LoginRejected as e:
        env_name = ctx.account(account).get("password_env")
        from agent_tester.auto import web_mode, secret_input
        if not (env_name and sys.stdin and (sys.stdin.isatty() or web_mode())):
            raise
        # Ask the person for the password again (masked) and retry once before giving up.
        print(f"\n{e}\nThe saved password may be wrong.")
        pw = secret_input(f"Re-enter the password for {ctx.account(account)['username']} ({env_name}), "
                             "or press Enter to skip: ")
        if not pw:
            raise
        from agent_tester.config import write_env_secret
        write_env_secret(env_name, pw)
        os.environ[env_name] = pw
        return await _attempt(browser, ctx, account, path)


async def _attempt(browser, ctx, account: str, path: str = ""):
    acc = ctx.account(account)
    env_name = acc.get("password_env")
    password = os.environ.get(env_name or "")
    if not password:
        raise HelperError(f"password not set: add {env_name} through the masked prompt")
    cfg = ctx.config or {}
    await browser.navigate(ctx.url(path or cfg.get("path", "")))
    form = await analyze(browser)
    if not form.get("has_password"):
        raise HelperError("no login form on this page (no visible password field); if you are already logged in, "
                          "skip the login step")
    if not form.get("user"):
        raise HelperError("the login form has a password field but no username field before it")
    snap = await browser.snapshot()
    boxes = [e for e in parse_snapshot(snap) if e.role in TEXT_ROLES]
    user_el, pass_el = _pick(boxes, form["user"]), _pick(boxes, form["password"])
    if not (user_el and pass_el):
        raise HelperError(f"login fields found in the page ({_fdesc(form['user'])}, {_fdesc(form['password'])}) "
                          "but not in the snapshot")
    await browser._do("browser_type", {"element": user_el.name or _fdesc(form["user"]), "target": user_el.ref,
                                       "text": acc["username"]})
    await browser._do("browser_type", {"element": pass_el.name or _fdesc(form["password"]), "target": pass_el.ref,
                                       "text": password, "_secret": True})
    cap = await solve_checkbox(browser)
    if cap.get("present") and not cap.get("passed") and cap.get("challenge"):
        from agent_tester.captcha import solve as solve_images   # vision model picks the tiles; staging/pre-prod only
        if await solve_images(browser, getattr(ctx, "env", "staging")):
            cap["passed"] = True
    if cap.get("present") and not cap.get("passed"):
        why = "showed a picture puzzle" if cap.get("challenge") else "did not tick when clicked"
        raise CaptchaNeedsPerson(f"the reCAPTCHA {why}; a person has to solve it. Call needs_manual_login.")
    sub = form.get("submit") or {}
    btn = await browser.find(sub["label"], ("button", "link"), required=False) if sub.get("label") else None
    if btn:
        await browser._do("browser_click", {"element": btn.name, "target": btn.ref})
    else:                                     # the form has no labelled button: submit it with Enter
        await browser._do("browser_press_key", {"key": "Enter"})
    browser.last_snapshot = ""
    await asyncio.sleep(float(os.environ.get("LOGIN_WAIT", cfg.get("wait_seconds", 3))))
    after = await analyze(browser)
    page = await browser.snapshot()
    markers = acc.get("role_markers", [])
    still_on_form = bool(after.get("has_password"))
    role_ok = all(m.lower() in page.lower() for m in markers)
    shot = await browser.screenshot(f"{ctx.shot_prefix}login-{ctx.app}-{account}.png")
    used = f"{_fdesc(form['user'])} / {_fdesc(form['password'])} / submit {_fdesc(sub) if sub else 'Enter'}"
    if still_on_form:
        seen = (f"the page says: {after['error']}" if after.get("error")
                else "no error message, the form just stayed")
        raise LoginRejected(f"login failed for {account} ({acc['username']}) using {used}; {seen}. "
                          "Do not retry by hand.")
    _save_learned(ctx, {"user": _fdesc(form["user"]), "password": _fdesc(form["password"]),
                        "submit": _fdesc(sub) if sub else "Enter"})
    return {"values": {"logged_in": "yes",
                       "role_ok": "yes" if role_ok else f"no (missing {', '.join(markers)})"},
            "screenshots": [shot], "notes": f"Logged in as {account} using {used}"}


def _fdesc(f: dict | None) -> str:
    f = f or {}
    return f.get("name") or f.get("id") or f.get("label") or "?"


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
    """Record the login form that worked for this app (field names only, no secrets) in the knowledge files."""
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
