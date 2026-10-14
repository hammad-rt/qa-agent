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
              "finish", "needs_manual_login", "recall_lessons", "propose_memory", "get_secret"}
ACTIONS_PER_STEP = int(os.environ.get("TESTER_ACTIONS_PER_STEP", "30"))   # actions allowed without step progress
LOOP_WARN, LOOP_STOP = 3, 5          # same call repeated this often without progress: warn, then stop


ACTING_TOOLS = {"browser_click", "browser_type", "browser_fill_form", "browser_select_option", "browser_press_key",
                "browser_navigate", "browser_navigate_back", "browser_drag", "browser_drop", "browser_file_upload",
                "browser_handle_dialog", "run_helper"}


def _intent_sig(name: str, args: dict) -> str | None:
    """What the call is aimed at, ignoring wording: a click on the same ref, or a script quoting the same texts."""
    if name == "browser_click":
        ref = re.sub(r"\W|ref", "", str(args.get("target") or args.get("ref") or ""))
        return f"click:{ref}" if ref else None
    if name in ("browser_run_code_unsafe", "browser_evaluate"):
        texts = sorted(set(re.findall(r"""['"]([^'"]{3,60})['"]""", str(args.get("code") or args.get("function") or ""))))
        return "code:" + "|".join(texts) if texts else None
    return None


def _settle_seconds(app: str = "") -> float:
    if app == "crm":                                  # CRM subpanels/wallet totals load ~8 s late
        return float(os.environ.get("TESTER_SETTLE_SECONDS_CRM", "10"))
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


