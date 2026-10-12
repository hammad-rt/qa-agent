import os
import unittest
from unittest import mock

import requests

from agent_tester import main as M
from agent_tester.preflight.redmine import Redmine, RedmineError


class FriendlyErrors(unittest.TestCase):
    def test_messages_are_plain(self):
        self.assertIn("Couldn't connect", M.friendly_error(requests.ConnectionError("x")))
        self.assertIn("disk is full", M.friendly_error(OSError(28, "No space")))
        self.assertTrue(M.friendly_error(RedmineError("down")).startswith("Redmine:"))

    def test_main_prints_message_not_traceback(self):
        with mock.patch.object(M, "_main", side_effect=RedmineError("Redmine is not responding (503)")), \
             mock.patch.dict(os.environ, {"AGENT_DEBUG": ""}), mock.patch("builtins.print") as p, \
             mock.patch.object(M, "_save_trace", return_value="agent_error.log"):
            self.assertEqual(M.main([]), 1)
        self.assertIn("503", " ".join(str(c) for c in p.call_args_list))

    def test_redmine_retries_5xx(self):
        s = mock.Mock()
        s.get.return_value = mock.Mock(status_code=503)
        with mock.patch("agent_tester.preflight.redmine.time.sleep"):
            with self.assertRaises(RedmineError) as cm:
                Redmine("https://r", "k", session=s)._get("/issues/1.json")
        self.assertEqual(s.get.call_count, 3)
        self.assertIn("not responding", str(cm.exception))
