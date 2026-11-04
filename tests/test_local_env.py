import unittest
from pathlib import Path

from agent_tester.preflight.gap_resolver import normalize_env
from agent_tester.tools.policy import Policy


class LocalEnv(unittest.TestCase):
    def test_names(self):
        for t in ("local", "localhost", "4", "dev"):
            self.assertEqual(normalize_env(t), "local")
        self.assertEqual(normalize_env("3"), "prod")

    def test_test_card_preapprovable_on_local_not_preprod(self):
        kw = dict(allow_hosts=[], risky_patterns=[], preapprovable=["pay_staging_test_card"], always_ask=[])
        self.assertTrue(Policy(env="local", **kw).can_preapprove("pay_staging_test_card"))
        self.assertFalse(Policy(env="preprod", **kw).can_preapprove("pay_staging_test_card"))

    def test_pack_has_local_urls_on_allowlisted_hosts(self):
        import yaml
        cfg = yaml.safe_load(Path("packs/savostore/pack.yaml").read_text())
        self.assertIn("localhost", cfg["allowlist"]["local"])
        for app in ("portal", "crm"):
            self.assertTrue(cfg["apps"][app]["envs"]["local"].startswith("http://localhost"))


if __name__ == "__main__":
    unittest.main()
