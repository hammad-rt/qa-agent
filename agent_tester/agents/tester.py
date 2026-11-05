"""Per-app tester (LLM + Playwright MCP). Every tool call goes through code callbacks that:
build the action log, enforce the domain allowlist and approvals, cap tool calls, mask page text,
and collect values and evidence. The StepResult is assembled by code, not by the model."""
from __future__ import annotations

import asyncio
import json
import re

from . import raw_calls
from urllib.parse import urlparse
import os
import sys
import time
import logging
log = logging.getLogger(__name__)
from pathlib import Path

from ..config import Settings
from ..models import StepResult
from ..redact import redact
from ..run_state import RunState
from ..tools import values as V
from ..tools.policy import Policy, PolicyViolation
from ..tools.screenshots import Evidence
from . import prompts
from .llm import model, run_agent

READ_ONLY_BROWSER = {"browser_snapshot", "browser_take_screenshot", "browser_wait_for", "browser_tab_list",
                     "browser_console_messages", "browser_network_requests", "browser_navigate_back", "browser_hover"}
BLOCKED_BROWSER = {"browser_evaluate", "browser_run_code", "browser_install", "browser_file_upload"}


# Calls that only read or record (they don't move the browser) don't use up the action budget.
FREE_TOOLS = {"browser_snapshot", "browser_take_screenshot", "browser_wait_for", "browser_find",
              "browser_console_messages", "browser_network_requests", "mark_step", "record_value", "evidence_name",
              "finish", "needs_manual_login", "recall_lessons", "propose_memory", "get_secret",
              "note_behaviour", "lookup_docs", "browser_tabs"}
# Real paste: clipboard + Ctrl+V when the browser allows it, else a paste event with the text (both run the
# page's paste handler; typing does not). Reads the field back after the page reacts.
PASTE_JS = r"""async (page) => {
  const el = page.locator('aria-ref=' + __REF__);
  const text = __TEXT__;
  await el.click();
  await el.fill('');
  let how = 'clipboard';
  try {
    await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.evaluate(t => navigator.clipboard.writeText(t), text);
    await el.press('ControlOrMeta+V');
    await page.waitForTimeout(300);
    if ((await el.inputValue()) === '' && text !== '') throw new Error('empty after paste');
  } catch (e) {
    how = 'paste-event';
    await el.fill('');
    await el.evaluate((node, t) => {
      const dt = new DataTransfer(); dt.setData('text/plain', t);
      const ev = new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true});
      if (node.dispatchEvent(ev)) {           // page did not handle it: insert like the browser would
        node.focus(); document.execCommand('insertText', false, t);
      }
    }, text);
  }
  await page.waitForTimeout(400);
  return JSON.stringify({value: await el.inputValue(), how});
}"""
_SAVE = re.compile(r"\b(save|submit|update|apply|confirm)\b", re.I)
RELOAD_HINT = ("You already saved/searched here and the expected message did not appear. Do not save or search "
               "again. Check the real result: reload the page (browser_navigate to the same address, or reopen it by "
               "its route), read the stored value with browser_snapshot and record_value it, then mark_step. "
               "\"No message and the old value is back after a reload\" is a valid result (silent rejection); "
               "call note_behaviour with it and continue. If the action opened another tab, use browser_tabs to "
               "list and select it, read it, then select the first tab again.")
ACTIONS_PER_STEP = int(os.environ.get("TESTER_ACTIONS_PER_STEP", "30"))   # actions allowed without step progress
HARD_STOP_AFTER = 4                  # calls allowed after a stop/limit error before code ends the turn itself
_AFTER_STOP_OK = {"finish", "record_value", "mark_step", "evidence_name", "note_behaviour", "propose_memory"}


class TesterHardStop(Exception):
    """Raised from before_tool when the model ignores a stop: ends the tester turn without another model call."""


def _hard_stop_in(e: BaseException) -> bool:
    """ADK/anyio may wrap the exception (ExceptionGroup, __cause__); find it anywhere in the chain."""
    seen = set()
    stack = [e]
    while stack:
        x = stack.pop()
        if x is None or id(x) in seen:
            continue
        seen.add(id(x))
        if isinstance(x, TesterHardStop):
            return True
        stack += [x.__cause__, x.__context__, *getattr(x, "exceptions", ())]
    return False


LOOP_WARN, LOOP_STOP = 3, 5          # same call repeated this often without progress: warn, then stop
IDLE_READS = 8                       # page reads in a row with nothing done in between: stalled, not working
EXPLORE_ACTIONS = 15                 # extra actions for one exploration of a screen when a step is stuck
EXPLORE_TEXT = (
    "STOP guessing: {why}. Explore before you act again ({n} more actions for this):\n"
    "1. browser_snapshot and list for yourself every menu, tab, button, link, dropdown (with its options) and "
    "field on this screen that could reach the step's goal. Check the other tabs too (browser_tabs).\n"
    "2. Look where the needed field, status or action really lives: the record's detail and edit views, its "
    "subpanels and related records, list filters and search, the actions menu, and the app's documentation "
    "(lookup_docs).\n"
    "3. Pick a route you have NOT tried. Already tried: {tried}. After each action, re-read the page and check "
    "what actually changed before the next one.\n"
    "4. Save each thing you learn with note_behaviour (e.g. where a field is set, the order statuses must follow, "
    "what a save rejects silently), so later cases and runs do not have to find it again.\n"
    "Then do the step. If the screen has no way to do it, finish(notes, stuck=True) with what you found.")


def _is_cycle(recent: list[str]) -> bool:
    """The last calls repeat a short pattern (A B A B A B, or A B C A B C) - a loop even if no single call repeats
    often enough on its own."""
    for period in (2, 3, 4):
        need = period * 3
        if len(recent) >= need:
            tail = recent[-need:]
            if len(set(tail)) > 1 and all(tail[i] == tail[i % period] for i in range(need)):
                return True
    return False


ACTING_TOOLS = {"browser_click", "browser_type", "browser_fill_form", "browser_select_option", "browser_press_key",
                "browser_navigate", "browser_navigate_back", "browser_drag", "browser_drop", "browser_file_upload",
                "browser_handle_dialog", "run_helper"}


def _label_of(ref: str, snapshot: str) -> str:
    """The visible name of an element ref in the last snapshot ('- button "Save" [ref=e12]' -> 'button Save')."""
    if not ref or not snapshot:
        return ""
    for line in snapshot.splitlines():
        if f"ref={ref}]" in line or f"ref={ref} " in line:
            m = re.match(r'\s*-\s*([\w-]+)\s*(?:"([^"]*)")?', line)
            return " ".join(x for x in (m.group(1), m.group(2) or "") if x).strip().lower() if m else ""
    return ""


def _intent_sig(name: str, args: dict, snapshot: str = "") -> str | None:
    """What the call is aimed at, ignoring wording: a click on the same element (by its label, since refs change
    with every snapshot), or a script quoting the same texts."""
    if name == "browser_click":
        ref = re.sub(r"\W|ref", "", str(args.get("target") or args.get("ref") or ""))
        label = _label_of(ref, snapshot) or ("" if ref else str(args.get("element") or "").strip().lower())
        if label:
            return f"click:{label}"
        return f"click:{ref}" if ref else None
    if name in ("browser_run_code_unsafe", "browser_evaluate"):
        texts = sorted(set(re.findall(r"""['"]([^'"]{3,60})['"]""", str(args.get("code") or args.get("function") or ""))))
        return "code:" + "|".join(texts) if texts else None
    return None


def _settle_seconds(app: str = "", pack=None) -> float:
    """Seconds to let a page settle after an action before reading it: TESTER_SETTLE_SECONDS_<APP>, else the
    pack's apps.<app>.settle_seconds (slow back-office screens), else TESTER_SETTLE_SECONDS (default 4)."""
    app = app if isinstance(app, str) else ""
    name = re.sub(r"\W", "_", app).upper()
    env = os.environ.get("TESTER_SETTLE_SECONDS_" + name) if app else None
    if env:
        return float(env)
    apps = getattr(pack, "apps", None) if pack is not None else None
    conf = apps.get(app) if isinstance(apps, dict) else None
    if isinstance(conf, dict) and isinstance(conf.get("settle_seconds"), (int, float)):
        return float(conf["settle_seconds"])
    return float(os.environ.get("TESTER_SETTLE_SECONDS", "4"))


RAW_TOOL_CALL = re.compile(r"<\|?tool_call\|?>|call:\w+\(")


def nudge_text(final: str) -> str:
    """The model ended its turn without finish() -- often by writing a tool call as plain text
    (Gemma's `<|tool_call>call:name(...)` syntax) instead of a real function call. Ask it to go on."""
    why = ("Your last message wrote a tool call as text, so it was not run."
           if RAW_TOOL_CALL.search(final or "") else "You stopped without calling finish().")
    return (why + " Continue from where you are using real function calls only (never write call syntax as text). "
            "When the steps are done or you cannot go on, call finish().")


def _call_sig(name: str, args: dict) -> str:
    try:
        return name + json.dumps({k: v for k, v in (args or {}).items() if k != "element"}, sort_keys=True,
                                 default=str)
    except (TypeError, ValueError):
        return name + str(args)


