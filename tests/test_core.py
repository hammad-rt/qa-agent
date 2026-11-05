import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_tester.config import ROOT
from agent_tester.models import CaseResult, CaseType, PlannedCase, Requirement, Step, StepResult, TestPlan
from agent_tester.redact import redact
from agent_tester.tools import values as V
from agent_tester.tools.policy import Policy, PolicyViolation, from_pack
from agent_tester.plan_checks import check_plan, execution_order
from agent_tester.preflight.pack_loader import find_pack, load_pack, NoPackFound
from agent_tester.preflight import gap_resolver as G
from agent_tester.memory.resources import ResourceMemory
from agent_tester.run_state import RunState
from agent_tester.coordinator import RunPaused, app_turns, run_plan

PACK = ROOT / "packs" / "savostore"


class Values(unittest.TestCase):
    def test_calculate(self):
        self.assertEqual(V.calculate("0.10 * ship", {"ship": 400}), 40)
        self.assertEqual(V.calculate("min(a, 0.1*b)", {"a": 60, "b": 1000}), 60)

    def test_calculate_rejects_code(self):
        for bad in ("__import__('os')", "a.b", "open('x')", "[1]"):
            with self.assertRaises(V.CalcError):
                V.calculate(bad, {"a": 1})
        with self.assertRaises(V.CalcError):
            V.calculate("missing * 2", {})

    def test_parse_money(self):
        self.assertEqual(V.parse_money("$1,234.50"), 1234.5)
        self.assertEqual(V.parse_money("KES 50,000"), 50000)
        self.assertEqual(V.parse_money("-$20.50"), -20.5)


class Redaction(unittest.TestCase):
    def test_masks(self):
        t = redact("card 4242 4242 4242 4242 mail a@b.com password: hunter2 call +254 712 345 678")
        self.assertIn("[card ****4242]", t)
        self.assertNotIn("a@b.com", t)
        self.assertNotIn("hunter2", t)
        self.assertNotIn("712 345", t)

    def test_keep_test_account(self):
        self.assertIn("sidney@savostore.com", redact("user sidney@savostore.com", keep_emails=("sidney@savostore.com",)))

    def test_money_untouched(self):
        self.assertEqual(redact("Total $1,234.50 and 10%"), "Total $1,234.50 and 10%")


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.pack = load_pack(PACK)

    def test_allowlist(self):
        p = from_pack(self.pack, "staging")
        p.check_url("https://staging5.rolustech.com:4225/x")
        with self.assertRaises(PolicyViolation):
            p.check_url("https://evil.example.com/")
        with self.assertRaises(PolicyViolation):
            p.check_url("https://app.savostore.com/")  # prod host not allowed on staging

    def test_preapproval_rules(self):
        p = from_pack(self.pack, "staging")
        p.preapprove(["pay_staging_test_card"])
        self.assertEqual(p.gate("pay", "TC3", action_type="pay_staging_test_card"), "pre_approved")
        p.preapprove(["change_settings"])
        self.assertEqual(p.gate("save settings", "TC9", action_type="change_settings"), "pre_approved")
        prod = from_pack(self.pack, "prod")
        with self.assertRaises(PolicyViolation):
            prod.preapprove(["change_settings"])

    def test_approver_and_denial(self):
        asked = []
        p = from_pack(self.pack, "staging", approver=lambda a, r, d: asked.append(a) or a.startswith("ok"))
        self.assertEqual(p.gate("ok click Pay", "x", risky=True), "approved")
        with self.assertRaises(PolicyViolation):
            p.gate("no click Delete", "x", risky=True)
        self.assertEqual(len(asked), 2)
        self.assertEqual(p.gate("read page", "x"), "not_needed")

    def test_prod_never_preapproved(self):
        p = from_pack(self.pack, "prod", approver=lambda *a: True)
        p.preapproved.add("create_test_order")
        self.assertEqual(p.gate("create", "x", action_type="create_test_order"), "approved")

    def test_risky_labels(self):
        p = from_pack(self.pack, "staging")
        self.assertTrue(p.is_risky("Pay now"))
        self.assertTrue(p.is_risky("button Place Order"))
        self.assertFalse(p.is_risky("View order"))


