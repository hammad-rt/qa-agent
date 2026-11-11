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
  // Pick the real submit button. Password show/hide eye icons and other toggles are buttons too; clicking one
  // leaves the form sitting there, which looked like a wrong password.
  const TOGGLE = /\b(show|hide|reveal|toggle|view)\b|eye|visib/i;
  const isToggle = b => b.hasAttribute('aria-pressed') || b.getAttribute('aria-controls') === pw.id ||
      TOGGLE.test([lab(b), b.getAttribute('title') || '', b.className || '', b.id || '', b.name || ''].join(' '));
  const cands = [...form.querySelectorAll('button, input[type=submit], input[type=button], input[type=image]')]
      .filter(b => vis(b) && (b.type || '').toLowerCase() !== 'reset' && !isToggle(b));
  const btn = cands.find(b => ['submit', 'image'].includes((b.type || '').toLowerCase())) ||
      cands.find(b => /log\s*in|sign\s*in|continue|submit|next/i.test(lab(b))) || null;
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
        old = os.environ.get(env_name)
        os.environ[env_name] = pw
        try:
            res = await _attempt(browser, ctx, account, path)
        except LoginRejected:
            if old is not None:
                os.environ[env_name] = old       # a wrong retry never replaces the saved password
            raise
        write_env_secret(env_name, pw)           # saved only once it actually logged in
        return res


_LOGOUT_CTRL = re.compile(r"(?i)\b(log\s*out|sign\s*out|signout)\b")
# Interactive a11y roles that can be a signed-in-only control (user menu, logout, role link).
_SIGNED_IN_ROLES = (
    "button", "link", "menuitem", "menuitemradio", "menuitemcheckbox",
    "tab", "option", "switch",
)


def _control_name_matches(name: str, marker: str) -> bool:
    """Exact or word-boundary match on a control's accessible name (not free text)."""
    n, m = (name or "").strip(), (marker or "").strip()
    if not n or not m:
        return False
    if n.casefold() == m.casefold():
        return True
    return bool(re.search(rf"(?i)\b{re.escape(m)}\b", n))


def _snapshot_controls(page: str):
    return [e for e in parse_snapshot(page or "") if e.role in _SIGNED_IN_ROLES]


def _markers_on_controls(page: str, markers: list) -> bool:
    markers = [str(m).strip() for m in (markers or []) if str(m).strip()]
    if not markers:
        return False
    controls = _snapshot_controls(page)
    return all(any(_control_name_matches(e.name, m) for e in controls) for m in markers)


def _signed_in_evidence(page: str, markers: list) -> str | None:
    """Why the accessibility tree shows a signed-in session, or None if unsure."""
    controls = _snapshot_controls(page)
    if any(_LOGOUT_CTRL.search(e.name or "") for e in controls):
        return "logout control"
    if _markers_on_controls(page, markers):
        return "role markers"
    return None


async def _session_already_valid(browser, ctx, account: str, acc: dict) -> dict | None:
    """Skip login only with positive evidence of a signed-in session.

    A public / signed-out home often has no password field — that alone is not enough.
    A prior ``login_form.logged_in`` flag alone is also not enough if the page shows a
    login form or no signed-in control. Evidence is a signed-in-only control in the
    accessibility tree: a logout / sign-out control, or every pack ``role_markers``
    name as an exact / word-boundary control (not free-text substring). When unsure,
    return None so ``_attempt`` navigates to the login path.
    """
    try:
        form = await analyze(browser)
    except Exception:  # noqa: BLE001
        return None
    if form.get("has_password"):
        return None  # signed out / on a login form
    page = ""
    try:
        page = await browser.snapshot() or ""
    except Exception:  # noqa: BLE001
        page = ""
    markers = [m for m in (acc.get("role_markers") or []) if str(m).strip()]
    how = _signed_in_evidence(page, markers)
    if not how:
        return None
    markers_ok = _markers_on_controls(page, markers)
    return {"values": {"logged_in": "yes",
                       "role_ok": "yes" if markers_ok else f"skipped ({how})"},
            "screenshots": [],
            "notes": f"Already logged in on {ctx.app}; skipped login_form ({how})"}


async def _attempt(browser, ctx, account: str, path: str = ""):
    acc = ctx.account(account)
    env_name = acc.get("password_env")
    password = os.environ.get(env_name or "")
    if not password:
        raise HelperError(f"password not set: add {env_name} through the masked prompt")
    cfg = ctx.config or {}
    # Check the current session first — do not navigate to /login when already signed in.
    already = await _session_already_valid(browser, ctx, account, acc)
    if already is not None:
        return already
    # If we are already on a login form, fill it in place (no extra navigate).
    form = await analyze(browser)
    if not form.get("has_password"):
        await browser.navigate(ctx.url(path or cfg.get("path", "")))
        form = await analyze(browser)
    if not form.get("has_password"):
        # After navigating to the login path, skip only if a signed-in control is now visible.
        page = ""
        try:
            page = await browser.snapshot() or ""
        except Exception:  # noqa: BLE001
            page = ""
        how = _signed_in_evidence(page, acc.get("role_markers") or [])
        if how:
            markers_ok = _markers_on_controls(page, acc.get("role_markers") or [])
            return {"values": {"logged_in": "yes",
                               "role_ok": "yes" if markers_ok else f"skipped ({how})"},
                    "screenshots": [],
                    "notes": f"Already logged in on {ctx.app} (shared session); no login needed ({how})"}
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
    role_ok = (not markers) or _markers_on_controls(page, markers)
    # Basename only — McpBrowser rewrites under evidence_dir / run shots/ (never repo cwd).
    shot = await browser.screenshot(f"login-{ctx.app}-{_account_role_key(ctx, account)}.png")
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


_SAFE_ROLE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,40}$")


def _account_role_key(ctx, account: str) -> str:
    """Pack account role/key (admin, customer). Never an email or other identity."""
    raw = (account or "").strip()
    accounts = {}
    pack = getattr(ctx, "pack", None)
    app = getattr(ctx, "app", "") or ""
    if pack is not None and hasattr(pack, "accounts"):
        try:
            accounts = pack.accounts(app) or {}
        except Exception:  # noqa: BLE001
            accounts = {}
    if raw in accounts:
        return raw
    for key in accounts:
        if str(key).lower() == raw.lower():
            return str(key)
    want = raw.lower()
    for key, a in accounts.items():
        if str((a or {}).get("username") or "").lower() == want:
            return str(key)
    plan = (getattr(getattr(ctx, "state", None), "data", None) or {}).get("plan") or {}
    for r in plan.get("requirements") or []:
        k = str(r.get("key") or "")
        if k.startswith(f"account.{app}.") and str(r.get("value") or "").lower() == want:
            return k.split(".", 2)[2]
    if raw and _SAFE_ROLE_KEY.fullmatch(raw) and "@" not in raw:
        return raw
    return "account"


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
