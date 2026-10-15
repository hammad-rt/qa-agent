import unittest
from pathlib import Path
from agent_tester.web.server import run_sort_key


class T(unittest.TestCase):
    def test_newest_first_by_name(self):
        names = ["182941-20261014-145408", "182941-20261015-144127", "166023-20261012-181056", "180966-20261012-183747"]
        got = sorted((Path(n) for n in names), key=run_sort_key, reverse=True)
        self.assertEqual([p.name for p in got], ["182941-20261015-144127", "182941-20261014-145408",
                                                 "180966-20261012-183747", "166023-20261012-181056"])


if __name__ == "__main__":
    unittest.main()
