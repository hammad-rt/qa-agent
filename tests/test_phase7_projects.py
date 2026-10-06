"""Phase 7: second project. Template, validation, onboarding from Redmine (fake), and isolation between packs.
The second pack here is a fictional fixture (example.com); no real project was named yet."""
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from agent_tester.config import ROOT
from agent_tester.helpers import load_helpers
from agent_tester.memory.service import Memory
from agent_tester.memory.store import MemoryStore
from agent_tester.preflight import onboard as O
from agent_tester.preflight.pack_loader import NoPackFound, find_pack, load_pack
from agent_tester.preflight.packs import new_pack, validate
from agent_tester.tools.policy import PolicyViolation, from_pack

FIXTURE = ROOT / "tests" / "fixtures" / "packs" / "example-shop"


def packs_copy(d):
    p = Path(d) / "packs"
    p.mkdir()
    for src in (ROOT / "packs" / "savostore", ROOT / "packs" / "_template", FIXTURE):
        shutil.copytree(src, p / src.name)
    return p


class TemplateAndValidate(unittest.TestCase):
    def test_new_pack_from_template(self):
        with tempfile.TemporaryDirectory() as d:
            packs = packs_copy(d)
            dest = new_pack(packs, "acme", "Acme", [990002])
            pack = load_pack(dest)
            self.assertEqual((pack.slug, pack.config["redmine"]["project_ids"]), ("acme", [990002]))
            self.assertEqual(validate(pack, packs), [])
            self.assertEqual(load_helpers(pack).keys(), {"login_form", "read_table"})   # template example is not loaded
            with self.assertRaises(FileExistsError):
                new_pack(packs, "acme")
            with self.assertRaises(ValueError):
                new_pack(packs, "Bad Slug")

    def test_real_and_fixture_packs_are_valid(self):
        with tempfile.TemporaryDirectory() as d:
            packs = packs_copy(d)
            for s in ("savostore", "example-shop"):
                self.assertEqual(validate(load_pack(packs / s), packs), [], s)

    def test_validation_catches_unsafe_packs(self):
        with tempfile.TemporaryDirectory() as d:
            packs = packs_copy(d)
            root = packs / "example-shop"
            cfg = yaml.safe_load((root / "pack.yaml").read_text())
            cfg["redmine"]["project_ids"] = [1567]                                  # savostore's id
            cfg["apps"]["shop"]["envs"]["staging"] = "https://other.example.com/"   # not allowlisted
            cfg["apps"]["shop"]["accounts"]["buyer"]["password"] = "hunter2"
            cfg["preapprovable_actions"].append("send_email")
            cfg["always_ask_actions"] = ["redmine_post"]
            cfg["risky_patterns"].append("(unclosed")
            (root / "pack.yaml").write_text(yaml.safe_dump(cfg))
            probs = "\n".join(validate(load_pack(root), packs))
            for want in ("also claimed by pack 'savostore'", "other.example.com is not in allowlist", "literal secret",
                         "has a password field", "must include", "does not compile"):
                self.assertIn(want, probs)


class Isolation(unittest.TestCase):
    def test_pack_chosen_by_redmine_project(self):
        with tempfile.TemporaryDirectory() as d:
            packs = packs_copy(d)
            self.assertEqual(find_pack(packs, [1567]).slug, "savostore")
            self.assertEqual(find_pack(packs, [990001]).slug, "example-shop")
            with self.assertRaises(NoPackFound):
                find_pack(packs, [42])

    def test_allowlist_known_bugs_and_helpers_are_per_pack(self):
        shop, savo = load_pack(FIXTURE), load_pack(ROOT / "packs" / "savostore")
        with self.assertRaises(PolicyViolation):
            from_pack(shop, "staging").check_url("https://staging5.rolustech.com:4225/")
        with self.assertRaises(PolicyViolation):
            from_pack(savo, "staging").check_url("https://staging.example.com/")
        self.assertEqual(len(shop.known_bugs), 1)
        self.assertNotIn("Points", " ".join(b["text"] for b in savo.known_bugs))
        self.assertIn("read_points", load_helpers(shop))
        self.assertNotIn("read_points", load_helpers(savo))
        self.assertNotIn("read_wallet", load_helpers(shop))

    def test_memory_never_crosses_projects(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d) / "m.db", None)
            store.add("lesson", "project:savostore", "Rewards apply button is under Order Summary", "t", area="rewards")
            store.add("lesson", "project:example-shop", "Points refresh needs a reload", "t", area="points")
            store.add("lesson", "core", "Wait for the spinner to go before reading values", "t")
            shop = [r["text"] for r in Memory(store, "example-shop").preload()]
            savo = [r["text"] for r in Memory(store, "savostore").preload()]
            store.close()
        self.assertIn("Points refresh needs a reload", shop)
        self.assertNotIn("Rewards apply button is under Order Summary", shop)
        self.assertNotIn("Points refresh needs a reload", savo)
        self.assertTrue(any("spinner" in t for t in shop) and any("spinner" in t for t in savo))


