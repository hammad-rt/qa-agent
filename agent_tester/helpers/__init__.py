"""Helpers (phase 6, FR-31): small, tested browser routines for steps that repeat, so the tester calls one
tool instead of re-discovering the UI each run. Core helpers (login form, table read) live in
agent_tester/helpers/core/, project helpers in packs/<slug>/helpers/. A pack helper with the same name
overrides the core one. Each module defines:

    HELPER = {"name": ..., "app": "any" | "<app key>", "description": ..., "changes_data": bool,
              "action_type": None | "<policy action type>", "params": {"name": "what it is"},
              "needs_config": ["key", ...]}       # keys required under pack.yaml helpers.<name>
    async def run(browser, ctx, **params) -> dict  # {"values": {...}, "screenshots": [...], "notes": "..."}

Helpers never see more than the code gives them: passwords come from env vars inside the helper and are
never returned, logged or shown to the model; data-changing helpers go through the same approval gate."""
from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

from ..redact import redact

CORE_DIR = Path(__file__).parent / "core"
_REF = re.compile(r'^(?P<indent>\s*)-\s+(?P<role>[\w-]+)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?(?P<rest>[^\n]*?)\[ref=(?P<ref>[\w-]+)\]')


class HelperError(RuntimeError):
    pass


def _load_module(path: Path, prefix: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"{prefix}_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not isinstance(getattr(mod, "HELPER", None), dict) or not callable(getattr(mod, "run", None)):
        raise HelperError(f"{path} must define HELPER (dict) and async run(browser, ctx, **params)")
    return mod


@dataclass
class HelperInfo:
    name: str
    module: ModuleType
    source: str                     # "core" or "pack"
    enabled: bool
    why_disabled: str = ""
    config: dict = field(default_factory=dict)

    @property
    def meta(self) -> dict:
        return self.module.HELPER


def load_helpers(pack) -> dict[str, HelperInfo]:
    """Core helpers, then the pack's helpers (same name overrides). Enabled only when pack.yaml
    `helpers.<name>` has every key in needs_config and enabled isn't false."""
    cfg_all = (pack.config.get("helpers") or {}) if pack else {}
    found: dict[str, tuple[ModuleType, str]] = {}
    for d, src in ((CORE_DIR, "core"), ((pack.root / "helpers") if pack else None, "pack")):
        if d and d.is_dir():
            for p in sorted(d.glob("*.py")):
                if p.name.startswith("_"):
                    continue
                mod = _load_module(p, f"helper_{src}_{pack.slug if pack else 'none'}")
                found[mod.HELPER["name"]] = (mod, src)
    out = {}
    for name, (mod, src) in found.items():
        cfg = dict(cfg_all.get(name) or {})
        missing = [k for k in mod.HELPER.get("needs_config", []) if not cfg.get(k)]
        why = ("disabled in pack.yaml" if cfg.get("enabled") is False else
               f"set {', '.join(missing)} under helpers.{name} in pack.yaml" if missing else "")
        out[name] = HelperInfo(name, mod, src, not why, why, cfg)
    return out


# ---- browser adapter over Playwright MCP ------------------------------------------------
@dataclass
class Element:
    role: str
    name: str
    ref: str
    line: str


def parse_snapshot(text: str) -> list[Element]:
    out = []
    for line in (text or "").splitlines():
        m = _REF.match(line)
        if m:
            out.append(Element(m["role"], (m["name"] or "").replace('\\"', '"'), m["ref"], line.strip()))
    return out


class McpBrowser:
    """Thin adapter: call(tool_name, args) -> response text. Works with ADK's MCPTool or a fake in tests."""

    def __init__(self, call, evidence_dir: Path | None = None, guard=None):
        self._call, self.evidence_dir, self.guard = call, evidence_dir, guard
        self.last_snapshot = ""
        self.calls: list[tuple[str, dict]] = []

    async def _do(self, tool: str, args: dict) -> str:
        if self.guard:
            self.guard(tool, args)          # allowlist / risky-click checks; raises HelperError
        self.calls.append((tool, {k: ("[masked]" if k == "text" and args.get("_secret") else v)
                                  for k, v in args.items() if k != "_secret"}))
        res = await self._call(tool, {k: v for k, v in args.items() if k != "_secret"})
        return res if isinstance(res, str) else str(res)

    async def navigate(self, url: str) -> str:
        return await self._do("browser_navigate", {"url": url})

    async def snapshot(self) -> str:
        self.last_snapshot = await self._do("browser_snapshot", {})
        return self.last_snapshot

    async def find(self, label: str | list[str], roles: tuple[str, ...] = (), required: bool = True) -> Element | None:
        labels = [label] if isinstance(label, str) else label
        els = parse_snapshot(self.last_snapshot or await self.snapshot())
        for want in labels:
            w = want.lower()
            for exact in (True, False):
                for e in els:
                    if roles and e.role not in roles:
                        continue
                    if (e.name.lower() == w) if exact else (w in e.name.lower()):
                        return e
        if required:
            raise HelperError(f"not found on page: {' / '.join(labels)}")
        return None

    async def click(self, label, roles=("button", "link", "menuitem", "tab", "checkbox", "radio")) -> str:
        e = await self.find(label, roles)
        out = await self._do("browser_click", {"element": e.name, "ref": e.ref})
        self.last_snapshot = ""
        return out

    async def type(self, label, text: str, secret: bool = False, submit: bool = False) -> str:
        e = await self.find(label, ("textbox", "searchbox", "spinbutton", "combobox"))
        return await self._do("browser_type", {"element": e.name, "ref": e.ref, "text": text, "submit": submit, "_secret": secret})

    async def select(self, label, value: str) -> str:
        e = await self.find(label, ("combobox", "listbox"))
        return await self._do("browser_select_option", {"element": e.name, "ref": e.ref, "values": [value]})

    async def screenshot(self, filename: str) -> str:
        await self._do("browser_take_screenshot", {"filename": filename})
        return filename

    def page_text(self) -> str:
        return redact(self.last_snapshot)


def adk_caller(toolset, tool_context):
    """Build call(tool, args) over an ADK McpToolset from inside a function tool."""
    cache: dict = {}

    async def call(name: str, args: dict):
        if not cache:
            for t in await toolset.get_tools():
                cache[t.name] = t
        if name not in cache:
            raise HelperError(f"browser tool {name} not available")
        res = await cache[name].run_async(args=args, tool_context=tool_context)
        if isinstance(res, dict) and "content" in res:
            return "\n".join(c.get("text", "") for c in res["content"] if isinstance(c, dict))
        content = getattr(res, "content", None)
        if content:
            return "\n".join(getattr(c, "text", "") for c in content)
        return str(res)
    return call


# ---- running a helper under the policy and the action log ----------------------------------
@dataclass
class HelperContext:
    pack: object
    env: str
    app: str
    case_id: str
    config: dict = field(default_factory=dict)
    state: object = None
    policy: object = None
    shot_prefix: str = ""

    def url(self, path: str = "") -> str:
        base = (self.pack.url(self.app, self.env) or "").rstrip("/")
        return path if path.startswith("http") else f"{base}/{path.lstrip('/')}" if path else base + "/"

    def account(self, name: str) -> dict:
        acc = self.pack.accounts(self.app).get(name)
        if not acc:
            raise HelperError(f"no account '{name}' for app {self.app} in pack.yaml")
        return acc


async def run_helper(info: HelperInfo, browser: McpBrowser, ctx: HelperContext, params: dict) -> dict:
    meta = info.meta
    if not info.enabled:
        return {"error": f"helper {info.name} is not set up: {info.why_disabled}"}
    if meta.get("app", "any") not in ("any", ctx.app):
        return {"error": f"helper {info.name} is for app {meta['app']}, not {ctx.app}"}
    unknown = set(params) - set(meta.get("params", {}))
    if unknown:
        return {"error": f"unknown parameter(s): {', '.join(sorted(unknown))}"}
    approval = "not_needed"
    if ctx.policy is not None and meta.get("changes_data"):
        from ..tools.policy import PolicyViolation
        try:
            approval = ctx.policy.gate(f"helper {info.name} {params}", f"{ctx.case_id} on {ctx.app}",
                                       action_type=meta.get("action_type") or "helper_write",
                                       details={"helper": info.name, "params": params})
        except PolicyViolation as e:
            if ctx.state is not None:
                ctx.state.log_action(case_id=ctx.case_id, app=ctx.app, tool=f"helper:{info.name}",
                                     args=params, approval="denied", outcome=str(e))
            return {"error": f"Blocked: {e}. Do not retry this; continue or call finish()."}
    ctx.config = dict(info.config)
    browser.guard = _guard(ctx.policy, meta.get("changes_data", False))
    try:
        out = await info.module.run(browser, ctx, **params)
        outcome = "ok"
    except HelperError as e:
        out, outcome = {"error": str(e)}, f"helper error: {e}"
    if ctx.state is not None:
        ctx.state.log_action(case_id=ctx.case_id, app=ctx.app, tool=f"helper:{info.name}", args=params,
                             approval=approval, outcome=outcome)
    return _clean(out)


_MONEY = re.compile(r"(?:[A-Z]{3}\s?|\$\s?)-?[\d,]+(?:\.\d+)?|-?[\d,]+\.\d{2}")


def read_labelled_values(text: str, labels: list[str]) -> dict:
    """Find "<label> ... <amount>" in the snapshot (same line, or the next lines). Returns raw text per label."""
    lines = (text or "").splitlines()
    out = {}
    for label in labels:
        for i, line in enumerate(lines):
            if label.lower() in line.lower():
                after = line[line.lower().index(label.lower()) + len(label):]
                m = _MONEY.search(after) or next((m for nxt in lines[i + 1:i + 4] if (m := _MONEY.search(nxt))), None)
                if m:
                    out[label] = m.group().strip()
                    break
    return out


async def run_form(browser, ctx, params: dict) -> dict:
    """Config-driven form: pack.yaml helpers.<name> gives path (may use {param}), fields {param: label},
    selects {param: label}, submit_label, success_text and read_labels. Nothing is guessed: unset labels
    keep the helper disabled."""
    cfg = ctx.config
    await browser.navigate(ctx.url(cfg["path"].format(**params)))
    await browser.snapshot()
    for name, label in (cfg.get("fields") or {}).items():
        if name in params:
            await browser.type(label, str(params[name]))
    for name, label in (cfg.get("selects") or {}).items():
        if name in params:
            await browser.select(label, str(params[name]))
    await browser.click(cfg["submit_label"])
    page = await browser.snapshot()
    ok = (cfg.get("success_text", "").lower() in page.lower()) if cfg.get("success_text") else None
    shot = await browser.screenshot(f"{ctx.shot_prefix}{ctx.case_id}-{cfg.get('name', 'form')}.png")
    values = read_labelled_values(page, cfg.get("read_labels") or [])
    if ok is not None:
        values["success_message_seen"] = "yes" if ok else "no"
    return {"values": values, "screenshots": [shot]}


def _guard(policy, may_change: bool):
    """Same checks the tester callbacks apply to the model's own browser calls."""
    def check(tool: str, args: dict):
        if policy is None:
            return
        from ..tools.policy import PolicyViolation
        try:
            if tool == "browser_navigate":
                policy.check_url(args.get("url", ""))
            elif tool in ("browser_click", "browser_type", "browser_select_option") and not may_change:
                label = f"{args.get('element', '')} {args.get('values', '')}"
                if policy.is_risky(label) or args.get("submit"):
                    raise HelperError(f"read-only helper tried a risky action: {label.strip()}")
        except PolicyViolation as e:
            raise HelperError(str(e)) from e
    return check


def _clean(o):
    if isinstance(o, str):
        return redact(o)
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


def candidates(memory_store, slug: str, min_success: int = 3) -> list[dict]:
    """Recipes that kept working: suggest turning them into helpers (FR-31)."""
    rows = memory_store.query([f"project:{slug}"], ["recipe"])
    return sorted(({"id": r["id"], "text": r["text"], "area": r.get("area"), "success_count": r["success_count"]}
                   for r in rows if r["success_count"] >= min_success), key=lambda r: -r["success_count"])