def _case(cid, steps, **kw):
    return PlannedCase(id=cid, title=cid, steps=steps, **kw)


class PlanChecks(unittest.TestCase):
    def test_clean_plan(self):
        plan = TestPlan(task_id=1, cases=[_case("TC1", [Step(text="open", app="portal")])])
        self.assertEqual(check_plan(plan), [])

    def test_cycle_and_unknown_dep(self):
        plan = TestPlan(task_id=1, cases=[_case("A", [], depends_on=["B"]), _case("B", [], depends_on=["A", "Z"])])
        issues = " | ".join(check_plan(plan))
        self.assertIn("cycle", issues)
        self.assertIn("unknown case Z", issues)

    def test_irreversible_must_be_last(self):
        pay = Step(text="pay", app="portal", changes_data=True, costs_money=True, irreversible=True,
                   action_type="pay_staging_test_card", record="order:A1")
        look = Step(text="edit", app="portal", changes_data=True, action_type="add_parcel", record="order:A1")
        plan = TestPlan(task_id=1, cases=[_case("TC1", [pay]), _case("TC2", [look], depends_on=["TC1"])])
        self.assertTrue(any("irreversible" in i for i in check_plan(plan)))
        look.record = "order:FRESH"
        self.assertFalse(any("irreversible" in i for i in check_plan(plan)))

    def test_admin_restore_inferred_and_approval(self):
        s1 = Step(text="change threshold", app="crm", changes_data=True)
        s2 = Step(text="login", app="crm", inferred=True)
        plan = TestPlan(task_id=1, cases=[_case("TC9", [s2, s1], type=CaseType.ADMIN_CONFIG),
                                          _case("TC10", [], type=CaseType.NOT_UI)])
        issues = " | ".join(check_plan(plan))
        self.assertIn("no restore step", issues)
        self.assertIn("inferred step without reason", issues)
        self.assertIn("no approval or action type", issues)
        self.assertIn("skip_reason", issues)

    def test_order(self):
        plan = TestPlan(task_id=1, cases=[_case("C", [], depends_on=["B"]), _case("A", []), _case("B", [])])
        self.assertEqual(execution_order(plan), ["B", "C", "A"])


class PackTests(unittest.TestCase):
    def test_load_and_find(self):
        pack = find_pack(ROOT / "packs", [1567])
        self.assertEqual(pack.slug, "savostore")
        self.assertEqual(len(pack.known_bugs), 9)
        self.assertEqual(pack.url("crm", "staging"), "https://staging5.rolustech.com:4220/")
        with self.assertRaises(NoPackFound):
            find_pack(ROOT / "packs", [99999])

    def test_no_secrets_in_pack(self):
        text = (PACK / "pack.yaml").read_text().lower()
        self.assertNotIn("password:", text)


class GapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = ResourceMemory(Path(self.tmp.name) / "res.json")
        self.pack = load_pack(PACK)

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, env="staging", wiki=None, task="", checker=lambda u: True, pack=None):
        pack = pack or self.pack
        return G.GapContext(pack=pack, env=env, memory=self.mem, wiki_texts=wiki or {}, task_text=task,
                            url_checker=checker, allow_hosts=list(pack.allowlist(env)))

    def test_pack_url_resolves(self):
        r = G.resolve([Requirement(key="env.staging.portal")], self.ctx())[0]
        self.assertEqual((r.status, r.source), ("resolved", "pack"))

    def test_unreachable_then_wiki_question(self):
        wiki = {"Staging_Cardentials": "CRM staging\nhttps://staging5.rolustech.com:4999/\nuser: x password: SECRET"}
        r = G.resolve([Requirement(key="env.staging.crm")], self.ctx(wiki=wiki, checker=lambda u: False))[0]
        self.assertEqual(r.status, "unverified")
        self.assertIn("Staging_Cardentials", r.question)
        self.assertNotIn("SECRET", r.question)

    def test_missing_asks_and_saves_answer(self):
        bare = load_pack(PACK)
        bare.config = {**bare.config, "apps": {"portal": {"label": "Customer portal", "envs": {}}}}
        ctx = self.ctx(env="uat", pack=bare)
        r = G.resolve([Requirement(key="env.uat.portal")], ctx)[0]
        self.assertEqual(r.status, "missing")
        self.assertIn("Where is Customer portal on uat", r.question)
        G.apply_answers([r], {"env.uat.portal": "https://uat.example.com/"}, ctx, None, lambda *a: None)
        self.assertEqual(r.status, "resolved")
        self.assertIn("uat.example.com", ctx.allow_hosts)
        # next run finds it in memory
        r2 = G.resolve([Requirement(key="env.uat.portal")], ctx)[0]
        self.assertEqual(r2.status, "resolved")
        self.assertTrue(r2.source.startswith("memory"))

    def test_account_secret_flow(self):
        os.environ.pop("SAVOSTORE_CRM_PASSWORD", None)
        ctx = self.ctx()
        r = G.resolve([Requirement(key="account.crm.admin")], ctx)[0]
        self.assertEqual(r.status, "missing")
        self.assertEqual(r.secret_env, "SAVOSTORE_CRM_PASSWORD__ADMIN")
        self.assertEqual(r.must_have, ["Admin"])
        stored = {}

        def write(name, val):
            stored[name] = val
            os.environ[name] = val
        G.apply_answers([r], {}, ctx, secret_prompt=lambda p: "s3cret", write_secret=write)
        self.assertEqual(r.status, "resolved")
        self.assertNotIn("s3cret", (Path(self.tmp.name) / "res.json").read_text())
        os.environ.pop("SAVOSTORE_CRM_PASSWORD", None)

    def test_data_requirement(self):
        ctx = self.ctx()
        r = G.resolve([Requirement(key="data.rewards_balance_100")], ctx)[0]
        self.assertEqual(r.status, "missing")
        G.apply_answers([r], {"data.rewards_balance_100": "customer sidney has $120"}, ctx, None, lambda *a: None)
        self.assertEqual(G.resolve([Requirement(key="data.rewards_balance_100")], ctx)[0].status, "resolved")


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = RunState(Path(self.tmp.name) / "run")

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self):
        return TestPlan(task_id=180966, cases=[
            _case("TC1", [Step(text="a", app="portal"), Step(text="b", app="crm"), Step(text="c", app="portal")],
                  expected="x"),
            _case("TC2", [Step(text="d", app="portal")], depends_on=["TC1"]),
            _case("TC3", [], type=CaseType.NOT_UI, skip_reason="webhook retry"),
        ])

    def test_turns(self):
        self.assertEqual(app_turns(self.plan().cases[0]), [("portal", [0]), ("crm", [1]), ("portal", [2])])

    def test_run_and_blocked(self):
        calls = []

        def tester(app, brief, state):
            calls.append((brief["case_id"], app, [s["n"] for s in brief["steps"]]))
            state.log_action(case_id=brief["case_id"], app=app, tool="browser_click", args={})
            return StepResult(case_id=brief["case_id"], app=app, values={"cap": 40.0})

        def judge(brief, actions, results, state):
            self.assertNotIn("notes", str(brief))  # judge never gets tester reasoning
            v = "FAIL" if brief["case_id"] == "TC1" else "PASS"
            return CaseResult(case_id=brief["case_id"], verdict=v, actual="y", values={"cap": 40.0})

        res = run_plan(self.plan(), self.state, {"portal": tester, "crm": tester}, judge)
        self.assertEqual(calls, [("TC1", "portal", [1]), ("TC1", "crm", [2]), ("TC1", "portal", [3])])
        self.assertEqual(res["TC1"].verdict, "FAIL")
        self.assertEqual(res["TC2"].verdict, "BLOCKED")
        self.assertEqual(res["TC3"].verdict, "SKIPPED")
        self.assertEqual(len(self.state.actions_for("TC1")), 3)

    def test_incomplete(self):
        tester = lambda app, brief, state: StepResult(case_id=brief["case_id"], app=app, limit_hit=True)
        res = run_plan(self.plan(), self.state, {"portal": tester, "crm": tester},
                       lambda *a: self.fail("judge should not run"))
        self.assertEqual(res["TC1"].verdict, "AGENT INCOMPLETE")

    def test_crash_marks_case_and_continues(self):
        def flaky(app, brief, state):
            raise ConnectionError("LLM down")
        res = run_plan(self.plan(), self.state, {"portal": flaky, "crm": flaky}, lambda *a: None)
        self.assertEqual(res["TC1"].verdict, "AGENT INCOMPLETE")
        self.assertEqual(len(res), 3)          # the run went on past the crash

    def test_resume_after_crash(self):
        ok = lambda app, brief, state: StepResult(case_id=brief["case_id"], app=app)
        judge = lambda b, a, r, s: CaseResult(case_id=b["case_id"], verdict="PASS")
        res = run_plan(self.plan(), RunState(self.state.dir), {"portal": ok, "crm": ok}, judge)
        self.assertEqual(res["TC2"].verdict, "PASS")