def tool_budget(n_steps: int, floor: int = 40, cap: int = 120, per_step: int = 20) -> int:
    """Per-case browser/tool call budget: floor from env, scales with steps, hard cap."""
    return max(floor, min(cap, per_step * max(int(n_steps), 1)))


def _is_xsrf_page(text: str) -> bool:
    """True when SuiteCRM (or similar) shows a Cross-Site Request Forgery / referer block page."""
    t = (text or "").lower()
    return "cross site request forgery" in t or "acceptable referer" in t


def _url_nav_key(url: str) -> str:
    """Normalize path+query so absolute and relative admin deep-links compare equal."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://local/" + raw.lstrip("/")
    u = urlparse(raw)
    path = u.path or "/"
    q = u.query or ""
    return f"{path}?{q}" if q else path


def run_urls(pack, env: str, state) -> dict:
    """app -> base URL for this run: the URL resolved at the plan gate wins over pack.yaml (pre-prod etc.)."""
    urls = {a: pack.url(a, env) for a in pack.apps if pack.url(a, env)}
    plan = (getattr(state, "data", None) or {}).get("plan") or {}
    for r in plan.get("requirements", []):
        parts = (r.get("key") or "").split(".")
        if len(parts) >= 3 and parts[0] == "env" and parts[1] == env and r.get("value"):
            urls[parts[2]] = r["value"]
    return urls


def browser_env() -> dict:
    """The MCP client passes only a few env vars (PATH, HOME...) to the browser server by default. Without DISPLAY /
    WAYLAND_DISPLAY Playwright silently runs headless, so a "visible" login window never appears."""
    return dict(os.environ)


def headed() -> bool:
    return os.environ.get("QA_HEADED", "") in ("1", "true", "yes")


def browser_home(run_dir: Path) -> Path:
    """Browser logins are shared by every run on one environment (only one run goes at a time), so a portal
    login + CAPTCHA done for one task is reused by the next. Staging and production never share a session.
    QA_BROWSER_DIR overrides the base folder (default: runs/.browser)."""
    env = "staging"
    try:
        data = json.loads((run_dir / "state.json").read_text())
        env = (data.get("plan") or {}).get("env") or data.get("env") or env
    except (OSError, ValueError):
        pass
    base = Path(os.environ.get("QA_BROWSER_DIR") or (Path(run_dir).resolve().parent / ".browser"))
    env = re.sub(r"[^A-Za-z0-9_-]+", "_", str(env)) or "staging"
    return base / env


def profile_path(run_dir: Path, app: str) -> Path:
    return browser_home(run_dir) / "profiles" / app


def storage_state_path(run_dir: Path, app: str) -> Path:
    """Cookies + local storage saved after a manual login; testers reload this instead of hoping the profile survived."""
    return browser_home(run_dir) / "storage" / f"{app}.json"


_LOGOUT = re.compile(r"log[\s_-]?out|sign[\s_-]?out|logoff", re.I)


def _chrome_lock_pid(profile: Path) -> int | None:
    """Chrome's SingletonLock is a symlink named hostname-pid. None if missing or unreadable."""
    lock = profile / "SingletonLock"
    if not lock.exists() and not lock.is_symlink():
        return None
    try:
        target = os.readlink(lock)
    except OSError:
        return None
    try:
        return int(str(target).rsplit("-", 1)[-1])
    except ValueError:
        return None


def profile_in_use(profile: Path) -> bool:
    """True when another Chrome process still holds this user-data-dir."""
    pid = _chrome_lock_pid(profile)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def clear_stale_profile_locks(profile: Path) -> None:
    """Remove Singleton* leftovers when Chrome is gone so the next MCP can open the profile."""
    if profile_in_use(profile):
        return
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = profile / name
        try:
            if p.exists() or p.is_symlink():
                p.unlink()
        except OSError:
            pass


def wait_profile_free(profile: Path, timeout: float = 20.0) -> bool:
    """Wait until Chrome has released this user-data-dir (or clear a dead SingletonLock)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not profile_in_use(profile):
            clear_stale_profile_locks(profile)
            return True
        time.sleep(0.25)
    if not profile_in_use(profile):
        clear_stale_profile_locks(profile)
        return True
    return False


def mark_clean_exit(profile: Path) -> None:
    """Stop Chrome's "didn't shut down correctly / Restore pages?" bubble on the shared profile."""
    prefs = profile / "Default" / "Preferences"
    try:
        data = json.loads(prefs.read_text())
        prof = data.setdefault("profile", {})
        if prof.get("exit_type") != "Normal" or prof.get("exited_cleanly") is not True:
            prof["exit_type"], prof["exited_cleanly"] = "Normal", True
            prefs.write_text(json.dumps(data))
    except (OSError, ValueError):
        pass


def release_profile(profile: Path, timeout: float = 20.0) -> bool:
    """Wait for Chrome to exit and clear stale Singleton locks. Never SIGTERM/SIGKILL — that drops in-memory cookies."""
    profile.mkdir(parents=True, exist_ok=True)
    return wait_profile_free(profile, timeout=timeout)


def playwright_toolset(run_dir: Path, app: str):
    """No --allowed-origins: Playwright matches it per scheme://host:port and blocks every other request,
    including a site's own CDN files and payment pages (it is not a security boundary either). The sites a run
    may use are enforced in code instead: navigate is checked before it runs, and after every browser action
    the page address is checked; a new site needs the user's OK (TesterSession.after_tool)."""
    from mcp import StdioServerParameters
    try:
        from google.adk.tools.mcp_tool.mcp_toolset import McpToolset as Toolset
    except ImportError:  # older ADK
        from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset as Toolset
    from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams

    out = run_dir / "shots"
    out.mkdir(parents=True, exist_ok=True)
    storage = storage_state_path(run_dir, app)
    profile = profile_path(run_dir, app)
    # Never start a second Chrome on a busy profile — that is the "browser crashed" handoff after CAPTCHA login.
    if storage.is_file():
        # Isolated context loaded from the post-login storage file (covers session-only cookies too).
        args = ["-y", "@playwright/mcp@latest", *([] if headed() else ["--headless"]),
                "--isolated", "--storage-state", str(storage.resolve()),
                "--output-dir", str(out), "--viewport-size", "1366,900"]
    else:
        profile.mkdir(parents=True, exist_ok=True)
        if not release_profile(profile, timeout=25.0):
            raise RuntimeError(
                f"Chrome is still using profile {profile}; close that window and retry.")
        args = ["-y", "@playwright/mcp@latest", *([] if headed() else ["--headless"]),
                "--user-data-dir", str(profile),
                "--output-dir", str(out), "--viewport-size", "1366,900"]
    return Toolset(connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(command="npx", args=args, env=browser_env()), timeout=60))


AUTO_SHOT_SETTLE = float(os.environ.get("AUTO_SHOT_SETTLE", "1.5"))


def _page_body(snap: str) -> str:
    """Snapshot text without element refs, so only real page changes count."""
    return re.sub(r"\[ref=[^\]]*\]", "", snap or "")


_ERR = re.compile(r"(?m)^(?:### Error\s*\n)?\s*((?:\w+\.)*\w*(?:Error|Exception)\b[:\s].*)$")


def _tool_error(text: str | None) -> str | None:
    """First error line of a Playwright MCP result, or None when the call worked."""
    if not text:
        return None
    if "### Error" not in text and not re.search(r"(?m)^\s*(?:\w+\.)*\w*(?:Timeout)?Error\b", text):
        return None
    m = _ERR.search(text)
    line = (m.group(1) if m else text.split("### Error", 1)[-1].strip().splitlines()[0] if "### Error" in text else "")
    return line.strip()[:300] or "the tool reported an error"


EXECUTE_LABEL = re.compile(r"(?i)\bexecute\b")


def _is_not_found(text: str) -> bool:
    t = (text or "")[:6000]
    return bool(re.search(r"\b404\b[^\n]{0,80}(not found|could not be found)|This page could not be found|"
                          r"Page Title:\s*404|heading \"404\"", t, re.I))


def _other_app(url: str, app_urls: dict, app: str) -> str | None:
    """The other app whose base URL this address is under (longest match), or None."""
    u = (url or "").rstrip("/") + "/"
    best, blen = None, -1
    for a, base in (app_urls or {}).items():
        b = (base or "").rstrip("/") + "/"
        if b != "/" and u.startswith(b) and len(b) > blen:
            best, blen = a, len(b)
    return best if best and best != app else None


def _guide_targets(text: str, state) -> set[str]:
    """Case ids the note names (e.g. 'before E1 ...'); empty when it names none."""
    try:
        ids = [c.id for c in state.plan.cases] if state.plan else []
    except Exception:
        ids = []
    return {i for i in ids if re.search(rf"(?<![A-Za-z0-9]){re.escape(i)}(?![A-Za-z0-9])", text, re.I)}


def _guide_fits(row: dict, case_id: str, state) -> bool:
    """A note that names other cases waits for them: the case running now must not act on advice for another
    (it would change settings in the wrong case). Prep turns for a case don't count as that case."""
    targets = _guide_targets(str(row.get("text", "")), state)
    return not targets or (case_id or "").upper() in {t.upper() for t in targets}


