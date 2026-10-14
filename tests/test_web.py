import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_tester.web import server as W


class WebUI(unittest.TestCase):
    def test_summary_and_safe_paths(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "1-x"
            d.mkdir()
            (d / "state.json").write_text(json.dumps({"status": "done", "plan": {"task_id": 1, "cases": [{}]},
                                                      "results": {"A": {"verdict": "PASS"}}}))
            with mock.patch.object(W, "RUNS", Path(t)):
                s = W.run_summary(d)
            self.assertEqual((s["task"], s["counts"]), (1, {"PASS": 1}))
            self.assertIsNone(W._safe(d, "../../etc/passwd"))
            self.assertIsNotNone(W._safe(d, "state.json"))

    def test_run_dir_from_output(self):
        self.assertEqual(W._run_dir_from("Pack: x. Run folder: /a/runs/182941-20261014-1\n"), "182941-20261014-1")


class KnowledgeApi(unittest.TestCase):
    def test_knowledge_lists_pack_learned(self):
        from agent_tester.web.server import knowledge
        k = {x["pack"]: x for x in knowledge()}
        self.assertIn("savostore", k)
        self.assertIn("## ", k["savostore"]["learned"])


class MemoryApi(unittest.TestCase):
    def test_approve_edit_delete(self):
        import os, tempfile
        from pathlib import Path
        from agent_tester.memory.store import MemoryStore
        from agent_tester.web import server
        with tempfile.TemporaryDirectory() as t:
            db = Path(t) / "m.db"
            s = MemoryStore(db, None)
            mid = s.add("recipe", "project:x", "old text", "run", status="proposed"); s.close()
            old = os.environ.get("MEMORY_DB"); os.environ["MEMORY_DB"] = str(db)
            try:
                self.assertEqual(server.memory_change(mid, "approve"), {"ok": True})
                self.assertEqual(server.memory_change(mid, "edit", "new text"), {"ok": True})
                rows = server.memory_list()
                self.assertEqual((rows[0]["status"], rows[0]["text"]), ("active", "new text"))
                self.assertNotIn("value_or_ref", rows[0])
                self.assertEqual(server.memory_change(mid, "delete"), {"ok": True})
                self.assertEqual(server.memory_list(), [])
                self.assertIn("error", server.memory_change(999, "approve"))
            finally:
                os.environ.pop("MEMORY_DB", None)
                if old: os.environ["MEMORY_DB"] = old