class TesterCallbacks(unittest.TestCase):
    def setUp(self):
        from agent_tester.agents.tester import TesterSession
        from agent_tester.tools.screenshots import Evidence
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.state = RunState(d / "run")
        self.asked = []
        pack = load_pack(PACK)
        self.policy = from_pack(pack, "staging", approver=lambda a, r, dd: self.asked.append(a) or False)
        brief = {"case_id": "TC3", "requirements": {"account.portal.customer": {"secret_env": "X_PW"}}}
        self.s = TesterSession("portal", brief, self.state, self.policy, Evidence(self.state.dir), max_calls=5, max_minutes=5)
        self.tools = {f.__name__: f for f in self.s.tools()}

    def tearDown(self):
        self.tmp.cleanup()

    def t(self, name):
        return SimpleNamespace(name=name)

    def test_navigation_allowlist(self):
        self.assertIsNone(self.s.before_tool(self.t("browser_navigate"), {"url": "https://staging5.rolustech.com:4225/"}, None))
        self.assertIn("error", self.s.before_tool(self.t("browser_navigate"), {"url": "https://google.com"}, None))

    def test_risky_click_denied_and_logged(self):
        r = self.s.before_tool(self.t("browser_click"), {"element": "Pay now button", "ref": "e1"}, None)
        self.assertIn("Blocked", r["error"])
        self.assertEqual(self.asked, ["browser_click: Pay now button"])
        self.assertEqual(self.state.actions_for("TC3")[-1]["approval"], "denied")

    def test_evaluate_blocked(self):
        self.assertIn("error", self.s.before_tool(self.t("browser_evaluate"), {"function": "()=>1"}, None))

    def test_limit(self):
        # reads are free: 5 snapshots don't use the 5-action budget
        for _ in range(5):
            self.assertIsNone(self.s.before_tool(self.t("browser_snapshot"), {}, None))
        for i in range(5):
            self.s.before_tool(self.t("browser_navigate"), {"url": f"https://shop.example/p{i}"}, None)
        self.assertIn("Limit", self.s.before_tool(self.t("browser_navigate"), {"url": "https://shop.example/z"}, None)["error"])
        self.assertTrue(self.s.result.limit_hit)

    def test_runaway_reads_still_stop(self):
        import agent_tester.agents.tester as T
        errs = []
        with self.assertRaises(T.TesterHardStop):       # told to stop, kept reading: code stops it
            for _ in range(40):
                r = self.s.before_tool(self.t("browser_snapshot"), {}, None)
                if r:
                    errs.append(r["error"])
        self.assertTrue(any("re-reading" in e for e in errs))

    def test_loop_warn_then_stop(self):
        self.s.max_calls = 100
        args = {"expression": "1+1"}
        rs = [self.s.before_tool(self.t("calculate"), dict(args), None) for _ in range(5)]
        self.assertIsNone(rs[0]); self.assertIsNone(rs[1])
        self.assertIn("did not move", rs[2]["error"])
        self.assertIn("Explore before you act", rs[4]["error"])      # first stall: explore, don't stop
        self.assertFalse(self.s.result.limit_hit)
        rs = [self.s.before_tool(self.t("calculate"), dict(args), None) for _ in range(5)]
        self.assertIn("Stop now", rs[4]["error"])                    # same loop after exploring: stop
        self.assertTrue(self.s.result.loop and self.s.result.limit_hit)

    def test_idle_rereads_explore_then_stop(self):
        import agent_tester.agents.tester as T
        self.s.max_calls = 100
        r = [self.s.before_tool(self.t("browser_snapshot"), {}, None) for _ in range(T.IDLE_READS)]
        self.assertIn("Explore before you act", r[-1]["error"])
        r = [self.s.before_tool(self.t("browser_snapshot"), {}, None) for _ in range(T.IDLE_READS)]
        self.assertIn("re-reading the page", r[-1]["error"])
        self.assertTrue(self.s.result.loop)

    def test_rereads_between_actions_are_fine(self):
        self.s.max_calls = 100
        for i in range(20):
            r = self.s.before_tool(self.t("browser_snapshot"), {}, None)
            self.assertIsNone(r)
            self.s.before_tool(self.t("calculate"), {"expression": str(i)}, None)

    def test_cycle_is_a_loop(self):
        import agent_tester.agents.tester as T
        self.assertTrue(T._is_cycle(["a", "b"] * 3))
        self.assertTrue(T._is_cycle(["x", "a", "b", "c"] + ["a", "b", "c"] * 2))
        self.assertFalse(T._is_cycle(["a", "b", "c", "d", "e", "f"]))
        self.s.max_calls = 100
        r = [self.s.before_tool(self.t("calculate"), {"expression": e}, None) for e in ["1", "2"] * 3]
        self.assertIn("Explore before you act", r[-1]["error"])

    def test_label_from_snapshot(self):
        import agent_tester.agents.tester as T
        snap = '- button "Save" [ref=f4e12]\n- link "Edit" [ref=f4e13]'
        self.assertEqual(T._intent_sig("browser_click", {"target": "f4e12"}, snap), "click:button save")
        snap2 = '- button "Save" [ref=f7e90]'
        self.assertEqual(T._intent_sig("browser_click", {"target": "f7e90"}, snap2), "click:button save")

    def test_mark_step_renews_step_allowance(self):
        import agent_tester.agents.tester as T
        self.s.max_calls = 100
        old = T.ACTIONS_PER_STEP
        T.ACTIONS_PER_STEP = 3
        try:
            for i in range(3):
                self.assertIsNone(self.s.before_tool(self.t("calculate"), {"expression": f"a{i}"}, None))
            self.s.before_tool(self.t("mark_step"), {"n": 1}, None)
            self.s.result.steps_done.append(1)
            for i in range(3):
                self.assertIsNone(self.s.before_tool(self.t("calculate"), {"expression": f"b{i}"}, None))
            self.assertIn("Explore", self.s.before_tool(self.t("calculate"), {"expression": f"c"}, None)["error"])
            for i in range(4 + T.EXPLORE_ACTIONS):
                r = self.s.before_tool(self.t("calculate"), {"expression": f"d{i}"}, None)
            self.assertIn("Limit", r["error"])
        finally:
            T.ACTIONS_PER_STEP = old

    def test_values_and_calc(self):
        self.tools["record_value"]("shipping_unpaid", "$400.00", "order A1158 summary")
        self.assertEqual(self.tools["calculate"]("0.10 * shipping_unpaid"), {"result": 40.0})
        self.assertEqual(self.state.values["shipping_unpaid"]["value"], 400.0)

    def test_secret_scope(self):
        # Passwords must never be returned to the model (use login_form helper instead).
        self.assertIn("error", self.tools["get_secret"]("HOME"))
        os.environ["X_PW"] = "pw"
        out = self.tools["get_secret"]("X_PW")
        self.assertIn("error", out)
        self.assertNotIn("value", out)
        self.assertNotIn("pw", str(out))
        os.environ.pop("X_PW")

    def test_page_text_redacted(self):
        out = self.s.after_tool(self.t("browser_snapshot"), {}, None, {"text": "card 4242424242424242"})
        self.assertNotIn("4242424242424242", str(out))