def storage_state_path(run_dir: Path, app: str) -> Path:
    """Cookies + local storage saved after a manual login; testers reload this instead of hoping the profile survived."""
    return run_dir / "storage" / f"{app}.json"


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
    profile = run_dir / "profiles" / app
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

    # ---- tools exposed to the model -------------------------------------------------
    def tools(self):
        s = self

        def record_value(name: str, raw_text: str, source: str) -> dict:
            """Record a value exactly as read on the page. raw_text is the text you saw, e.g. "$1,234.50"."""
            try:
                # IDs like A1142 / B1465 stay text: only plain amounts become numbers
                v = raw_text.strip() if re.search(r"[A-Za-z]\d|\d[A-Za-z]|^[A-Za-z]{1,3}-?\d+$", raw_text.strip()) \
                    and not re.match(r"^(USD|KES|EUR|GBP|PKR)\b", raw_text.strip(), re.I) else V.parse_money(raw_text)
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
                return {"approved": True, "how": how}
            except PolicyViolation as e:
                s.state.log_action(case_id=s.case_id, app=s.app, tool="request_approval",
                                   args={"action_type": action_type, "description": description},
                                   plan_step=s.current_step, approval="denied", outcome=str(e))
                return {"approved": False, "reason": str(e)}

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

        async def finish(notes: str, tool_context, stuck: bool = False) -> dict:
            """Call when your steps are done, or when you are stuck and cannot continue.
            A final proof screenshot is taken automatically."""
            await s.auto_shot("final", tool_context)
            s.result.notes, s.result.stuck, s.finished = redact(notes), stuck, True
            return {"ok": True}

        def recall_lessons(query: str) -> dict:
            """Search saved lessons, recipes and known bugs for this project, e.g. "rewards apply button"."""
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

        tools = [record_value, calculate, request_approval, mark_step, evidence_name, get_secret, finish, recall_lessons,
                 needs_manual_login]
        if s.helpers and s.toolset is not None:
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
        settle = _settle_seconds(getattr(self, "app", ""))
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
        if name == "mark_step" and args.get("n") not in self.result.steps_done:
            self.since_progress, self._sigs = 0, {}          # real progress: fresh per-step allowance
        limit = self._budget_check(name, args)
        if limit:
            return limit
        if name in BLOCKED_BROWSER:
            return self._deny(name, args, "this browser tool is not allowed")
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
                    self.policy.check_url(url)
                    self._last_nav_url = url
                label = " ".join(str(args.get(k, "")) for k in ("element", "text", "values") if args.get(k))
                if name not in READ_ONLY_BROWSER and name != "browser_navigate":
                    risky = self.policy.is_risky(label) or bool(args.get("submit"))
                    action_type = self._step_action_type()
                    # Close the hole where Save on a change_settings step skipped the console gate.
                    if action_type and action_type not in self._action_gated:
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
            self.state.log_action(case_id=self.case_id, app=self.app, tool=name, args=_safe_args(args),
                                  plan_step=self.current_step)
        return None

    def _budget_check(self, name: str, args: dict):
        """Progress-based budget. Reads (snapshots, screenshots, recording) are free; actions are capped per plan
        step (reset by each new mark_step) and in total; the same call repeated without progress is a loop."""
        if name == "finish":
            return None
        over_time = time.time() > self.deadline
        if not over_time and self.calls > 4 * self.max_calls:
            over_time = True                                 # runaway reads
        if name not in FREE_TOOLS:
            self.actions += 1
            self.since_progress += 1
            sig = _call_sig(name, args)
            self._sigs[sig] = self._sigs.get(sig, 0) + 1
            intent = _intent_sig(name, args)              # same target reached by reworded calls/scripts
            if intent:
                self._sigs[intent] = self._sigs.get(intent, 0) + 1
            n = max(self._sigs[sig], self._sigs.get(intent, 0) if intent else 0)
            clicky = name in ("browser_click", "browser_run_code_unsafe", "browser_evaluate")
            warn_at, stop_at = (2, 4) if clicky else (LOOP_WARN, LOOP_STOP)   # a page click that did nothing: rethink at once
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
                                 "the same purpose). When the other route works, call propose_memory with it as a recipe "
                                 "so the next run goes straight there. If no route exists, finish(stuck=True)."}
            if self.since_progress > int(self.brief.get("actions_per_step") or ACTIONS_PER_STEP) or self.actions > self.max_calls:
                over_time = True
        if over_time:
            self.result.limit_hit = True
            return {"error": "Limit reached. Call finish() now with what you have, saying which step you reached."}
        return None

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
            blocked = self._check_page_site(tool.name, text)
            if blocked:
                return {"error": blocked}
            xsrf = self._note_xsrf(tool.name, args, text)
            if xsrf:
                return {"error": xsrf}
            return _cap_obj(_redact_obj(tool_response, self.keep_emails), MAX_PAGE_CHARS)
        return None

    def before_model(self, callback_context, llm_request):
        """Keep the model's history small: only the last KEEP_PAGE_READS browser results stay whole; older ones
        are replaced by a one-line placeholder (the tester can take a new snapshot)."""
        prune_history(getattr(llm_request, "contents", None) or [], KEEP_PAGE_READS)
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
    profile = run_dir / "profiles" / app
    profile.mkdir(parents=True, exist_ok=True)
    shots = run_dir / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    storage = storage_state_path(run_dir, app)
    storage.parent.mkdir(parents=True, exist_ok=True)
    clear_stale_profile_locks(profile)
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
            base_url = urls.get(app) or pack.url(app, env)
            if brief.get("discovery"):
                instr = prompts.DISCOVERY.format(app_label=pack.apps[app].get("label", app), base_url=base_url, env=env)
            else:
                instr = prompts.TESTER.format(app_label=pack.apps[app].get("label", app), base_url=base_url, env=env)
            if helpers and not brief.get("discovery"):
                instr += "\nTested helpers (prefer them for these routine steps; call run_helper):\n" + "\n".join(
                    f"- {n}: {h.meta['description']} params {json.dumps(h.meta.get('params', {}))}"
                    + (" [changes data: needs approval]" if h.meta.get("changes_data") else "") for n, h in helpers.items())
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
                try:
                    await run_agent(agent, prompt, settings, trace_path=run_dir / "trace.jsonl",
                                    follow_up=lambda final: nudge_text(final) if not (sess.finished or sess.result.limit_hit) else None)
                except Exception as e:  # noqa: BLE001
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
                    release_profile(run_dir / "profiles" / app, timeout=25.0)
            asyncio.run(go())
        finally:
            policy.read_only = was_ro
            policy.prep = was_prep
        if not sess.finished and not sess.result.limit_hit:
            sess.result.stuck = True
            sess.result.notes = sess.result.notes or "tester stopped without calling finish()"
        return sess.result

    return tester
