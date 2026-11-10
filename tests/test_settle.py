import unittest
from types import SimpleNamespace
from unittest import mock
from agent_tester.agents import tester as T


class Settle(unittest.TestCase):
    def test_record_after_action_needs_fresh_snapshot_and_snapshot_waits(self):
        sess = mock.MagicMock(calls=0, _last_act=0.0, _last_snap=0.0)
        sess._budget_check = mock.Mock(return_value=None)  # click counts early; do not short-circuit
        sess._need_fresh_snap = False
        sess.before_tool = lambda *a: T.TesterSession.before_tool(sess, *a)
        tool = lambda n: SimpleNamespace(name=n)
        with mock.patch.dict("os.environ", {"TESTER_SETTLE_SECONDS": "0.2"}), mock.patch.object(T.time, "sleep") as sl:
            sess.before_tool(tool("browser_click"), {}, None)
            r = sess.before_tool(tool("record_value"), {}, None)
            self.assertIn("fresh browser_snapshot", r["error"])
            sess.before_tool(tool("browser_snapshot"), {}, None)
            self.assertTrue(sl.called)


class IntentLoop(unittest.TestCase):
    def test_reworded_clicks_share_intent(self):
        snap = '- link "View Invoice" [ref=f1e348]\n'
        a = T._intent_sig("browser_click", {"element": "View Invoice", "target": "f1e348"}, snap)
        b = T._intent_sig("browser_click", {"target": "[ref=f1e348]"}, snap)
        self.assertEqual(a, b)
        # Element name alone still groups changing refs when the snapshot has no match yet.
        c = T._intent_sig("browser_click", {"element": "View Invoice", "ref": "e999"})
        d = T._intent_sig("browser_click", {"element": "View Invoice", "ref": "e1000"})
        self.assertEqual(c, d)
