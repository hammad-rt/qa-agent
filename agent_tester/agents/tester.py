"""Per-app tester (LLM + Playwright MCP). Every tool call goes through code callbacks that:
build the action log, enforce the domain allowlist and approvals, cap tool calls, mask page text,
and collect values and evidence. The StepResult is assembled by code, not by the model."""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlparse
import os
import time
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
    profile = run_dir / "profiles" / app
    profile.mkdir(parents=True, exist_ok=True)
    # one browser profile per app per run: logging in once (or a person solving a CAPTCHA once) lasts the run
    args = ["-y", "@playwright/mcp@latest", *([] if headed() else ["--headless"]), "--user-data-dir", str(profile),
            "--output-dir", str(out), "--viewport-size", "1366,900"]
    return Toolset(connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(command="npx", args=args, env=browser_env()), timeout=60))


class TesterSession:
    """Holds the per-turn bookkeeping that the callbacks and tool functions share."""

    def __init__(self, app: str, brief: dict, state: RunState, policy: Policy, evidence: Evidence,
                 max_calls: int, max_minutes: int, keep_emails: tuple[str, ...] = (), memory=None, ledger=None,
                 helpers: dict | None = None, pack=None, env: str = "staging", toolset=None):
        self.app, self.brief, self.state, self.policy, self.ev = app, brief, state, policy, evidence
        self.case_id = brief["case_id"]
        self.max_calls, self.deadline = max_calls, time.time() + max_minutes * 60
        self.calls = 0
        self.result = StepResult(case_id=self.case_id, app=app)
        self.finished = False
        self.keep_emails = keep_emails
        self.last_snapshot = ""
        self.current_step: int | None = None
        self.memory, self.ledger = memory, ledger
        self.helpers, self.pack, self.env, self.toolset = helpers or {}, pack, env, toolset
        self.off_site: str | None = None      # page address outside this run's sites, until the user allows it or we go back

    # ---- tools exposed to the model -------------------------------------------------
    def tools(self):
        s = self

        def record_value(name: str, raw_text: str, source: str) -> dict:
            """Record a value exactly as read on the page. raw_text is the text you saw, e.g. "$1,234.50"."""
            try:
                v = V.parse_money(raw_text)
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

        def mark_step(n: int, deviation: str = "", inferred: bool = False, reason: str = "") -> dict:
            """Mark plan step n as done. Describe any difference from the plan in deviation."""
            s.current_step = n
            if n not in s.result.steps_done:
                s.result.steps_done.append(n)
            s.state.log_action(case_id=s.case_id, app=s.app, tool="mark_step", args={"n": n, "reason": reason},
                               plan_step=n, inferred=inferred, deviation=deviation or None)
            return {"ok": True}

        def evidence_name(what: str) -> dict:
            """Get the filename to pass to browser_take_screenshot for evidence of `what`."""
            png, txt = s.ev.paths(s.case_id, what)
            s.result.screenshots.append(png.name)
            if s.last_snapshot:
                s.ev.save_snapshot(txt, s.last_snapshot, keep_emails=s.keep_emails)
                s.result.snapshots.append(txt.name)
            return {"filename": png.name}

        def get_secret(env_name: str) -> dict:
            """Return the password stored in env var env_name, only for typing into a login form."""
            allowed = {r.get("secret_env") for r in s.brief["requirements"].values() if r.get("secret_env")}
            if env_name not in allowed or not os.environ.get(env_name):
                return {"error": f"{env_name} is not available for this run"}
            return {"value": os.environ[env_name]}

        def needs_manual_login(reason: str) -> dict:
            """Call when login is blocked by a CAPTCHA or robot check. Do not try to solve it."""
            s.result.needs_login = redact(reason) or "CAPTCHA at login"
            s.result.notes, s.result.stuck, s.finished = f"needs a manual login: {s.result.needs_login}", True, True
            return {"ok": True, "next": "stop now; a person will log in and the case will be retried"}

        def finish(notes: str, stuck: bool = False) -> dict:
            """Call when your steps are done, or when you are stuck and cannot continue."""
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
            browser = McpBrowser(adk_caller(s.toolset, tool_context))
            ctx = HelperContext(pack=s.pack, env=s.env, app=s.app, case_id=s.case_id, state=s.state,
                                policy=s.policy, shot_prefix="")
            out = await _run(info, browser, ctx, params)
            for k, v in (out.get("values") or {}).items():
                s.result.values[f"{name}.{k}"] = v
            s.result.screenshots += [x for x in out.get("screenshots", []) if x not in s.result.screenshots]
            return out

        tools = [record_value, calculate, request_approval, mark_step, evidence_name, get_secret, finish, recall_lessons,
                 needs_manual_login]
        if s.helpers and s.toolset is not None:
            tools.append(run_helper)
        if not s.brief.get("read_only"):
            tools += [propose_memory, ledger_record, record_original, record_restored]
        return tools

    # ---- callbacks: the code side of every tool call --------------------------------
    def before_tool(self, tool, args, tool_context):
        name = tool.name
        self.calls += 1
        if self.calls > self.max_calls or time.time() > self.deadline:
            self.result.limit_hit = True
            return {"error": "Limit reached. Call finish() now with what you have."} if name != "finish" else None
        if name in BLOCKED_BROWSER:
            return self._deny(name, args, "this browser tool is not allowed")
        if name.startswith("browser_"):
            if self.off_site and name not in ("browser_navigate", "browser_navigate_back", "browser_snapshot",
                                              "browser_take_screenshot"):
                return self._deny(name, args, f"the browser is on {self.off_site}, which this run may not use; "
                                              "go back (browser_navigate_back) or navigate to the app")
            try:
                if name == "browser_navigate":
                    self.policy.check_url(args.get("url", ""))
                label = " ".join(str(args.get(k, "")) for k in ("element", "text", "values") if args.get(k))
                if name not in READ_ONLY_BROWSER and name != "browser_navigate":
                    risky = self.policy.is_risky(label) or bool(args.get("submit"))
                    if risky:
                        self.policy.gate(f"{name}: {label}", f"{self.case_id} on {self.app}", risky=True,
                                         details={"tool": name, "args": _safe_args(args)})
            except PolicyViolation as e:
                return self._deny(name, args, str(e))
            self.state.log_action(case_id=self.case_id, app=self.app, tool=name, args=_safe_args(args),
                                  plan_step=self.current_step)
        return None

    def after_tool(self, tool, args, tool_context, tool_response):
        if tool.name == "browser_snapshot":
            self.last_snapshot = _text_of(tool_response)
        if tool.name.startswith("browser_"):
            blocked = self._check_page_site(tool.name, _text_of(tool_response))
            if blocked:
                return {"error": blocked}
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
    """Open a visible browser on the same profile the tester uses, so a person can log in (and solve a CAPTCHA)
    once. The login then lasts for the rest of the run. Returns True when the person says they're done."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    profile = run_dir / "profiles" / app
    profile.mkdir(parents=True, exist_ok=True)
    args = ["-y", "@playwright/mcp@latest", "--user-data-dir", str(profile), "--output-dir", str(run_dir / "shots"),
            "--viewport-size", "1366,900"]

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
                try:
                    await sess.call_tool("browser_close", {})
                except Exception:  # noqa: BLE001
                    pass
                return ok
    try:
        return asyncio.run(go())
    except Exception as e:  # noqa: BLE001  no display, npx missing...
        out(f"Couldn't open a browser window for the manual login ({type(e).__name__}: {e}).")
        return False


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
        try:
            urls = run_urls(pack, env, state)
            toolset = playwright_toolset(run_dir, app)
            sess = TesterSession(app, brief, state, policy, evidence, settings.max_tool_calls_per_case,
                                 settings.max_minutes_per_case, keep_emails=keep, memory=memory, ledger=Ledger(state),
                                 helpers=helpers, pack=pack, env=env, toolset=toolset)
            instr = prompts.TESTER.format(app_label=pack.apps[app].get("label", app), base_url=urls.get(app) or pack.url(app, env), env=env)
            if helpers:
                instr += "\nTested helpers (prefer them for these routine steps; call run_helper):\n" + "\n".join(
                    f"- {n}: {h.meta['description']} params {json.dumps(h.meta.get('params', {}))}"
                    + (" [changes data: needs approval]" if h.meta.get("changes_data") else "") for n, h in helpers.items())
            if brief.get("read_only"):
                instr += "\nThis turn is READ-ONLY: log in and read values only. Never change anything."
            agent = LlmAgent(name=f"tester_{app}", model=model(settings, max_tokens=TESTER_MAX_TOKENS),
                             instruction=lambda _c: instr, tools=[toolset, *sess.tools()],
                             before_tool_callback=sess.before_tool, after_tool_callback=sess.after_tool,
                             before_model_callback=sess.before_model)
            prompt = "Your steps:\n" + json.dumps(brief, indent=1, default=str)

            async def go():
                try:
                    await run_agent(agent, prompt, settings, trace_path=run_dir / "trace.jsonl")
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
            asyncio.run(go())
        finally:
            policy.read_only = was_ro
        if not sess.finished and not sess.result.limit_hit:
            sess.result.stuck = True
            sess.result.notes = sess.result.notes or "tester stopped without calling finish()"
        return sess.result

    return tester
