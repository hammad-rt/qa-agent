import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_tester.web import server as W


class ConsoleSaved(unittest.TestCase):
    def test_console_log_written_and_resume_hint(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td)
            (runs / "123-x").mkdir()
            with mock.patch.object(W, "RUNS", runs):
                j = W.Job.__new__(W.Job)
                j.args, j.out, j._buf, j._saved, j.pending = ["test", "123"], "", "", 0, None
                j.run_hint = None
                import threading
                j.lock = threading.Lock()
                j._feed("Pack: savostore. Run folder: /x/runs/123-x\nline two\n")
                j._persist()
                j._feed("more\n")
                j._persist()
                text = (runs / "123-x" / "console.log").read_text()
                self.assertIn("line two", text)
                self.assertEqual(text.count("more"), 1)
                self.assertEqual(text.count("=== "), 1)
        r = W.Job.__new__(W.Job)
        self.assertEqual(Path("runs/184815-1").name, "184815-1")


if __name__ == "__main__":
    unittest.main()
