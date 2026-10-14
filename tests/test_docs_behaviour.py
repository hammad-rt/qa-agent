import tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from agent_tester import docs, learned


class R:
    def __init__(self, text): self.text = text


class DocsTest(unittest.TestCase):
    def test_lookup_fetches_then_caches(self):
        d = Path(tempfile.mkdtemp())
        pack = SimpleNamespace(root=d, config={"stack": {"crm": {"name": "SuiteCRM 7", "docs": ["https://docs.suitecrm.com"]}}})
        calls = []
        def get(url, params=None):
            calls.append(url)
            if "duckduckgo" in url:
                return R('<a class="result__a" href="https://docs.suitecrm.com/admin/settings/">x</a>')
            return R("<p>Admin settings are saved with the Save button and validation errors show next to the field.</p>")
        out = docs.lookup(pack, "crm", "admin settings save validation", get=get)
        self.assertTrue(out["results"])
        self.assertTrue((d / "docs" / "crm.md").is_file())
        n = len(calls)
        again = docs.lookup(pack, "crm", "admin settings save validation", get=get)
        self.assertEqual(again["results"][0]["source"], "cache")
        self.assertEqual(len(calls), n)

    def test_no_stack(self):
        pack = SimpleNamespace(root=Path(tempfile.mkdtemp()), config={})
        self.assertIn("error", docs.lookup(pack, "crm", "anything here"))

    def test_behaviour_line(self):
        d = tempfile.mkdtemp()
        self.assertTrue(learned.record_behaviour(d, "a", "crm", "Save with 12.5 shows no message", "TC5"))
        self.assertIsNone(learned.record_behaviour(d, "a", "crm", "Save with 12.5 shows no message", "TC6"))
        self.assertTrue(any(l.startswith("Behaviour:") for l in learned.read(d, "a", "crm")))
