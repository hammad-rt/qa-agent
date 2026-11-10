"""Secret scrubbing, value-kind validation, finish sanity, find→create budget."""
from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace as N
from unittest import mock

from agent_tester import data_prep as D
from agent_tester.finish_sanity import classify_finish
from agent_tester.models import PlannedCase, Step
from agent_tester.redact import (
    SCRUB_CALL_SITES, effective_api_key, for_user, is_placeholder_secret,
    scrub_secrets, strip_redaction_tokens, warn_skipped_secrets,
)
from agent_tester.run_state import RunState
from agent_tester.tools.value_kind import infer_kind, validate_value


class RedactSecrets(unittest.TestCase):
    def test_placeholder_api_key_does_not_scrub_none(self):
        env = {"LLM_API_KEY": "none", "SAVOSTORE_CRM_PASSWORD": "hunter22x"}
        t = scrub_secrets("found none; pw hunter22x; answer (none)", env)
        self.assertIn("found none", t)
        self.assertIn("(none)", t)
        self.assertIn("[SAVOSTORE_CRM_PASSWORD]", t)
        self.assertNotIn("hunter22x", t)

    def test_short_and_common_skipped(self):
        for v in ("none", "null", "x", "-", "true", "12345", "sk-none", "EMPTY"):
            self.assertTrue(is_placeholder_secret(v), v)
        self.assertFalse(is_placeholder_secret("real-secret-value-99"))

    def test_token_boundaries(self):
        env = {"X_API_KEY": "secret99"}
        self.assertEqual(scrub_secrets("secret99x and secret99 end", env),
                         "secret99x and [X_API_KEY] end")

    def test_for_user_hides_tokens(self):
        self.assertEqual(for_user("saw [LLM_API_KEY] here"), "saw (hidden value) here")
        self.assertEqual(strip_redaction_tokens("a [FOO_PASSWORD] b"), "a (hidden value) b")

    def test_effective_api_key(self):
        self.assertEqual(effective_api_key("none"), "no-key")
        self.assertEqual(effective_api_key("sk-real-key-here"), "sk-real-key-here")

    def test_warn_skipped_names_only(self):
        lines = []
        warn_skipped_secrets({"LLM_API_KEY": "none", "OK_PASSWORD": "longsecret1"},
                             out=lines.append)
        self.assertEqual(len(lines), 1)
        self.assertIn("LLM_API_KEY", lines[0])
        self.assertNotIn("none", lines[0])

    def test_state_file_keeps_real_secret_scrubbed(self):
        os.environ["X_TEST_PASSWORD"] = "Zq9!longsecret"
        try:
            with tempfile.TemporaryDirectory() as d:
                st = RunState(__import__("pathlib").Path(d) / "r")
                st.data["notes"].append("typed Zq9!longsecret into the form")
                st.save()
                text = st.path.read_text()
                self.assertNotIn("Zq9!longsecret", text)
                self.assertIn("[X_TEST_PASSWORD]", text)
        finally:
            os.environ.pop("X_TEST_PASSWORD", None)

    def test_scrub_call_sites_documented(self):
        self.assertIn("run_state.save", SCRUB_CALL_SITES)
        self.assertIn("progress._write", SCRUB_CALL_SITES)


class ValueKind(unittest.TestCase):
    def test_infer(self):
        self.assertEqual(infer_kind("order_earn"), "order_number")
        self.assertEqual(infer_kind("crm_contact_id"), "record_id")
        self.assertEqual(infer_kind("customer_email"), "email")
        self.assertEqual(infer_kind("wallet_ready"), "money")
        self.assertEqual(infer_kind("order_earn_ok"), "yes_no")

    def test_reject_none_and_tokens(self):
        r = validate_value("order_earn", "none")
        self.assertFalse(r["ok"])
        self.assertTrue(r["not_found"])
        self.assertFalse(validate_value("order_earn", "ORDER-MULTI",
                                        tokens={"ORDER-MULTI"})["ok"])
        self.assertFalse(validate_value("order_earn", "[LLM_API_KEY]")["ok"])
        self.assertFalse(validate_value("order_earn", "{client}")["ok"])

    def test_accept_real_values(self):
        self.assertTrue(validate_value("order_earn", "A1234")["ok"])
        self.assertTrue(validate_value("customer_email", "a@b.com")["ok"])
        self.assertTrue(validate_value(
            "crm_contact_id", "deadbeef-0000-0000-0000-000000000001")["ok"])
        self.assertTrue(validate_value("wallet_ready", "$12.50")["ok"])
        self.assertTrue(validate_value("order_ok", "no: balance 0")["ok"])