class TesterSession:
    """Holds the per-turn bookkeeping that the callbacks and tool functions share."""

    def __init__(self, app: str, brief: dict, state: RunState, policy: Policy, evidence: Evidence,
                 max_calls: int, max_minutes: int, keep_emails: tuple[str, ...] = (), memory=None, ledger=None,
                 helpers: dict | None = None, pack=None, env: str = "staging", toolset=None):
        self.app, self.brief, self.state, self.policy, self.ev = app, brief, state, policy, evidence
        self.case_id = brief["case_id"]
        self.max_calls, self.deadline = max_calls, time.time() + max_minutes * 60
        self.calls = 0                  # every tool call (safety net: 4x max_calls)
        self.actions = 0                # calls that act on the browser/app; capped by max_calls
        self.since_progress = 0         # actions since the last new mark_step
        self._sigs: dict[str, int] = {} # call signature -> repeats since the last new mark_step
        self.result = StepResult(case_id=self.case_id, app=app)
        self.finished = False
        self.keep_emails = keep_emails
        self.last_snapshot = ""
        self.current_step: int | None = None
        self.memory, self.ledger = memory, ledger
        self.helpers, self.pack, self.env, self.toolset = helpers or {}, pack, env, toolset
        self.off_site: str | None = None      # page address outside this run's sites, until the user allows it or we go back
        self._action_gated: set[str] = set()  # action_types already approved this turn (once per gated action)
        self._xsrf_urls: set[str] = set()     # path?query keys that returned an XSRF page this session
        self._last_nav_url: str | None = None # last browser_navigate URL (for XSRF attribution)
        self._repair_kind: str | None = None  # check_repair_sql result for the SQL on the page
        self._exec_ok = False                 # an Execute click was approved for that SQL (or explicitly)

    # ---- tools exposed to the model -------------------------------------------------
    def tools(self):
        s = self

        def record_value(name: str, raw_text: str, source: str) -> dict:
            """Record a value exactly as read on the page. raw_text is the text you saw, e.g. "$1,234.50"."""
            try:
                # IDs like A1142 / INV-0042 stay text: only plain amounts become numbers
                v = raw_text.strip() if re.search(r"[A-Za-z]\d|\d[A-Za-z]|^[A-Za-z]{1,3}-?\d+$", raw_text.strip()) \
                    and not re.match(r"^[A-Z]{3}\s?[\d$]", raw_text.strip()) else V.parse_money(raw_text)
            except ValueError:
                v = raw_text
            V.record(s.state.values, name, v, source=f"{s.case_id}/{s.app}: {source}")
            s.result.values[name] = v
            s.state.save()
            return {"recorded": name, "value": v}

        def calculate(expression: str) -> dict:
            """Arithmetic over recorded value names, e.g. "0.10 * shipping_unpaid". Returns the number."""
            vals = {k: (x["value"] if isinstance(x, dict) else x) for k, x in s.state.values.items()}
            vals = {k: v for k, v in vals.items() if isinstance(v, (int, float))}
            try:
                return {"result": round(V.calculate(expression, vals), 4)}
            except V.CalcError as e:
                return {"error": str(e)}

        def request_approval(action_type: str, description: str) -> dict:
            """Ask before a data-changing, paying or irreversible step. Returns approved true/false."""
            try:
                how = s.policy.gate(description, f"{s.case_id} on {s.app}", action_type=action_type,
                                    details={"case": s.case_id, "app": s.app})
                s.state.log_action(case_id=s.case_id, app=s.app, tool="request_approval",
                                   args={"action_type": action_type, "description": description},
                                   plan_step=s.current_step, approval=how)
                if action_type == "execute_other" or (s._repair_kind and action_type == s._repair_kind):
                    s._exec_ok = True
                return {"approved": True, "how": how}
            except PolicyViolation as e:
                s.state.log_action(case_id=s.case_id, app=s.app, tool="request_approval",
                                   args={"action_type": action_type, "description": description},
                                   plan_step=s.current_step, approval="denied", outcome=str(e))
                return {"approved": False, "reason": str(e)}

        def check_repair_sql(sql: str) -> dict:
            """After Quick Repair and Rebuild lists database differences with an Execute button: pass the full SQL
            exactly as the page shows it. Returns action_type for request_approval: 'execute_repair_sql' when it only
            adds columns/tables/indexes for the new fields (may be pre-approved on staging), 'repair_sql_destructive'
            when anything drops, changes or rewrites data (always asks). action_type null: nothing to execute."""
            from ..repair_sql import classify
            out = classify(sql)
            s._repair_kind, s._exec_ok = out["action_type"], False
            s.state.log_action(case_id=s.case_id, app=s.app, tool="check_repair_sql",
                               args={"sql": sql[:4000]}, plan_step=s.current_step,
                               outcome=f"{out['action_type']}: {len(out['additive'])} additive, {len(out['risky'])} risky")
            s.state.data.setdefault("values", {})[f"{s.case_id}.repair_sql"] = {"value": sql[:4000], "app": s.app}
            s.state.save()
            return out

        async def mark_step(n: int, tool_context, deviation: str = "", inferred: bool = False,
                            reason: str = "") -> dict:
            """Mark plan step n as done. Describe any difference from the plan in deviation.
            A proof screenshot of the page is taken automatically."""
            s.current_step = n
            await s.auto_shot(f"step{n}", tool_context)
            if n not in s.result.steps_done:
                s.result.steps_done.append(n)
            s.state.log_action(case_id=s.case_id, app=s.app, tool="mark_step", args={"n": n, "reason": reason},
                               plan_step=n, inferred=inferred, deviation=deviation or None)
            return {"ok": True}

        def evidence_name(what: str) -> dict:
            """Absolute path for browser_take_screenshot; basename is recorded only after the PNG exists."""
            png, txt = s.ev.paths(s.case_id, what)
            if s.last_snapshot:
                s.ev.save_snapshot(txt, s.last_snapshot, keep_emails=s.keep_emails)
                s.result.snapshots.append(txt.name)
            return {"filename": str(png.resolve())}

        def get_secret(env_name: str) -> dict:
            """Passwords are never returned to the model. Use run_helper("login_form", ...) so code types them."""
            return {"error": f"passwords are not returned to the model (asked for {env_name}); "
                             f'use run_helper("login_form", \'{{"account": "<pack account>"}}\')'}

        def needs_manual_login(reason: str) -> dict:
            """Call when login is blocked by a CAPTCHA or robot check. Do not try to solve it."""
            s.result.needs_login = redact(reason) or "CAPTCHA at login"
            s.result.notes, s.result.stuck, s.finished = f"needs a manual login: {s.result.needs_login}", True, True
            return {"ok": True, "next": "stop now; a person will log in and the case will be retried"}

        async def paste_text(target: str, text: str, tool_context, element: str = "") -> dict:
            """Paste text into a field the way a person does (clipboard paste, Ctrl+V), not typing. Use it whenever
            a step says paste: sites often clean pasted text in a separate paste handler that typing never runs.
            target is the element ref from browser_snapshot (e.g. "e15"). The field is cleared first, and the value
            the field shows afterwards is read back and returned; record_value it before pasting the next value."""
            from ..helpers import adk_caller
            ref = str(target or "").removeprefix("ref=").strip()
            if not re.fullmatch(r"[a-z]?\d*e\d+", ref):
                return {"error": "target must be an element ref from browser_snapshot, e.g. e15"}
            if s.toolset is None or tool_context is None:
                return {"error": "browser not available"}
            if getattr(s, "unread_input", None) == ref:
                return {"error": "you have not read back the last value in this field; browser_snapshot and "
                                 "record_value it first, then paste the next one"}
            code = PASTE_JS.replace("__REF__", json.dumps(ref)).replace("__TEXT__", json.dumps(str(text)))
            try:
                out = str(await adk_caller(s.toolset, tool_context)("browser_run_code_unsafe", {"code": code}) or "")
            except Exception as e:  # noqa: BLE001
                return {"error": f"paste failed: {str(e)[:300]}"}
            m = re.search(r"\{.*\}", out.replace('\\"', '"'), re.S)
            try:
                res = json.loads(m.group(0)) if m else {}
            except ValueError:
                res = {}
            s.state.log_action(case_id=s.case_id, app=s.app, tool="paste_text",
                               args={"element": element, "target": ref, "text": redact(str(text))},
                               plan_step=s.current_step, outcome="ok" if "value" in res else out[:150])
            if "value" not in res:
                return {"error": f"paste did not run: {out[:300]}"}
            return {"pasted": str(text), "field_now_shows": res["value"], "how": res.get("how"),
                    "next": "record_value what field_now_shows says, then continue"}

        async def finish(notes: str, tool_context, stuck: bool = False) -> dict:
            """Call when your steps are done, or when you are stuck and cannot continue.
            A final proof screenshot is taken automatically."""
            await s.auto_shot("final", tool_context)
            s.result.notes, s.result.stuck, s.finished = redact(notes), stuck, True
            return {"ok": True}

        def ask_user(question: str, why: str = "") -> dict:
            """Ask the person running the test when you are genuinely unsure and the page cannot tell you
            (e.g. which of two similar menus the case means, or whether an unexpected screen is normal).
            Never ask for passwords or codes. At most 3 questions per case; the answer is shown in the report."""
            from ..auto import is_auto, ask_line
            s.asked = getattr(s, "asked", 0) + 1
            if s.asked > 3:
                return {"error": "question limit reached for this case; decide from the page or finish(stuck=True)"}
            if re.search(r"password|passcode|otp|one.time|2fa|secret|token", question, re.I):
                return {"error": "never ask for passwords or codes; use run_helper login_form or needs_manual_login"}
            q = redact(question)
            if is_auto() and os.environ.get("QA_WEB") != "1" and not (sys.stdin and sys.stdin.isatty()):
                ans = ""
            else:
                try:
                    ans = ask_line(f"\n[{s.case_id}] The tester asks: {q}" + (f"\n  (why: {redact(why)})" if why else "")
                                   + "\n  Your answer (Enter = let it decide): ").strip()
                except EOFError:
                    ans = ""
            s.state.log_action(case_id=s.case_id, app=s.app, tool="ask_user", args={"question": q, "answer": ans},
                               plan_step=getattr(s, "current_step", None), inferred=False, deviation=None)
            return {"answer": ans} if ans else {"answer": "", "note": "no answer; decide from the page and say so in notes"}

        def note_behaviour(text: str) -> dict:
            """Save how the app behaves so later cases and runs know it, e.g. "Threshold Save with 12.5 shows no
            message; after a reload the old value is back (silent reject)" or "Print opens in a new tab".
            Only what you saw on the page. No record ids, names or passwords."""
            from .. import learned
            line = learned.record_behaviour(s.state.data.get("pack_root"), s.brief.get("area"), s.app,
                                            redact(text), s.case_id)
            s.state.log_action(case_id=s.case_id, app=s.app, tool="note_behaviour", args={"text": redact(text)[:2000]},
                               plan_step=s.current_step)
            return {"saved": bool(line)} if line else {"saved": False, "note": "already known or too short"}

        def lookup_docs(question: str) -> dict:
            """Search this app's stack documentation (listed in the pack, e.g. SuiteCRM or Next.js) when you do not
            know how a screen or workflow works. Answers are cached for later runs. Still check the page itself."""
            from .. import docs
            out = docs.lookup(s.pack, s.app, question)
            s.state.log_action(case_id=s.case_id, app=s.app, tool="lookup_docs", args={"question": question[:200]},
                               plan_step=s.current_step, outcome="ok" if out.get("results") else str(out)[:120])
            return out

        def recall_lessons(query: str) -> dict:
            """Search saved lessons, recipes and known bugs for this project, e.g. "discount apply button"."""
            if s.memory is None:
                return {"lessons": []}
            return {"lessons": [{"id": r["id"], "kind": r["kind"], "text": r["text"]} for r in s.memory.recall(query)]}

        def propose_memory(kind: str, text: str, area: str = "") -> dict:
            """Suggest a lesson, recipe or fact worth keeping (e.g. a workaround that worked). The user approves it at the end."""
            if s.memory is None:
                return {"error": "memory not available"}
            try:
                mid = s.memory.propose(kind, text, area=area or s.brief.get("area"), evidence=f"{s.state.dir.name}/{s.case_id}")
                return {"proposed": mid}
            except ValueError as e:
                return {"error": str(e)}

        def ledger_record(record: str, action: str, final_state: str = "", note: str = "") -> dict:
            """Log test data you created or changed. action: created | changed | used. record e.g. "order A1158"."""
            if s.ledger is None:
                return {"error": "ledger not available"}
            s.ledger.add(s.case_id, s.app, record, action, final_state, note)
            return {"ok": True, "tag": s.ledger.run_tag}

        def record_original(setting: str, value: str) -> dict:
            """Before changing an admin setting, record its current value exactly as shown."""
            if s.ledger is None:
                return {"error": "ledger not available"}
            s.ledger.record_original(s.case_id, s.app, setting, value)
            return {"ok": True}

        def record_restored(setting: str, value: str, verified: bool) -> dict:
            """After restoring an admin setting, record the value now shown and whether you re-read it to verify."""
            if s.ledger is None:
                return {"error": "ledger not available"}
            try:
                r = s.ledger.record_restored(s.app, setting, value, verified)
            except KeyError as e:
                return {"error": str(e)}
            return {"restored_ok": r["verified"], "original": r["original"]}

        def create_tool(name: str, purpose: str, script: str, params_json: str = "{}", changes_data: bool = False,
                        action_type: str = "") -> dict:
            """Ask for a new tool when no existing tool can do what a step needs (e.g. drag and drop a file, read a
            canvas value, press a key combination on one field). script is ONE Playwright function:
            async (page, args) => { ...; return JSON.stringify({...}); }  using page.locator('aria-ref=e15') for
            elements from browser_snapshot and args for inputs. params_json describes the inputs, e.g.
            '{"target": "element ref", "text": "value to enter"}'. Code checks it and the person running the test
            approves it; once approved, call run_helper(name, params_json) to use it (later runs have it too).
            No secrets, cookies, network calls, page navigation or logout. At most 2 per case."""
            from .. import gen_tools
            from ..auto import ask_line, is_auto
            s.drafted = getattr(s, "drafted", 0) + 1
            if s.drafted > 2:
                return {"error": "tool request limit reached for this case; carry on without it or finish(stuck=True)"}
            try:
                params = json.loads(params_json or "{}")
                assert isinstance(params, dict)
            except Exception:  # noqa: BLE001
                return {"error": "params_json must be a JSON object"}
            problems = gen_tools.check(name, script, set(s.helpers))
            if changes_data and not action_type:
                problems.append("a tool that changes data needs an action_type for the approval gate")
            if problems:
                return {"error": "tool rejected by checks: " + "; ".join(problems) + ". Fix the script and ask again."}
            src = gen_tools.render(name, redact(purpose), params, script, changes_data, action_type, s.case_id)
            root = s.state.data.get("pack_root") or (str(s.pack.root) if s.pack else None)
            if not root:
                return {"error": "no pack to save tools in"}
            ans = ""
            if not (is_auto() and os.environ.get("QA_WEB") != "1" and not (sys.stdin and sys.stdin.isatty())):
                try:
                    ans = ask_line(f"\n[{s.case_id}] The tester asks for a new tool '{name}': {redact(purpose)}\n"
                                   f"  Inputs: {json.dumps(params)}" + (f"\n  Changes data: yes ({action_type})" if changes_data else "")
                                   + f"\n----- script -----\n{script}\n------------------\n"
                                   "  Approve this tool? [y/N]: ").strip().lower()
                except EOFError:
                    ans = ""
            approved = ans in ("y", "yes")
            path = gen_tools.save(root, name, src, approved)
            s.state.log_action(case_id=s.case_id, app=s.app, tool="create_tool",
                               args={"name": name, "purpose": redact(purpose)[:200]}, plan_step=s.current_step,
                               approval="approved" if approved else "rejected", outcome=str(path))
            s.state.data.setdefault("tool_requests", []).append(
                {"case_id": s.case_id, "name": name, "purpose": redact(purpose)[:200], "approved": approved, "path": str(path)})
            s.state.save()
            if not approved:
                return {"approved": False, "note": "not approved (saved for review). Carry on another way or finish(stuck=True) "
                                                   "saying which tool was missing"}
            try:
                from ..helpers import HelperInfo, _load_module
                mod = _load_module(path, "helper_gen")
                s.helpers[mod.HELPER["name"]] = HelperInfo(mod.HELPER["name"], mod, "pack", True)
            except Exception as e:  # noqa: BLE001
                return {"error": f"approved but could not load: {e}"}
            return {"approved": True, "next": f"call run_helper(\"{name}\", params_json) to use it, then read the page back"}

        async def run_helper(name: str, params_json: str, tool_context) -> dict:
            """Run a tested helper instead of clicking through a routine step yourself, e.g.
            run_helper("login_form", '{"account": "admin"}'). Returns the values it read and its screenshots."""
            from ..helpers import HelperContext, McpBrowser, adk_caller
            from ..helpers import run_helper as _run
            info = s.helpers.get(name)
            if info is None:
                return {"error": f"no helper {name}; available: {', '.join(sorted(s.helpers)) or 'none'}"}
            try:
                params = json.loads(params_json or "{}")
            except json.JSONDecodeError as e:
                return {"error": f"params_json is not JSON: {e}"}
            if s.brief.get("read_only") and info.meta.get("changes_data"):
                return {"error": "this turn is read-only; data-changing helpers are not allowed"}
            shots = s.ev.shots.resolve()
            shots.mkdir(parents=True, exist_ok=True)
            browser = McpBrowser(adk_caller(s.toolset, tool_context), evidence_dir=shots)
            ctx = HelperContext(pack=s.pack, env=s.env, app=s.app, case_id=s.case_id, state=s.state,
                                policy=s.policy, shot_prefix=str(shots) + "/")
            out = await _run(info, browser, ctx, params)
            for k, v in (out.get("values") or {}).items():
                s.result.values[f"{name}.{k}"] = v
            for x in out.get("screenshots", []):
                bn = Path(x).name
                if bn not in s.result.screenshots and (shots / bn).is_file():
                    s.result.screenshots.append(bn)
            return out

        tools = [record_value, calculate, request_approval, check_repair_sql, mark_step, evidence_name, get_secret, finish, recall_lessons,
                 needs_manual_login, ask_user, note_behaviour, lookup_docs]
        if s.toolset is not None:
            tools.append(paste_text)
            if not s.brief.get("discovery"):
                tools.append(create_tool)
        if s.toolset is not None:
            tools.append(run_helper)
        # Discovery is read-only for data writes but may propose recipes; ledger only on real cases.
        if s.brief.get("discovery") or not s.brief.get("read_only"):
            tools.append(propose_memory)
        if not s.brief.get("read_only"):
            tools += [ledger_record, record_original, record_restored]
        return tools

    def _step_action_type(self) -> str | None:
        """action_type of the current plan step (or the next unfinished step that has one)."""
        steps = self.brief.get("steps") or []
        if self.current_step is not None:
            for st in steps:
                if st.get("n") == self.current_step and st.get("action_type"):
                    return st["action_type"]
        done = set(self.result.steps_done or [])
        for st in steps:
            if st.get("action_type") and st.get("n") not in done:
                return st["action_type"]
        return None

    # ---- callbacks: the code side of every tool call --------------------------------
    def before_tool(self, tool, args, tool_context):
        name = tool.name
        self.calls += 1
        note = TesterSession._new_guidance(self)
        if note:
            self._guidance_notes = getattr(self, "_guidance_notes", []) + [note]
        if note and name != "finish":
            self.state.log_action(case_id=self.case_id, app=self.app, tool=name, args=_safe_args(args),
                                  plan_step=self.current_step, approval="held", outcome="user guidance delivered")
            self.state.checkpoint(f"{self.case_id} ({self.app}): guidance from the user delivered to the agent")
            return {"error": f"{note}\nYour {name} call was NOT run. Follow the guidance, then call it again "
                             "if it still fits."}
        settle = _settle_seconds(getattr(self, "app", ""), getattr(self, "pack", None))
        now = time.monotonic()
        last_act, last_snap = getattr(self, "_last_act", 0.0), getattr(self, "_last_snap", 0.0)
        if name in ACTING_TOOLS:
            self._last_act = now
        elif name in ("browser_snapshot", "browser_take_screenshot", "browser_find"):
            if last_act and now - last_act < settle:          # pages update 3-8 s late: let them settle
                time.sleep(settle - (now - last_act))
            self._last_snap = time.monotonic()
        elif name == "record_value" and last_act and last_act > last_snap:
            return {"error": "The page changed after your last read and may still be updating. "
                             "Take a fresh browser_snapshot (it waits for the page to settle), then record the value again."}
        if name in ("browser_type", "browser_fill_form"):
            ref = str(args.get("target") or args.get("ref") or "").removeprefix("ref=")
            if ref and getattr(self, "unread_input", None) == ref:
                return {"error": "you typed into this field and never read the result back. browser_snapshot, "
                                 "record_value what it shows, then enter the next value"}
            self.unread_input = ref or None
        elif name in ("browser_snapshot", "browser_find", "record_value"):
            self.unread_input = None
        entered = self.state.data.setdefault("entered_values", {}).setdefault(self.case_id, {})
        if name in ("browser_type", "paste_text") and args.get("text") is not None:
            key = f"{re.sub(r'[^a-z0-9 ]', '', str(args.get('element') or '').lower()).strip()}={args.get('text')}"
            if entered.get(key) == "saved":
                return {"error": f"You already entered {args.get('text')!r} in this field and saved it. Do not enter it "
                                 "again. Reopen the page by its route, READ the field with browser_snapshot, "
                                 "record_value what it shows (that is the stored value), then mark_step and go to the "
                                 "next value."}
            entered[key] = "typed"
        if name == "browser_click" and _SAVE.search(str(args.get("element") or "")):
            for k, v in list(entered.items()):
                if v == "typed":
                    entered[k] = "saved"
        if name == "browser_find":
            self.misses = getattr(self, "misses", 0) + 1
            if self.misses == 3:
                self.misses = 0
                return {"error": RELOAD_HINT}
        if name == "record_value" and not (last_act and last_act > last_snap):
            self.since_progress, self._sigs = 0, {}          # a value read back is progress too
        if name == "mark_step" and args.get("n") not in self.result.steps_done:
            self.since_progress, self._sigs, self.saves, self.misses = 0, {}, 0, 0          # real progress: fresh per-step allowance
            self._explored, self._recent, self._reads = False, [], 0
        limit = self._budget_check(name, args)
        if limit:
            return limit
        if isinstance(args.get("target"), str) and args["target"].startswith("ref="):
            args["target"] = args["target"][4:]              # models often copy "ref=e15"; the tool wants "e15"
        if isinstance(args.get("ref"), str) and args["ref"].startswith("ref="):
            args["ref"] = args["ref"][4:]
        if name in BLOCKED_BROWSER:
            return self._deny(name, args, "this browser tool is not allowed. Read the page with browser_snapshot or "
                                          "browser_find; if the result you expected is not there, that is your result: "
                                          "reload, read the stored value, record_value it and move on. If you truly need "
                                          "page scripting for a step, ask for a tool with create_tool")
        if name in ("browser_navigate", "browser_click") and _LOGOUT.search(str(args.get("url") or args.get("element") or "")) \
                and not _LOGOUT.search(json.dumps(self.brief.get("steps") or [])):
            return self._deny(name, args, "logging out is not allowed: the browser login is shared by later runs "
                                          "(log out only when the test case itself is about logging out)")
        if name == "browser_take_screenshot":
            self._fix_screenshot_args(args)
        if name.startswith("browser_"):
            if self.off_site and name not in ("browser_navigate", "browser_navigate_back", "browser_snapshot",
                                              "browser_take_screenshot"):
                return self._deny(name, args, f"the browser is on {self.off_site}, which this run may not use; "
                                              "go back (browser_navigate_back) or navigate to the app")
            try:
                if name == "browser_navigate":
                    url = args.get("url", "") or ""
                    key = _url_nav_key(url)
                    if key and key in self._xsrf_urls:
                        return self._deny(
                            name, args,
                            "this URL already returned an XSRF / acceptable-referer page; "
                            "open the screen via the discovery menu path instead of deep-linking again")
                    other = _other_app(url, getattr(self, "app_urls", {}), self.app)
                    if other:
                        return self._deny(name, args, (
                            f"{url} is the {other} app, but this turn only does the {self.app} steps (the {other} "
                            f"steps run in their own turn, with their own login). Stay on {self.app}. If a {self.app} "
                            f"step needs test data that is missing, finish with notes 'BLOCKED: <what is missing>'."))
                    self.policy.check_url(url)
                    self._last_nav_url = url
                label = " ".join(str(args.get(k, "")) for k in ("element", "text", "values") if args.get(k))
                if name == "browser_click" and EXECUTE_LABEL.search(label) and not self._exec_ok:
                    return self._hold(name, args, (
                        "Execute needs approval first. For Quick Repair and Rebuild SQL: read the full SQL from the "
                        "page, call check_repair_sql with it, then request_approval with the action_type it returns. "
                        "For any other Execute button: request_approval with action_type 'execute_other'.")
                        if self._repair_kind is None else
                        f"Execute needs request_approval with action_type '{self._repair_kind}' (from check_repair_sql) first.")
                if name not in READ_ONLY_BROWSER and name != "browser_navigate":
                    risky = self.policy.is_risky(label) or bool(args.get("submit"))
                    action_type = self._step_action_type()
                    if self.policy.prod_change(name, label, args) or (self.policy.env == "prod" and risky):
                        # production: every action that may change a value asks, each time
                        how = self.policy.gate(f"{name}: {label}", f"{self.case_id} on {self.app}",
                                               action_type=action_type or "prod_write", risky=True,
                                               details={"tool": name, "args": _safe_args(args)})
                        self.state.log_action(case_id=self.case_id, app=self.app, tool="request_approval",
                                              args={"action_type": action_type or "prod_write",
                                                    "description": f"{name}: {label}", "how": how, "via": "prod_gate"},
                                              plan_step=self.current_step, approval=how)
                    # Close the hole where Save on a change_settings step skipped the console gate.
                    elif self.policy.env == "prod":
                        pass                                 # reads/navigation on prod: no prompt
                    elif action_type and action_type not in self._action_gated:
                        how = self.policy.gate(f"{name}: {label}", f"{self.case_id} on {self.app}",
                                               action_type=action_type, risky=risky,
                                               details={"tool": name, "args": _safe_args(args)})
                        self._action_gated.add(action_type)
                        self.state.log_action(case_id=self.case_id, app=self.app, tool="request_approval",
                                              args={"action_type": action_type, "description": f"{name}: {label}",
                                                    "how": how, "via": "browser_gate"},
                                              plan_step=self.current_step, approval=how)
                    elif risky:
                        self.policy.gate(f"{name}: {label}", f"{self.case_id} on {self.app}", risky=True,
                                         details={"tool": name, "args": _safe_args(args)})
            except PolicyViolation as e:
                return self._deny(name, args, str(e))
            logged = _safe_args(args)
            if name in ("browser_click", "browser_select_option", "browser_type") and not logged.get("element"):
                lab = _label_of(str(args.get("target") or args.get("ref") or ""), self.last_snapshot)
                if lab:
                    logged = {**logged, "element": lab}     # learned routes need the label; refs die with the page
            self._pending_seq = self.state.log_action(case_id=self.case_id, app=self.app, tool=name,
                                                      args=logged, plan_step=self.current_step).seq
        return None

    def _budget_check(self, name: str, args: dict):
        """Progress-based budget. Reads (snapshots, screenshots, recording) are free; actions are capped per plan
        step (reset by each new mark_step) and in total; the same call repeated without progress is a loop."""
        if name == "finish":
            return None
        if self.result.limit_hit:                            # already told to stop: don't let it carry on for ever
            self._after_stop = getattr(self, "_after_stop", 0) + 1
            if self._after_stop > HARD_STOP_AFTER:
                self.result.stuck = True
                self.finished = True
                why = "a repeated call" if self.result.loop else "the case's time or action limit"
                self.result.notes = self.result.notes or (
                    f"Stopped by code: the tester was told to finish after {why} but kept calling tools "
                    f"(last: {name}). Step reached: {self.current_step}.")
                self.state.log_action(case_id=self.case_id, app=self.app, tool="hard_stop", args={"call": name},
                                      plan_step=self.current_step, outcome="stop")
                raise TesterHardStop(self.result.notes)
            if name not in _AFTER_STOP_OK:
                return {"error": "You were already told to stop. No browser tool will run now. Call "
                                 "finish(notes, stuck=True) saying which step you reached and what the page showed."}
            return None
        over_time = time.time() > self.deadline
        if not over_time and self.calls > 4 * self.max_calls:
            over_time = True                                 # runaway reads
        if name in ("browser_snapshot", "browser_take_screenshot", "browser_find"):   # browser_wait_for is a real wait
            self._reads = getattr(self, "_reads", 0) + 1
            if self._reads >= IDLE_READS:
                self._reads = 0
                if not getattr(self, "_explored", False):
                    return self._start_exploring(name, f"the page was read {IDLE_READS} times in a row with nothing done")
                self.result.limit_hit = True
                self.result.loop = True
                self.state.log_action(case_id=self.case_id, app=self.app, tool="loop_detected",
                                      args={"call": name, "repeats": IDLE_READS}, plan_step=self.current_step,
                                      outcome="stop")
                return {"error": "You keep re-reading the page without doing anything. Stop now: call "
                                 "finish(notes, stuck=True) saying which step you reached and what is on screen."}
        elif name not in FREE_TOOLS:
            self._reads = 0
        if name not in FREE_TOOLS:
            self.actions += 1
            self.since_progress += 1
            sig = _call_sig(name, args)
            self._sigs[sig] = self._sigs.get(sig, 0) + 1
            intent = _intent_sig(name, args, self.last_snapshot)   # same target reached by reworded calls/refs
            if intent:
                self._sigs[intent] = self._sigs.get(intent, 0) + 1
            n = max(self._sigs[sig], self._sigs.get(intent, 0) if intent else 0)
            if name == "browser_click" and _SAVE.search(str(args.get("element") or "")):
                cnt = self.state.data.setdefault("save_clicks", {})
                cnt[self.case_id] = cnt.get(self.case_id, 0) + 1
                self.saves = getattr(self, "saves", 0) + 1
                if self.saves == 2 or cnt[self.case_id] == 2:
                    return {"error": RELOAD_HINT}
            self._recent = (getattr(self, "_recent", []) + [intent or sig])[-12:]
            clicky = name in ("browser_click", "browser_run_code_unsafe", "browser_evaluate")
            warn_at, stop_at = (2, 4) if clicky else (LOOP_WARN, LOOP_STOP)   # a page click that did nothing: rethink at once
            cycling = _is_cycle(self._recent)
            if (n >= stop_at or cycling) and not getattr(self, "_explored", False):
                return self._start_exploring(name, "the same actions kept repeating" if cycling else
                                             f"{name} was repeated {n} times")
            if cycling:
                n = stop_at
            if n >= stop_at:
                self.result.limit_hit = True
                self.result.loop = True
                self.state.log_action(case_id=self.case_id, app=self.app, tool="loop_detected",
                                      args={"call": name, "repeats": n}, plan_step=self.current_step, outcome="stop")
                return {"error": f"You repeated {name} with the same arguments {n} times without finishing a step. "
                                 "Stop now: call finish(notes, stuck=True) describing where you are."}
            if n == warn_at:
                return {"error": f"Blocked: you already did this {name} and the page did not move the step forward; "
                                 "repeating it will not help. Take a fresh browser_snapshot, re-read the page and look "
                                 "for another control or URL that reaches this step's goal (e.g. a different button with "
                                 "the same purpose). If no available tool can do what the step needs, ask for one with "
                                 "create_tool. When the other route works, call propose_memory with it as a recipe "
                                 "so the next run goes straight there. If no route exists, finish(stuck=True)."}
            per_step = int(self.brief.get("actions_per_step") or ACTIONS_PER_STEP)
            if self.since_progress > per_step and not getattr(self, "_explored", False) and self.actions <= self.max_calls:
                return self._start_exploring(name, f"{per_step} actions went by without finishing a step")
            if self.since_progress > per_step + (EXPLORE_ACTIONS if getattr(self, "_explored", False) else 0) \
                    or self.actions > self.max_calls + (EXPLORE_ACTIONS if getattr(self, "_explored", False) else 0):
                over_time = True
        if over_time:
            self.result.limit_hit = True
            return {"error": "Limit reached. Call finish() now with what you have, saying which step you reached."}
        return None

    def _start_exploring(self, name: str, why: str) -> dict:
        """Stuck: instead of stopping (or retrying the same guess), map the screen once, then retry from what was
        found. One exploration per step; a second stall stops the case with the findings."""
        self._explored = True
        tried = [t for t in dict.fromkeys(getattr(self, "_recent", [])) if t][-8:]
        self._sigs, self._recent, self.since_progress = {}, [], 0
        self.state.log_action(case_id=self.case_id, app=self.app, tool="explore", args={"why": why, "tried": tried},
                              plan_step=self.current_step, outcome="explore")
        self.state.checkpoint(f"{self.case_id}: stuck ({why}); exploring the screen instead of guessing")
        return {"error": EXPLORE_TEXT.format(why=why, tried="; ".join(tried) or "none", n=EXPLORE_ACTIONS)}

    async def auto_shot(self, what: str, tool_context) -> str | None:
        """Code-taken proof screenshot (never left to the model). Waits for the page to settle, then checks the
        page did not change while the shot was taken (staging screenshots can lag a step); retakes once if so.
        Never raises: a failed shot must not fail the case."""
        if self.toolset is None or tool_context is None:
            return None
        from ..helpers import adk_caller
        call = adk_caller(self.toolset, tool_context)
        try:
            png, txt = self.ev.paths(self.case_id, what)
            self.ev.shots.mkdir(parents=True, exist_ok=True)
            for attempt in range(2):
                try:
                    await call("browser_wait_for", {"time": AUTO_SHOT_SETTLE})
                except Exception:
                    pass
                before = str(await call("browser_snapshot", {}) or "")
                await call("browser_take_screenshot", {"filename": str(png.resolve()), "type": "png"})
                after = str(await call("browser_snapshot", {}) or "")
                if _page_body(before) == _page_body(after):
                    break
            self.last_snapshot = after or before
            if self.last_snapshot:
                self.ev.save_snapshot(txt, self.last_snapshot, keep_emails=self.keep_emails)
                if txt.name not in self.result.snapshots:
                    self.result.snapshots.append(txt.name)
            if png.is_file() and png.name not in self.result.screenshots:
                self.result.screenshots.append(png.name)
                return png.name
        except Exception as e:  # noqa: BLE001
            log.warning("auto screenshot %s failed: %s", what, e)
        return None

    def _fix_screenshot_args(self, args: dict) -> None:
        """The model often calls browser_take_screenshot without a filename; Playwright MCP then returns the image
        inline only and nothing reaches shots/ or the report. Always give it an absolute path under shots/
        (ADK passes this same dict to the tool, so the in-place change is what runs)."""
        raw = (args.get("filename") or "").strip()
        p = Path(raw) if raw else None
        if p is not None and p.is_absolute() and p.parent.resolve() == self.ev.shots.resolve():
            return
        what = p.stem if p is not None and p.stem else (self.brief.get("title") or "evidence")
        png, txt = self.ev.paths(self.case_id, what)
        if self.last_snapshot:
            self.ev.save_snapshot(txt, self.last_snapshot, keep_emails=self.keep_emails)
            if txt.name not in self.result.snapshots:
                self.result.snapshots.append(txt.name)
        args["filename"] = str(png.resolve())

    def after_tool(self, tool, args, tool_context, tool_response):
        if tool.name == "browser_snapshot":
            self.last_snapshot = _text_of(tool_response)
        if tool.name == "browser_take_screenshot":
            raw = args.get("filename") or ""
            png = Path(raw) if Path(raw).is_absolute() else self.ev.shots / Path(raw).name
            if not png.is_file():
                # fall back to a path the tool reported, copied into shots/
                m = re.search(r"([^\s'\"()\[\]]+\.png)", _text_of(tool_response) or "")
                if m and Path(m.group(1)).is_file():
                    import shutil
                    dst = self.ev.shots / Path(m.group(1)).name
                    if Path(m.group(1)).resolve() != dst.resolve():
                        shutil.copy(m.group(1), dst)
                    png = dst
            if png.is_file():
                name = png.name
                if name not in self.result.screenshots:
                    self.result.screenshots.append(name)
        if tool.name.startswith("browser_"):
            text = _text_of(tool_response)
            err = _tool_error(text)
            seq = getattr(self, "_pending_seq", None)
            if err and seq:
                # the action log is written before the call; record the real result so the judge doesn't read
                # "ok" for a call that failed (#183498 TC1: a type into a read-only field timed out)
                self.state.set_outcome(seq, f"error: {err}")
            self._pending_seq = None
            blocked = self._check_page_site(tool.name, text)
            if blocked:
                return {"error": blocked}
            if tool.name == "browser_navigate" and _is_not_found(text):
                self.state.log_action(case_id=self.case_id, app=self.app, tool="not_found",
                                      args={"url": str(args.get("url", ""))[:300]}, plan_step=self.current_step,
                                      outcome="404")
                return {"error": (f"{args.get('url', '')} does not exist (404 page). Do not guess addresses. Use "
                                  "only a URL from the project rules / recipes (filled with ids from values_so_far) "
                                  "or a link on the page; otherwise open the screen through the menus.")}
            xsrf = self._note_xsrf(tool.name, args, text)
            if xsrf:
                return {"error": xsrf}
            return _cap_obj(_redact_obj(tool_response, self.keep_emails), MAX_PAGE_CHARS)
        return None

    def before_model(self, callback_context, llm_request):
        """Keep the model's history small: only the last KEEP_PAGE_READS browser results stay whole; older ones
        are replaced by a one-line placeholder (the tester can take a new snapshot)."""
        contents = getattr(llm_request, "contents", None)
        prune_history(contents or [], KEEP_PAGE_READS)
        note = TesterSession._new_guidance(self)
        if note:                                     # the user's note reaches the model at its very next thought
            self._guidance_notes = getattr(self, "_guidance_notes", []) + [note]
            self.state.log_action(case_id=self.case_id, app=self.app, tool="guidance", args={},
                                  plan_step=self.current_step, outcome="user guidance delivered")
            self.state.checkpoint(f"{self.case_id} ({self.app}): guidance from the user delivered to the agent")
        notes = getattr(self, "_guidance_notes", [])
        if notes and contents is not None:           # ADK rebuilds the request each call: keep the notes in view
            from google.genai import types as _gt
            contents.append(_gt.Content(role="user", parts=[_gt.Part(
                text="\n\n".join(notes) + "\nApply this now, in your current step, and keep following it.")]))
        return None

    def _check_page_site(self, tool_name: str, text: str) -> str | None:
        """The app may send the browser to another site (e.g. a payment page). A site this run hasn't
        been allowed to use needs the user's OK once; after that it is allowed for the rest of the run."""
        m = re.search(r"Page URL:\s*(\S+)", text or "")
        if not m:
            return None
        url = m.group(1)
        host = (urlparse(url).hostname or "").lower()
        if not host or urlparse(url).scheme not in ("http", "https"):
            self.off_site = None
            return None
        try:
            self.policy.check_url(url)
            self.off_site = None
            return None
        except PolicyViolation:
            pass
        try:
            self.policy.gate(f"continue on {host} (the app sent the browser to {url})",
                             f"{self.case_id} on {self.app}: a site this run hasn't used", risky=True,
                             details={"tool": tool_name, "new_site": host})
            self.policy.allow_host(host)
            self.off_site = None
            self.state.log_action(case_id=self.case_id, app=self.app, tool="allow_site", args={"host": host},
                                  plan_step=self.current_step, approval="approved")
            return None
        except PolicyViolation as e:
            self.off_site = host
            self.state.log_action(case_id=self.case_id, app=self.app, tool="allow_site", args={"host": host},
                                  plan_step=self.current_step, approval="denied", outcome=str(e))
            return (f"Blocked: the browser went to {host}, which this run may not use ({e}). "
                    "Go back with browser_navigate_back, or call finish() and say the case needs that site.")

    def _note_xsrf(self, tool_name: str, args: dict, text: str) -> str | None:
        """If the page is SuiteCRM XSRF, remember the URL key and tell the model to use the menu path."""
        if not _is_xsrf_page(text):
            return None
        m = re.search(r"Page URL:\s*(\S+)", text or "")
        page_url = m.group(1) if m else ""
        # Prefer the URL we navigated to; XSRF pages sometimes keep or rewrite the address.
        for candidate in (args.get("url") if tool_name == "browser_navigate" else None,
                          self._last_nav_url, page_url):
            key = _url_nav_key(candidate or "")
            if key:
                self._xsrf_urls.add(key)
                break
        else:
            key = ""
        self.state.checkpoint(
            f"XSRF page on {self.app}/{self.case_id}: {key or page_url or '(unknown url)'} "
            f"(via {tool_name}); do not deep-link again")
        self.state.log_action(case_id=self.case_id, app=self.app, tool="xsrf_detected",
                              args={"url_key": key, "page_url": page_url, "via": tool_name},
                              plan_step=self.current_step, outcome="xsrf")
        return ("XSRF / acceptable-referer page detected. Do not browser_navigate to this admin deep-link again. "
                "Open the screen via the discovery menu/label path (Home or Administration menu), then continue.")

    def _new_guidance(self) -> str | None:
        """Notes the user typed in the web UI while the run goes (run_dir/guidance.jsonl). Each session sees
        every note once, at its next tool call. Guidance is advice only: approvals and safety rules still apply."""
        f = self.state.dir / "guidance.jsonl"
        if not f.exists():
            return None
        try:
            rows = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        except (OSError, ValueError):
            return None
        seen = getattr(self, "_guide_seen", 0)
        new = [r for r in rows[seen:] if _guide_fits(r, getattr(self, "case_id", ""), self.state)]
        self._guide_seen = len(rows)
        if not new:
            return None
        older = "" if seen else " Earlier notes may have been meant for an earlier case; use them only where they fit."
        lines = "\n".join(f"- {str(r.get('text', '')).strip()}" for r in new if str(r.get("text", "")).strip())
        if not lines:
            return None
        return ("GUIDANCE FROM THE USER (the person running this test; it is advice, it does not lift approvals, "
                f"production rules or the read-the-page rule).{older}\n{lines}")

    def _hold(self, name, args, why):
        """Not yet: the click is allowed once the named approval is in place."""
        self.state.log_action(case_id=self.case_id, app=self.app, tool=name, args=_safe_args(args),
                              plan_step=self.current_step, approval="held", outcome=why)
        return {"error": f"Not clicked yet: {why} Then click Execute again. If it is not approved, don't click it; "
                         "note the listed SQL and continue."}

    def _deny(self, name, args, why):
        self.state.log_action(case_id=self.case_id, app=self.app, tool=name, args=_safe_args(args),
                              plan_step=self.current_step, approval="denied", outcome=why)
        return {"error": f"Blocked: {why}. Do not retry this; continue or call finish()."}


