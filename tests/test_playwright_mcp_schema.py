"""Contract: McpBrowser args must match the live Playwright MCP tool schemas.

A fake browser cannot catch ref→target renames; this test starts the real MCP, lists
tools, and checks every argument the helper sends against the published schema.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_tester.helpers import McpBrowser
from agent_tester.helpers.core import login_form as login_mod

# Same package pin the tester installs via npx -y @playwright/mcp@latest (0.0.83 today).
MCP_PACKAGE = "@playwright/mcp@0.0.83"

LOGIN_SNAP = '''- generic [ref=e1]:
  - textbox "Username" [ref=e5]
  - textbox "Password" [ref=e6]
  - button "Log In" [ref=e7]'''
HOME_SNAP = '''- banner [ref=e1]:
  - link "Admin" [ref=e2]
  - heading "Home" [ref=e3]'''


class RecordingMcp:
    """Records args the helper would send; returns canned snapshots so login_form can finish."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._shot = 0

    async def __call__(self, tool: str, args: dict):
        self.calls.append((tool, dict(args)))
        if tool == "browser_evaluate":
            return '{"has_password": false}' if self._shot else '{"has_password": true, "user": {"idx": 0, "name": "user_name", "label": "Username"}, "password": {"idx": 1, "name": "username_password", "label": "Password"}, "submit": {"id": "bigbutton", "label": "Log In"}, "error": ""}'
        if tool == "browser_snapshot":
            # After click, pretend we landed on home.
            return HOME_SNAP if self._shot else LOGIN_SNAP
        if tool == "browser_click":
            self._shot = 1
        if tool == "browser_take_screenshot":
            # Create the file so any path checks in callers are happy.
            fn = args.get("filename")
            if fn:
                Path(fn).parent.mkdir(parents=True, exist_ok=True)
                Path(fn).write_bytes(b"")
        return "ok"


def _helper_calls() -> list[tuple[str, dict]]:
    """Drive McpBrowser the way login_form does and return the MCP args it sent."""
    from agent_tester.helpers import HelperContext
    from agent_tester.preflight.pack_loader import load_pack
    from agent_tester.config import ROOT
    from agent_tester.run_state import RunState

    rec = RecordingMcp()
    browser = McpBrowser(rec)
    pack = load_pack(ROOT / "packs" / "savostore")
    with tempfile.TemporaryDirectory() as d, mock.patch.dict(
        os.environ, {"SAVOSTORE_CRM_PASSWORD__ADMIN": "pw-for-schema-test", "LOGIN_WAIT": "0"}
    ):
        st = RunState(Path(d) / "r")
        shots = st.dir / "shots"
        shots.mkdir()
        ctx = HelperContext(pack=pack, env="staging", app="crm", case_id="TC5",
                            state=st, shot_prefix=str(shots) + "/")
        browser.evidence_dir = shots
        asyncio.run(login_mod.run(browser, ctx, account="admin"))
    return rec.calls


class PlaywrightMcpSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as e:
            raise unittest.SkipTest(f"mcp package not installed: {e}") from e

        async def fetch():
            d = Path(tempfile.mkdtemp(prefix="qa-mcp-schema-"))
            args = ["-y", MCP_PACKAGE, "--headless",
                    "--user-data-dir", str(d / "profile"),
                    "--output-dir", str(d / "shots"),
                    "--viewport-size", "800,600"]
            env = os.environ.copy()
            schemas: dict[str, dict] = {}
            async with stdio_client(StdioServerParameters(command="npx", args=args, env=env)) as (r, w):
                async with ClientSession(r, w) as sess:
                    await sess.initialize()
                    listed = await sess.list_tools()
                    for t in listed.tools:
                        schemas[t.name] = t.input_schema or {}
                    await sess.call_tool("browser_close", {})
            return schemas

        try:
            cls.schemas = asyncio.run(fetch())
        except Exception as e:  # noqa: BLE001  npx/network/display
            raise unittest.SkipTest(f"could not start Playwright MCP: {type(e).__name__}: {e}") from e

    def test_helper_args_match_published_schema(self):
        calls = _helper_calls()
        self.assertTrue(calls, "login_form sent no browser calls")
        unknown_tools = sorted({t for t, _ in calls if t not in self.schemas})
        self.assertEqual(unknown_tools, [], f"helper called tools not in MCP: {unknown_tools}")

        for tool, args in calls:
            schema = self.schemas[tool]
            props = set((schema.get("properties") or {}).keys())
            required = set(schema.get("required") or [])
            sent = set(args.keys())
            extra = sent - props
            missing = required - sent
            self.assertFalse(
                extra,
                f"{tool}: helper sent unknown args {sorted(extra)}; "
                f"schema allows {sorted(props)}. (A rename like ref→target shows up here.)",
            )
            self.assertFalse(
                missing,
                f"{tool}: helper omitted required args {sorted(missing)}; sent {sorted(sent)}",
            )


if __name__ == "__main__":
    unittest.main()