class FinishSanity(unittest.TestCase):
    def test_prep_none_found(self):
        k = classify_finish("none found after search", data_prep=True, find_budget=True)
        self.assertEqual(k["outcome"], "not_found")
        self.assertTrue(k["not_found"])
        self.assertFalse(k["stuck"])

    def test_case_failure_not_success(self):
        k = classify_finish("could not complete the step", stuck=False)
        self.assertTrue(k["stuck"])
        self.assertEqual(k["outcome"], "failed")

    def test_blocked_prefix(self):
        k = classify_finish("BLOCKED: no Stripe on prod")
        self.assertEqual(k["outcome"], "blocked")
        self.assertTrue(k["stuck"])


class FindBudgetCreate(unittest.TestCase):
    def setUp(self):
        self.rule = {
            "id": "o", "value": "test_order", "app": "crm",
            "find": "find for {client}", "create": "make for {client}",
            "test_client": "H", "when": {"any": ["invoice"]},
            "verify": {"app": "portal", "text": "check {value} {record}"},
        }

    def _run(self, script, rule=None, env="local"):
        class Fake:
            def __init__(self, script):
                self.script, self.calls = script, []

            def __call__(self, app, brief, state):
                kind = brief["case_id"].rsplit("-", 1)[-1]
                if "-create-" in brief["case_id"]:
                    kind = "create"
                self.calls.append((kind, brief.get("read_only"), brief.get("max_tool_calls"),
                                   brief["steps"][0]["text"], app, brief.get("find_budget")))
                v = dict(self.script[kind].pop(0))
                notes = v.pop("_notes", "")
                limit_hit = bool(v.pop("_limit", False))
                not_found = bool(v.pop("_not_found", False))
                return N(values=v, notes=notes, stuck=False,
                         limit_hit=limit_hit, not_found=not_found)

        f = Fake(script)
        st = N(data={"action_log": [], "prep": {}, "prep_rejected": {}, "pack_cfg": {}},
               values={}, points=[])
        st.save = lambda: None
        st.checkpoint = lambda m, **k: st.points.append(m)
        plan = N(env=env, requirements=[], task_id=1)
        case = PlannedCase(id="B1", title="Open the invoice", area="portal",
                           steps=[Step(text="Open the invoice", app="portal")])
        with mock.patch("agent_tester.coordinator.brief_for",
                        side_effect=lambda p, c, s, stt: {
                            "case_id": c.id, "steps": [{"text": c.steps[0].text}]}):
            r = D._ensure_one(plan, case, st, {"crm": f, "portal": f}, rule or self.rule)
        return r, st, f

    def test_find_budget_capped(self):
        r, st, f = self._run({
            "find": [{"test_order": "A1", "test_order_record": "r1"}],
            "check": [{"test_order_ok": "yes"}],
        })
        self.assertIsNone(r)
        self.assertEqual(f.calls[0][2], D.DATA_FIND_MAX_STEPS)
        self.assertTrue(f.calls[0][5])

    def test_limit_hit_switches_to_create(self):
        r, st, f = self._run({
            "find": [{"_limit": True}],
            "create": [{"test_order": "A9", "test_order_record": "r9"}],
            "check": [{"test_order_ok": "yes"}],
        })
        self.assertIsNone(r)
        kinds = [c[0] for c in f.calls]
        self.assertEqual(kinds, ["find", "create", "check"])
        self.assertEqual(st.data["prep"]["o"]["value"], "A9")
        self.assertTrue(any("not_found" in p for p in st.points))

    def test_none_not_recorded(self):
        r, st, f = self._run({
            "find": [{"test_order": "none"}],
            "create": [{"test_order": "A9", "test_order_record": "r9"}],
            "check": [{"test_order_ok": "yes"}],
        })
        self.assertIsNone(r)
        self.assertEqual(st.data["prep"]["o"]["value"], "A9")
        self.assertNotEqual(st.values.get("test_order", {}).get("value"), "none")

    def test_finish_not_found_switches_to_create(self):
        r, st, f = self._run({
            "find": [{"_not_found": True, "_notes": "none found"}],
            "create": [{"test_order": "B2", "test_order_record": "r2"}],
            "check": [{"test_order_ok": "yes"}],
        })
        self.assertIsNone(r)
        self.assertEqual([c[0] for c in f.calls], ["find", "create", "check"])
        self.assertEqual(st.data["prep"]["o"]["value"], "B2")

    def test_create_parts_order_apps(self):
        rule = dict(self.rule, create=[
            {"app": "portal", "text": "make on portal for {client}"},
            {"app": "crm", "text": "finish in crm for {client}"},
        ])

        class SeqFake:
            def __init__(self, script):
                self.script, self.calls = script, []

            def __call__(self, app, brief, state):
                cid = brief["case_id"]
                kind = "create" if "-create" in cid else cid.rsplit("-", 1)[-1]
                self.calls.append((kind, app, brief.get("max_tool_calls")))
                v = self.script[kind].pop(0)
                return N(values=v, notes="", stuck=False, limit_hit=False, not_found=False)

        f = SeqFake({
            "find": [{"test_order": "none"}],
            "create": [{"test_order": "tmp"}, {"test_order": "A9", "test_order_record": "r9"}],
            "check": [{"test_order_ok": "yes"}],
        })
        st = N(data={"action_log": [], "prep": {}, "prep_rejected": {}, "pack_cfg": {}},
               values={}, points=[])
        st.save = lambda: None
        st.checkpoint = lambda m, **k: st.points.append(m)
        plan = N(env="local", requirements=[], task_id=1)
        case = PlannedCase(id="B1", title="Open the invoice", area="portal",
                           steps=[Step(text="Open the invoice", app="portal")])
        with mock.patch("agent_tester.coordinator.brief_for",
                        side_effect=lambda p, c, s, stt: {
                            "case_id": c.id, "steps": [{"text": c.steps[0].text}]}):
            r = D._ensure_one(plan, case, st, {"crm": f, "portal": f}, rule)
        self.assertIsNone(r)
        self.assertEqual([c[1] for c in f.calls if c[0] == "create"], ["portal", "crm"])

    def test_pay_create_allowed_on_preprod(self):
        rule = dict(self.rule, create=[
            {"app": "portal", "text": "pay with Stripe 4242 for {client}"},
        ])
        r, st, f = self._run({
            "find": [{"test_order": "none"}],
            "create": [{"test_order": "A9"}],
            "check": [{"test_order_ok": "yes"}],
        }, rule=rule, env="preprod")
        self.assertIsNone(r)
        self.assertTrue(any(c[0] == "create" for c in f.calls))

    def test_pay_create_blocked_on_production(self):
        rule = dict(self.rule, create=[
            {"app": "portal", "text": "pay with Stripe 4242 for {client}"},
        ])
        r, st, f = self._run({
            "find": [{"test_order": "none"}],
            "create": [{"test_order": "A9"}],
        }, rule=rule, env="prod")
        self.assertEqual(r.verdict, "BLOCKED")
        self.assertIn("payment", r.note.lower())

    def test_mpesa_create_blocked_without_phone(self):
        rule = dict(self.rule, create=[
            {"app": "portal", "text": "pay with M-Pesa for {client}"},
        ])
        r, st, f = self._run({
            "find": [{"test_order": "none"}],
            "create": [{"test_order": "A9"}],
        }, rule=rule, env="staging")
        self.assertEqual(r.verdict, "BLOCKED")
        self.assertRegex(r.note.lower(), r"m-?pesa|phone")

    def test_mpesa_create_allowed_with_phone(self):
        rule = dict(self.rule, create=[
            {"app": "portal", "text": "pay with M-Pesa phone +254712345678 for {client}"},
        ])
        r, st, f = self._run({
            "find": [{"test_order": "none"}],
            "create": [{"test_order": "A9"}],
            "check": [{"test_order_ok": "yes"}],
        }, rule=rule, env="staging")
        self.assertIsNone(r)


if __name__ == "__main__":
    unittest.main()