class JudgeRules(unittest.TestCase):
    def test_enforce(self):
        from agent_tester.agents.judge import enforce_rules
        r = enforce_rules(CaseResult(case_id="T", verdict="PASS"), {"evidence_required": ["cap shown"]}, [], [])
        self.assertEqual(r.verdict, "NOT VERIFIED")
        r = enforce_rules(CaseResult(case_id="T", verdict="FAIL"), {}, [], [])
        self.assertEqual(r.verdict, "NOT VERIFIED")
        r = enforce_rules(CaseResult(case_id="T", verdict="PASS", note="ok"), {},
                          [{"deviation": "button renamed"}], [])
        self.assertIn("deviation", r.note)


class ReportTests(unittest.TestCase):
    def test_build(self):
        from agent_tester.report.builder import build_report
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            st.set_plan(TestPlan(task_id=5, cases=[_case("TC1", [Step(text="login", app="portal", inferred=True,
                                                                            inferred_reason="needed", inferred_source="facts.md")])]))
            st.set_result(CaseResult(case_id="TC1", verdict="FAIL", known_bug=2, actual="a@b.com saw $34"))
            html = build_report(st).read_text()
            self.assertIn("still present: bug 2", html)
            self.assertNotIn("a@b.com", html)
            self.assertIn("Inferred steps", html)


