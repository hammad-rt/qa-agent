import io
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_tester import logsetup


class LogSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (logsetup.LOG_DIR, logsetup._file_handler, sys.excepthook, logging.getLogger().handlers[:],
                    logging.getLogger().level)
        logsetup.LOG_DIR = Path(self.tmp.name) / "logs"
        logsetup._file_handler = None

    def tearDown(self):
        root = logging.getLogger()
        for h in root.handlers[:]:
            if h not in self.old[3]:
                root.removeHandler(h)
                h.close()
        logsetup.LOG_DIR, logsetup._file_handler, sys.excepthook = self.old[:3]
        root.setLevel(self.old[4])
        self.tmp.cleanup()

    def test_tracebacks_go_to_file_not_console(self):
        lib = logging.getLogger("LiteLLM")
        err = io.StringIO()
        noisy = logging.StreamHandler(sys.stderr)
        lib.addHandler(noisy)
        with mock.patch.dict(os.environ, {"AGENT_DEBUG": ""}), mock.patch.object(sys, "stderr", err):
            noisy.stream = sys.stderr
            path = logsetup.setup()
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                lib.exception("lib failed")
        logsetup._file_handler.flush()
        self.assertNotIn(noisy, lib.handlers)
        self.assertEqual(err.getvalue(), "")
        text = path.read_text()
        self.assertIn("Traceback", text)
        self.assertIn("boom", text)

    def test_run_errors_file(self):
        with mock.patch.dict(os.environ, {"AGENT_DEBUG": ""}):
            logsetup.setup()
            run = Path(self.tmp.name) / "run"
            run.mkdir()
            with logsetup.RunErrors(run):
                logging.getLogger("x").warning("careful")
                logging.getLogger("x").info("chatty")
            t = (run / "errors.log").read_text()
        self.assertIn("careful", t)
        self.assertNotIn("chatty", t)

    def test_short_error(self):
        with mock.patch.dict(os.environ, {"AGENT_DEBUG": ""}):
            logsetup.setup()
            self.assertEqual(logsetup.short_error(Exception("OpenAIException - Connection error.\nmore")),
                             "the model server could not be reached")
            self.assertEqual(logsetup.short_error(ValueError("bad\nline2")), "ValueError: bad")

    def test_debug_keeps_console(self):
        self.assertIsNone(logsetup.setup(debug=True))


if __name__ == "__main__":
    unittest.main()
