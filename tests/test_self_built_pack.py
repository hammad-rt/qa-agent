import os, tempfile, unittest
from pathlib import Path
from unittest import mock

from agent_tester import learned, data_prep


class SelfBuiltPack(unittest.TestCase):
    def test_recipe_keeps_data_changing_steps(self):
        with tempfile.TemporaryDirectory() as d:
            acts = [
                {"app": "crm", "tool": "browser_click", "args": {"element": "link orders"}},
                {"app": "crm", "tool": "browser_click", "args": {"element": "button create"}},
                {"app": "crm", "tool": "browser_select_option", "approval": "approved",
                 "args": {"element": "combobox status", "values": ["Received"]}},
                {"app": "crm", "tool": "browser_click", "approval": "approved", "args": {"element": "button save"}},
            ]
            line = learned.record_recipe(Path(d), "orders", "crm", "create test data order", acts)
            self.assertIsNotNone(line)
            self.assertIn("Recipe (create test data order)", line)
            self.assertIn("Received", line)
            self.assertIn("save", line)
            self.assertTrue(any("Recipe" in l for l in learned.read(Path(d), "orders", "crm")))
            # plain routes still stop before the first change
            self.assertNotIn("save", " ".join(learned.route_from_actions(acts, "crm")))

    def test_find_prompt_searches_instead_of_two_pages(self):
        src = Path(data_prep.__file__).read_text()
        self.assertNotIn("first two list pages", src)
        self.assertIn("search, filters", src)

    def test_no_pack_builds_one_when_interactive(self):
        from agent_tester import main
        from agent_tester.preflight.pack_loader import NoPackFound
        calls = []
        def fake_find(_d, ids):
            calls.append(ids)
            if len(calls) == 1:
                raise NoPackFound("none")
            return "PACK"
        s = mock.Mock(packs_dir=Path("/nonexistent"))
        with mock.patch("agent_tester.preflight.pack_loader.find_pack", fake_find), \
             mock.patch.object(main, "cmd_onboard", return_value=0) as ob, \
             mock.patch.dict(os.environ, {"QA_WEB": "1"}):
            self.assertEqual(main._pack_or_onboard(s, 42), "PACK")
            ob.assert_called_once()

    def test_no_pack_noninteractive_raises(self):
        from agent_tester import main
        from agent_tester.preflight.pack_loader import NoPackFound
        s = mock.Mock(packs_dir=Path("/nonexistent"))
        env = {k: v for k, v in os.environ.items() if k != "QA_WEB"}
        with mock.patch("agent_tester.preflight.pack_loader.find_pack", side_effect=NoPackFound("x")), \
             mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdin") as si:
            si.isatty.return_value = False
            with self.assertRaises(NoPackFound):
                main._pack_or_onboard(s, 42)


if __name__ == "__main__":
    unittest.main()
