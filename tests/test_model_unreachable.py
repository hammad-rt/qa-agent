"""A dropped VPN gives a one-line message, not a traceback."""
import socket
import unittest
from types import SimpleNamespace
from unittest import mock

from agent_tester import main as M


class Unreachable(unittest.TestCase):
    def test_chain_detected(self):
        try:
            try:
                raise socket.gaierror(-2, "Name or service not known")
            except OSError as e:
                raise RuntimeError("litellm.APIConnectionError: wrapped") from e
        except RuntimeError as e:
            self.assertTrue(M.model_unreachable(e))
        self.assertFalse(M.model_unreachable(ValueError("bad json")))

    def test_preflight(self):
        st = SimpleNamespace(llm_base_url="https://ai.rolustech.com/v1")
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror(-2, "Name or service not known")):
            msg = M.model_host_ok(st)
        self.assertIn("ai.rolustech.com", msg)
        self.assertIn("VPN", msg)
        with mock.patch("socket.getaddrinfo", return_value=[()]):
            self.assertIsNone(M.model_host_ok(st))

    def test_main_prints_message(self):
        err = RuntimeError("x")
        err.__cause__ = socket.gaierror(-2, "Name or service not known")
        with mock.patch.object(M, "_main", side_effect=err), mock.patch("builtins.print") as p:
            self.assertEqual(M.main(["check"]), 4)
        self.assertIn("Can't reach the model server", p.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
