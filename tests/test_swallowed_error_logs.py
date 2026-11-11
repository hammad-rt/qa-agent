"""Swallowed exceptions in core paths now emit a log (control flow unchanged)."""
from __future__ import annotations

from agent_tester import testing_hermetic as hermetic  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path

from agent_tester.discovery_map import buffer_pending
from agent_tester.plan_cache import load_cached_plan


class _SaveFails:
    def __init__(self):
        self.data = {}

    def save(self):
        raise RuntimeError("save failed")


class SwallowedErrorLogs(unittest.TestCase):
    def test_buffer_pending_logs_warning_and_still_queues(self):
        state = _SaveFails()
        entry = {"app": "crm", "checklist_item": "open list"}
        with self.assertLogs("agent_tester.discovery_map", level="WARNING") as cm:
            buffer_pending(state, entry)
        self.assertEqual(state.data.get("_discovery_map_pending"), [entry])
        self.assertTrue(any("buffering discovery map" in r.getMessage() for r in cm.records))
        self.assertTrue(any(r.exc_info for r in cm.records))

    def test_invalid_cached_plan_logs_debug_and_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "plan.cache.json").write_text(json.dumps({
                "fingerprint": "abc",
                "plan": {"not": "a valid plan"},
            }))
            with self.assertLogs("agent_tester.plan_cache", level="DEBUG") as cm:
                out = load_cached_plan(run, "abc")
        self.assertIsNone(out)
        self.assertTrue(any("cached plan failed validation" in r.getMessage() for r in cm.records))
        self.assertTrue(any(r.exc_info for r in cm.records))


if __name__ == "__main__":
    unittest.main()
