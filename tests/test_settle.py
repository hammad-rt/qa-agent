import unittest
from types import SimpleNamespace
from unittest import mock
from agent_tester.agents import tester as T


class Settle(unittest.TestCase):
    def test_record_after_action_needs_fresh_snapshot_and_snapshot_waits(self):
        sess = mock.MagicMock(calls=0, _last_act=0.0, _last_snap=0.0)
        sess.before_tool = lambda *a: T.TesterSession.before_tool(sess, *a)
        tool = lambda n: SimpleNamespace(name=n)
        with mock.patch.dict("os.environ", {"TESTER_SETTLE_SECONDS": "0.2"}), mock.patch.object(T.time, "sleep") as sl:
            sess.before_tool(tool("browser_click"), {}, None)
            r = sess.before_tool(tool("record_value"), {}, None)
            self.assertIn("fresh browser_snapshot", r["error"])
            sess.before_tool(tool("browser_snapshot"), {}, None)
            self.assertTrue(sl.called)
