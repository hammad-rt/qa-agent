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
