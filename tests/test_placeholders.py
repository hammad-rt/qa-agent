"""Placeholder table, data plan, multi-app create parts, literal-token block."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest import mock

import yaml

from agent_tester import placeholders as P
from agent_tester.models import PlannedCase, Step, TestPlan
from agent_tester.run_state import RunState


class TokenScan(unittest.TestCase):
    def test_scan_order_tokens(self):
        self.assertEqual(P.scan_tokens("Open ORDER-MULTI then ORDER-EARN and ORDER-MULTI again"),
                         ["ORDER-MULTI", "ORDER-EARN"])

    def test_prose_does_not_count_as_token(self):
        cfg = yaml.safe_load(open("packs/savostore/pack.yaml"))
        pat = P.token_pattern(cfg)
        case = PlannedCase(
            id="B9", title="Customer can view wallet balance",
            steps=[Step(text="Log in as the customer and open the wallet page", app="portal")])
        self.assertEqual(P.tokens_in_case(case, pat, pack_cfg=cfg), [])
        rows = P.build_data_plan(TestPlan(task_id=1, cases=[case]), cfg)
        self.assertEqual(rows, [])

    def test_explicit_caps_and_braced_still_resolve(self):
        cfg = yaml.safe_load(open("packs/savostore/pack.yaml"))
        pat = P.token_pattern(cfg)
        case = PlannedCase(
            id="B9", title="x",
            steps=[Step(text="Use ORDER-MULTI and CUSTOMER and {WALLET}", app="portal")])
        self.assertEqual(P.tokens_in_case(case, pat, pack_cfg=cfg),
                         ["ORDER-MULTI", "CUSTOMER", "WALLET"])


class DataPlan(unittest.TestCase):
    def test_build_plan_for_selected_cases(self):
        cfg = yaml.safe_load(open("packs/savostore/pack.yaml"))
        plan = TestPlan(task_id=1, cases=[
            PlannedCase(id="E1", title="Earn", area="rewards",
                        steps=[Step(text="Pay ORDER-EARN by card", app="portal")]),
            PlannedCase(id="B9", title="Multi", area="rewards",
                        steps=[Step(text="Open ORDER-MULTI invoice", app="portal")]),
        ])
        rows = P.build_data_plan(plan, cfg)
        tokens = [r["token"] for r in rows]
        self.assertIn("ORDER-EARN", tokens)
        self.assertIn("ORDER-MULTI", tokens)
        earn = next(r for r in rows if r["token"] == "ORDER-EARN")
        self.assertEqual([p["app"] for p in earn["create"]], ["portal", "crm"])
        self.assertIn("portal", P.format_data_plan(rows).lower())

    def test_unknown_token_not_planned(self):
        """ORDER-* only counts when it is a defined pack item; unknown names do not prep or block."""
        cfg = {"placeholders": {"items": []}}
        plan = TestPlan(task_id=1, cases=[
            PlannedCase(id="T", title="x", steps=[Step(text="ORDER-ZZZ", app="portal")])])
        self.assertEqual(P.build_data_plan(plan, cfg), [])
        self.assertEqual(P.scan_tokens("ORDER-ZZZ", pack_cfg=cfg), [])


class ResolveInject(unittest.TestCase):
    def test_inject_and_unresolved(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            cfg = yaml.safe_load(open("packs/savostore/pack.yaml"))
            P.store_resolved(st, "ORDER-MULTI", value="A9", record="r9",
                             value_key="order_multi", prep_id="shipping_only_order_multi",
                             aliases=["test_order"])
            brief = P.inject_placeholders({"values_so_far": {}}, st, cfg)
            self.assertEqual(brief["placeholders"]["ORDER-MULTI"]["value"], "A9")
            self.assertIn("never search", brief["placeholder_note"].lower())
            self.assertEqual(st.values["test_order"]["value"], "A9")
            case = PlannedCase(id="B9", title="x",
                               steps=[Step(text="Open ORDER-MULTI", app="portal")])
            self.assertEqual(P.case_unresolved_tokens(case, st, cfg), [])
            case2 = PlannedCase(id="E1", title="x",
                                steps=[Step(text="Open ORDER-EARN", app="portal")])
            self.assertEqual(P.case_unresolved_tokens(case2, st, cfg), ["ORDER-EARN"])


class Checklist(unittest.TestCase):
    def test_merge_from_cases_and_plan(self):
        cases = [PlannedCase(id="E1", title="Earn", area="rewards", steps=[
            Step(text="create order on portal", app="portal"),
            Step(text="record a payment in the Payments subpanel", app="crm"),
            Step(text="move order status to Order Delivered", app="crm"),
        ])]
        items = P.checklist_from_cases(cases, "crm", limit=10)
        self.assertTrue(any("payment" in i for i in items))
        self.assertTrue(any("status" in i for i in items))
        plan_items = P.discovery_items_from_plan([{
            "token": "ORDER-EARN", "find_app": "crm", "find": "find it",
            "create": [{"app": "portal", "text": "Create shipping-only order."},
                       {"app": "crm", "text": "Set parcel Received; invoice shipping."}],
            "verify_app": "portal",
        }], "crm")
        self.assertTrue(any("ORDER-EARN" in i for i in plan_items))
        merged = P.merge_checklist(items, plan_items, limit=10)
        self.assertLessEqual(len(merged), 10)


if __name__ == "__main__":
    unittest.main()
