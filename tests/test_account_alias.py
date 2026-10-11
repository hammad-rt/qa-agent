import unittest
from types import SimpleNamespace

from agent_tester.helpers import HelperContext


class AccountAlias(unittest.TestCase):
    def _ctx(self):
        pack = SimpleNamespace(accounts=lambda app: {"customer": {"username": "pack.user@x.com", "password_env": "P"}})
        state = SimpleNamespace(data={"plan": {"requirements": [
            {"key": "account.portal.customer", "value": "chosen@x.com", "secret_env": "P2"}]}})
        c = HelperContext.__new__(HelperContext)
        c.pack, c.state, c.app = pack, state, "portal"
        return c

    def test_key_still_works(self):
        self.assertEqual(self._ctx().account("customer")["username"], "chosen@x.com")

    def test_chosen_username_maps_to_key(self):
        acc = self._ctx().account("chosen@x.com")
        self.assertEqual((acc["username"], acc["password_env"]), ("chosen@x.com", "P2"))

    def test_pack_username_maps_to_key(self):
        self.assertEqual(self._ctx().account("pack.user@x.com")["password_env"], "P2")


if __name__ == "__main__":
    unittest.main()