class FakeRedmine:
    """EXAMPLE DATA: a fictional project with addresses, a leaked credential and open bugs."""
    def issue(self, i):
        return {"id": i, "project": {"id": 990003}}

    def project(self, p):
        return {"id": 990003, "name": "Acme Store", "identifier": "acme-store",
                "description": "Staging: https://qa:pa55@staging.acme.example.com:8443/login?token=abc"}

    def issues(self, p, limit=50, subject_contains=None):
        return [
            {"id": 1, "subject": "Test Cases - Checkout", "description": "Run on https://staging.acme.example.com:8443/cart",
             "tracker": {"name": "Task"}, "status": {"name": "New"}},
            {"id": 2, "subject": "Coupon not applied", "description": "Live: https://www.acme.example.com/ password: hunter2",
             "tracker": {"name": "Bug"}, "status": {"name": "New"}, "priority": {"name": "Urgent"}},
            {"id": 3, "subject": "Old bug", "description": "", "tracker": {"name": "Bug"},
             "status": {"name": "Closed", "is_closed": True}, "priority": {"name": "Low"}},
            {"id": 4, "subject": "See MR", "description": "https://git.rolustech.com/x/y/-/merge_requests/1",
             "tracker": {"name": "Task"}, "status": {"name": "New"}}]

    def wiki_index(self, p):
        return ["Staging_Access", "Release_Notes"]

    def wiki_page(self, p, t):
        return "Admin: https://staging.acme.example.com:8443/admin user admin password: s3cret"


class Onboard(unittest.TestCase):
    def test_draft_takes_addresses_never_credentials(self):
        d = O.draft_pack(FakeRedmine(), "#123", "redmine.rolustech.com")
        bases = {c["base"]: c["env"] for c in d.candidates}
        self.assertEqual(bases, {"https://staging.acme.example.com:8443/": "staging", "https://www.acme.example.com/": "prod"})
        dumped = yaml.safe_dump(d.config) + d.facts
        for secret in ("pa55", "hunter2", "s3cret", "token=abc"):
            self.assertNotIn(secret, dumped)
        self.assertIn("1. [Critical] Coupon not applied (Redmine #2)", d.facts)
        self.assertNotIn("Old bug", d.facts)
        self.assertEqual(d.config["redmine"]["wiki_pages"], ["Staging_Access"])
        self.assertEqual(d.config["redmine"]["test_case_subject_prefix"], "Test Cases - ")

    def test_cli_asks_one_batch_and_saves_only_after_yes(self):
        answers = iter(["store", "store", "", "buyer@acme.example.com,My account", "n"])
        with tempfile.TemporaryDirectory() as tmp:
            packs = packs_copy(tmp)
            settings = SimpleNamespace(packs_dir=packs, redmine_url="https://redmine.rolustech.com", redmine_api_key="x")
            from agent_tester import main as M
            with mock.patch("agent_tester.preflight.redmine.Redmine", lambda *a, **k: FakeRedmine()):
                self.assertEqual(M.cmd_onboard(settings, "123", None, input_fn=lambda q: next(answers), out=lambda *_: None), 1)
                self.assertFalse((packs / "acme-store").exists())                    # declined: nothing saved
                answers2 = iter(["store", "store", "", "buyer@acme.example.com,My account", "y"])
                self.assertEqual(M.cmd_onboard(settings, "123", None, input_fn=lambda q: next(answers2), out=lambda *_: None), 0)
            pack = load_pack(packs / "acme-store")
            app = pack.apps["store"]
            self.assertEqual(app["envs"], {"staging": "https://staging.acme.example.com:8443/", "prod": "https://www.acme.example.com/"})
            self.assertEqual(app["accounts"]["tester"]["role_markers"], ["My account"])
            self.assertEqual(pack.allowlist("prod"), ["www.acme.example.com"])
            self.assertEqual(app["accounts"]["tester"]["password_env"], "ACME_STORE_STORE_PASSWORD")
            self.assertEqual(validate(pack, packs), [])
            self.assertEqual(find_pack(packs, [990003]).slug, "acme-store")


if __name__ == "__main__":
    unittest.main()