if __name__ == "__main__":
    unittest.main()


class AfterFinish(unittest.TestCase):
    def test_calls_after_finish_end_the_turn(self):
        import agent_tester.agents.tester as T
        cls = T.TesterSession
        s = cls.__new__(cls)
        s.calls, s.finished = 0, True
        tool = type("Tool", (), {"name": "mark_step"})()
        with self.assertRaises(T.TesterHardStop):
            s.before_tool(tool, {"n": 1}, None)


class RefIntents(unittest.TestCase):
    def test_same_element_after_reload_is_same_intent(self):
        import agent_tester.agents.tester as T
        a = T._intent_sig("browser_click", {"target": "f3e125"}, "")
        b = T._intent_sig("browser_click", {"target": "f9e125"}, "")
        self.assertEqual(a, b)
        self.assertEqual(T._intent_sig("browser_type", {"target": "f3e116", "text": "Hammad"}),
                         T._intent_sig("browser_type", {"target": "f6e116", "text": "Hammad"}))

    def test_label_from_trailing_text(self):
        import agent_tester.agents.tester as T
        self.assertEqual(T._label_of("f3e125", "- generic [ref=f3e125]: Iqbal"), "generic iqbal")
        self.assertEqual(T._label_of("f3e126", "- generic [ref=f3e126]: "), "")

    def test_search_cycle_with_new_refs_is_caught(self):
        import agent_tester.agents.tester as T
        seq = []
        for f in (3, 6, 9):
            seq += [T._intent_sig("browser_type", {"target": f"f{f}e116", "text": "x"}),
                    T._intent_sig("browser_click", {"target": f"f{f}e125"})]
        self.assertTrue(T._is_cycle(seq))


class StatusWalk(unittest.TestCase):
    def test_save_after_each_new_status_is_not_a_loop(self):
        t = TesterCallbacks("test_loop_warn_then_stop"); t.setUp()
        s = t.s; s.max_calls = 200
        for st in ["Shipped to Warehouse", "On Warehouse floor", "Ready to Ship", "Shipped", "Invoiced & Shipped"]:
            r1 = s.before_tool(t.t("browser_select_option"), {"target": "f5e554", "values": [st]}, None)
            r2 = s.before_tool(t.t("browser_click"), {"target": "f5e114", "element": "Save"}, None)
            for r in (r1, r2):
                self.assertTrue(r is None or "Explore" not in r.get("error", "") and "Stop now" not in r.get("error", ""), r)

    def test_save_with_same_value_still_loops(self):
        t = TesterCallbacks("test_loop_warn_then_stop"); t.setUp()
        s = t.s; s.max_calls = 200
        errs = []
        for _ in range(5):
            for r in (s.before_tool(t.t("browser_select_option"), {"target": "f5e554", "values": ["Shipped"]}, None),
                      s.before_tool(t.t("browser_click"), {"target": "f5e114"}, None)):
                if r: errs.append(r.get("error", ""))
        self.assertTrue(any("Explore" in e or "did not move" in e for e in errs))
