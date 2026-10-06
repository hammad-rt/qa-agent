"""Per-app tester (LLM + Playwright MCP). Every tool call goes through code callbacks that:
build the action log, enforce the domain allowlist and approvals, cap tool calls, mask page text,
and collect values and evidence. The StepResult is assembled by code, not by the model."""
from __future__ import annotations

import asyncio
import json
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


def playwright_toolset(run_dir: Path, app: str, allowed_hosts: list[str]):
    from mcp import StdioServerParameters
    try:
        from google.adk.tools.mcp_tool.mcp_toolset import McpToolset as Toolset
    except ImportError:  # older ADK
        from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset as Toolset
    from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams

    out = run_dir / "shots"
    profile = run_dir / "profiles" / app
    profile.mkdir(parents=True, exist_ok=True)
    origins = ";".join(f"https://{h}" for h in allowed_hosts)
    args = ["-y", "@playwright/mcp@latest", "--headless", "--isolated", "--output-dir", str(out),
            "--viewport-size", "1366,900"]
    if origins:
        args += ["--allowed-origins", origins]
    return Toolset(connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(command="npx", args=args), timeout=60))


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

        tools = [record_value, calculate, request_approval, mark_step, evidence_name, get_secret, finish, recall_lessons]
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
            return _redact_obj(tool_response, self.keep_emails)
        return None

    def _deny(self, name, args, why):
        self.state.log_action(case_id=self.case_id, app=self.app, tool=name, args=_safe_args(args),
                              plan_step=self.current_step, approval="denied", outcome=why)
        return {"error": f"Blocked: {why}. Do not retry this; continue or call finish()."}


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
            toolset = playwright_toolset(run_dir, app, policy.allow_hosts)
            sess = TesterSession(app, brief, state, policy, evidence, settings.max_tool_calls_per_case,
                                 settings.max_minutes_per_case, keep_emails=keep, memory=memory, ledger=Ledger(state),
                                 helpers=helpers, pack=pack, env=env, toolset=toolset)
            instr = prompts.TESTER.format(app_label=pack.apps[app].get("label", app), base_url=pack.url(app, env), env=env)
            if helpers:
                instr += "\nTested helpers (prefer them for these routine steps; call run_helper):\n" + "\n".join(
                    f"- {n}: {h.meta['description']} params {json.dumps(h.meta.get('params', {}))}"
                    + (" [changes data: needs approval]" if h.meta.get("changes_data") else "") for n, h in helpers.items())
            if brief.get("read_only"):
                instr += "\nThis turn is READ-ONLY: log in and read values only. Never change anything."
            agent = LlmAgent(name=f"tester_{app}", model=model(settings), instruction=lambda _c: instr,
                             tools=[toolset, *sess.tools()],
                             before_tool_callback=sess.before_tool, after_tool_callback=sess.after_tool)
            prompt = "Your steps:\n" + json.dumps(brief, indent=1, default=str)

            async def go():
                try:
                    await run_agent(agent, prompt, settings, trace_path=run_dir / "trace.jsonl")
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