MAX_PAGE_CHARS = int(os.environ.get("TESTER_MAX_PAGE_CHARS", "30000"))   # ~8K tokens per page read
KEEP_PAGE_READS = int(os.environ.get("TESTER_KEEP_PAGE_READS", "2"))
TESTER_MAX_TOKENS = int(os.environ.get("TESTER_MAX_TOKENS", "2048"))
DROPPED = "[older page read removed to save space; take a new browser_snapshot if you need the page]"


def prune_history(contents, keep: int) -> int:
    """Replace all but the last `keep` browser tool results in the request history. Returns how many were dropped."""
    refs = []
    for c in contents:
        for p in (getattr(c, "parts", None) or []):
            fr = getattr(p, "function_response", None)
            if fr is not None and (getattr(fr, "name", "") or "").startswith("browser_"):
                refs.append(fr)
    dropped = 0
    for fr in refs[:-keep] if keep > 0 else refs:
        if fr.response != {"result": DROPPED}:
            fr.response = {"result": DROPPED}
            dropped += 1
    return dropped


def _cap_obj(o, limit: int):
    if isinstance(o, str):
        return o if len(o) <= limit else (o[:limit] + f"\n[... page text cut: {len(o) - limit} more characters. "
                                          "Use browser_find, or scroll and snapshot again, for the rest.]")
    if isinstance(o, dict):
        return {k: _cap_obj(v, limit) for k, v in o.items()}
    if isinstance(o, list):
        return [_cap_obj(v, limit) for v in o]
    return o


