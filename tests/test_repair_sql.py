import unittest

from agent_tester.repair_sql import ADDITIVE, DESTRUCTIVE, classify, statements

ADD_SQL = """/*Checking Table: aos_invoices */
ALTER TABLE aos_invoices add column shipment_value_c decimal(26,6) NULL ;
ALTER TABLE aos_invoices ADD delivery_phone varchar(100) NULL , ADD INDEX idx_phone (delivery_phone);
CREATE TABLE savo_parcels (id char(36) NOT NULL, name varchar(255) DEFAULT 'drop me' NULL, PRIMARY KEY (id));
CREATE INDEX idx_x ON savo_parcels (name);"""


class RepairSql(unittest.TestCase):
    def test_additive(self):
        out = classify(ADD_SQL)
        self.assertEqual(out["action_type"], ADDITIVE, out)
        self.assertEqual(len(out["additive"]), 4)

    def test_destructive_anywhere(self):
        for bad in ["ALTER TABLE a MODIFY COLUMN x int", "ALTER TABLE a ADD b int, DROP c",
                    "ALTER TABLE a CHANGE x y int", "DROP TABLE a", "UPDATE a SET x=1", "TRUNCATE a",
                    "ALTER TABLE a ADD PRIMARY KEY (id)"]:
            out = classify(ADD_SQL + "\n" + bad + ";")
            self.assertEqual(out["action_type"], DESTRUCTIVE, bad)
            self.assertEqual(len(out["risky"]), 1, bad)

    def test_empty(self):
        self.assertIsNone(classify("  /* nothing */ \n")["action_type"])

    def test_split_respects_quotes(self):
        self.assertEqual(len(statements("ALTER TABLE a ADD b varchar(5) DEFAULT 'x;y';ALTER TABLE a ADD c int")), 2)


class ExecuteGuard(unittest.TestCase):
    def _session(self, approve=True):
        import tempfile
        from pathlib import Path
        from agent_tester.agents.tester import TesterSession
        from agent_tester.run_state import RunState
        from agent_tester.tools.policy import Policy
        self.tmp = tempfile.TemporaryDirectory()
        st = RunState(Path(self.tmp.name) / "r")
        pol = Policy(allow_hosts=["x.test"], risky_patterns=[], preapprovable=[ADDITIVE], always_ask=[DESTRUCTIVE],
                     approver=lambda a, r, d: approve)
        return TesterSession("crm", {"case_id": "TC0", "steps": []}, st, pol, evidence=None, max_calls=30, max_minutes=5)

    def tearDown(self):
        self.tmp.cleanup()

    def _tool(self, s, name):
        return next(t for t in s.tools() if getattr(t, "__name__", "") == name)

    def test_execute_held_until_checked_and_approved(self):
        s = self._session()
        self.assertTrue(hasattr(s, "_hold"))
        self.assertFalse(s._exec_ok)
        out = self._tool(s, "check_repair_sql")(ADD_SQL)
        self.assertEqual(out["action_type"], ADDITIVE)
        self.assertFalse(s._exec_ok)
        self.assertTrue(self._tool(s, "request_approval")("change_settings", "x")["approved"])
        self.assertFalse(s._exec_ok)                       # a different approval doesn't unlock Execute
        self.assertTrue(self._tool(s, "request_approval")(ADDITIVE, "add new fields")["approved"])
        self.assertTrue(s._exec_ok)

    def test_destructive_denied_stays_locked(self):
        s = self._session(approve=False)
        self._tool(s, "check_repair_sql")("ALTER TABLE a DROP b")
        self.assertFalse(self._tool(s, "request_approval")(DESTRUCTIVE, "drop")["approved"])
        self.assertFalse(s._exec_ok)


if __name__ == "__main__":
    unittest.main()
