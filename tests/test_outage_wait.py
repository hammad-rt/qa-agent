import unittest
from agent_tester.reliability import wait_for_model


class T(unittest.TestCase):
    def test_returns_when_back(self):
        seq = iter([False, False, True])
        slept = []
        self.assertTrue(wait_for_model(60, 10, sleep=slept.append, probe=lambda: next(seq)))
        self.assertEqual(slept, [10, 10])

    def test_gives_up(self):
        def down():
            raise OSError("refused")
        self.assertFalse(wait_for_model(20, 10, sleep=lambda s: None, probe=down))


if __name__ == "__main__":
    unittest.main()