def _is_context_error(e: BaseException) -> bool:
    t = f"{type(e).__name__} {e}"
    return "ContextWindow" in t or "maximum context length" in t


def manual_login(run_dir: Path, app: str, url: str, username: str | None, input_fn=input, out=print) -> bool:
    """Open a visible browser so a person can log in (and solve a CAPTCHA) once. Saves storage state to
    runs/<run>/storage/<app>.json, closes Chrome cleanly, then testers reload that file. Returns True when done."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    profile = profile_path(run_dir, app)
    storage_state_path(run_dir, app).parent.mkdir(parents=True, exist_ok=True)
    profile.mkdir(parents=True, exist_ok=True)
    shots = run_dir / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    storage = storage_state_path(run_dir, app)
    storage.parent.mkdir(parents=True, exist_ok=True)
    clear_stale_profile_locks(profile)
    mark_clean_exit(profile)
    # --caps=storage only here: needed for browser_storage_state. Testers load via --storage-state, no storage caps.
    args = ["-y", "@playwright/mcp@latest", "--user-data-dir", str(profile), "--output-dir", str(shots),
            "--viewport-size", "1366,900", "--caps=storage"]

    env = browser_env()
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        out("There's no screen to show a browser on (DISPLAY is not set), so a manual login isn't possible here.")
        return False

    async def go() -> bool:
        async with stdio_client(StdioServerParameters(command="npx", args=args, env=env)) as (r, w):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                res = await sess.call_tool("browser_navigate", {"url": url})
                if getattr(res, "isError", False):
                    out(f"The browser couldn't open {url}: {_text_of(res)[:300]}")
                    return False
                out(f"\nA browser window should now be open on {url}." +
                    (f" Log in as {username}" if username else " Log in") +
                    " (solve the CAPTCHA if there is one), wait for the home page, then press Enter here.")
                out("  After Enter, the session is saved and this window closes cleanly; the tester reopens with it.")
                ok = False
                for attempt in range(3):
                    ans = input_fn("  Press Enter when logged in, or type s to skip: ").strip().lower()
                    if ans in ("s", "skip", "n", "no"):
                        break
                    snap = await sess.call_tool("browser_snapshot", {})
                    m = re.search(r"Page URL:\s*(\S+)", _text_of(snap))
                    page = m.group(1) if m else ""
                    if page and still_on_login(page, url):
                        out(f"  The browser is still on the login page ({page}).")
                        continue
                    ok = True
                    break
                if not ok:
                    await sess.call_tool("browser_close", {})
                    return False
                # Persist cookies + local storage before close (covers session-only cookies the profile would drop).
                saved = await sess.call_tool("browser_storage_state", {"filename": str(storage.resolve())})
                if getattr(saved, "isError", False):
                    out(f"Could not save the login session: {_text_of(saved)[:300]}")
                    await sess.call_tool("browser_close", {})
                    return False
                await sess.call_tool("browser_close", {})
                return True
    try:
        ok = asyncio.run(go())
    except Exception as e:  # noqa: BLE001  no display, npx missing...
        out(f"Couldn't open a browser window for the manual login ({type(e).__name__}: {e}).")
        return False
    if not ok:
        release_profile(profile, timeout=10.0)
        if storage.exists():
            try:
                storage.unlink()
            except OSError:
                pass
        return False
    if not release_profile(profile, timeout=25.0):
        out("Couldn't free the browser profile after login; close any leftover Chrome for this run and press Enter again.")
        return False
    if not storage.is_file():
        out("Login looked done but the session file was not written; try the manual login again.")
        return False
    out(f"Login saved to {storage.name}. The tester will reopen with that session.")
    return True


def still_on_login(page: str, start: str) -> bool:
    """After a manual login the page should have moved off the login form."""
    p = urlparse(page).path.lower().rstrip("/")
    return any(w in p for w in ("login", "signin", "sign-in", "sign_in")) or \
        (page.rstrip("/") == start.rstrip("/") and "login" in start.lower())


def _safe_args(args: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        out[k] = "[masked]" if k in ("text", "value") and isinstance(v, str) and len(v) > 0 and "password" in str(args.get("element", "")).lower() else v
    return json.loads(redact(json.dumps(out, default=str)))


def _text_of(resp) -> str:
    if isinstance(resp, str):
        return resp
    try:
        return json.dumps(resp, default=str) if not hasattr(resp, "content") else "\n".join(
            getattr(c, "text", "") for c in resp.content)
    except Exception:  # noqa: BLE001
        return str(resp)


def _redact_obj(o, keep):
    if isinstance(o, str):
        return redact(o, keep_emails=keep)
    if isinstance(o, dict):
        return {k: _redact_obj(v, keep) for k, v in o.items()}
    if isinstance(o, list):
        return [_redact_obj(v, keep) for v in o]
    return o


def make_tester(settings: Settings, pack, env: str, policy: Policy, run_dir: Path, memory=None):
    """Returns tester(app, brief, state) -> StepResult for the coordinator."""
    evidence = Evidence(run_dir)
    keep = tuple(a.get("username", "") for app in pack.apps.values() for a in app.get("accounts", {}).values()
                 if "@" in a.get("username", ""))

    from ..helpers import load_helpers
    all_helpers = load_helpers(pack)

    def tester(app: str, brief: dict, state: RunState) -> StepResult:
        from google.adk.agents import LlmAgent

        from ..ledger import Ledger
        helpers = {n: h for n, h in all_helpers.items() if h.enabled and h.meta.get("app", "any") in ("any", app)}
        was_ro = policy.read_only
        policy.read_only = bool(brief.get("read_only")) or was_ro
        was_prep = policy.prep
        policy.prep = bool(brief.get("data_prep"))
        try:
            urls = run_urls(pack, env, state)
            toolset = playwright_toolset(run_dir, app)
            if brief.get("max_tool_calls") is not None:
                max_calls = int(brief["max_tool_calls"])
            elif brief.get("discovery"):
                max_calls = max(25, min(40, settings.max_tool_calls_per_case))
            else:
                max_calls = tool_budget(len(brief.get("steps") or []), floor=settings.max_tool_calls_per_case)
            sess = TesterSession(app, brief, state, policy, evidence, max_calls,
                                 settings.max_minutes_per_case, keep_emails=keep, memory=memory, ledger=Ledger(state),
                                 helpers=helpers, pack=pack, env=env, toolset=toolset)
            sess.app_urls = {a: u for a, u in (urls or {}).items() if u}
            base_url = urls.get(app) or pack.url(app, env)
            if brief.get("discovery"):
                instr = prompts.DISCOVERY.format(app_label=pack.apps[app].get("label", app), base_url=base_url, env=env)
            else:
                instr = prompts.TESTER.format(app_label=pack.apps[app].get("label", app), base_url=base_url, env=env)
            if helpers and not brief.get("discovery"):
                instr += "\nTested helpers (prefer them for these routine steps; call run_helper):\n" + "\n".join(
                    f"- {n}: {h.meta['description']} params {json.dumps(h.meta.get('params', {}))}"
                    + (" [changes data: needs approval]" if h.meta.get("changes_data") else "") for n, h in helpers.items())
            if not brief.get("discovery") and getattr(pack, "facts", ""):
                from ..facts_pick import brief_text, relevant
                rules = relevant(pack.facts, brief_text(brief))
                if rules:
                    instr += ("\nProject rules (facts.md, current). They apply even if your steps were written before "
                              "a rule existed: if a step skips a prerequisite a rule requires (for example paying in "
                              "full before Order Delivered), do the prerequisite first and say in notes it was inferred "
                              "from facts.md; if it cannot be done, stop and report BLOCKED with the reason.\n" + rules + "\n")
            if brief.get("read_only") and not brief.get("discovery"):
                instr += "\nThis turn is READ-ONLY: log in and read values only. Never change anything."
            recipes = brief.get("recipes") or []
            disco_notes = (brief.get("discovery_notes") or "").strip()
            if recipes or disco_notes:
                instr += "\nUI map from discovery / memory (follow menu/label paths; do not re-explore from scratch):\n"
                instr += ("On an XSRF / acceptable-referer page, return via Home/menu — never retry the same "
                          "Administration deep-link with browser_navigate.\n")
                for r in recipes:
                    instr += f"- recipe: {r}\n"
                if disco_notes:
                    instr += disco_notes + "\n"
            agent = LlmAgent(name=f"tester_{app}", model=model(settings, max_tokens=TESTER_MAX_TOKENS),
                             instruction=lambda _c: instr, tools=[toolset, *sess.tools()],
                             before_tool_callback=sess.before_tool, after_tool_callback=sess.after_tool,
                             before_model_callback=sess.before_model, after_model_callback=raw_calls.after_model)
            prompt = "Your steps:\n" + json.dumps(brief, indent=1, default=str)

            async def go():
                if not os.environ.get("AGENT_DEBUG"):
                    from ..logsetup import quiet_libraries
                    quiet_libraries()                 # LiteLLM/ADK add console handlers on first use
                try:
                    await run_agent(agent, prompt, settings, trace_path=run_dir / "trace.jsonl",
                                    follow_up=lambda final: nudge_text(final) if not (sess.finished or sess.result.limit_hit) else None)
                except TesterHardStop:
                    pass
                except Exception as e:  # noqa: BLE001
                    if _hard_stop_in(e):
                        return
                    if not _is_context_error(e):
                        raise
                    sess.result.stuck, sess.finished = True, True
                    sess.result.notes = ("the pages read in this case no longer fit in the model's memory "
                                         f"({str(e)[:160]}); rerun the case or split it into smaller cases")
                finally:
                    try:
                        await toolset.close()
                    except Exception:  # noqa: BLE001
                        pass
                    # MCP close returns before Chrome drops SingletonLock; wait so manual_login
                    # (or the next case) does not open a second Chrome on the same profile.
                    release_profile(profile_path(run_dir, app), timeout=25.0)
            asyncio.run(go())
        finally:
            policy.read_only = was_ro
            policy.prep = was_prep
        if not sess.finished and not sess.result.limit_hit:
            sess.result.stuck = True
            sess.result.notes = sess.result.notes or "tester stopped without calling finish()"
        return sess.result

    return tester
