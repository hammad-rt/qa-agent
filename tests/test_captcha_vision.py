import asyncio, json, os, unittest
from unittest import mock

from agent_tester import captcha, vision
from agent_tester.config import Settings, job_model


def res(d):
    return "### Result\n" + json.dumps(json.dumps(d))


class FakeBrowser:
    def __init__(self, passed_after=1):
        self.calls, self.verifies, self.passed_after = [], 0, passed_after

    async def _do(self, tool, args):
        code = args["code"]
        self.calls.append(code)
        if "recaptcha-verify-button" in code:
            self.verifies += 1
            return res({"passed": self.verifies >= self.passed_after})
        if "screenshot" in code:
            return res({"open": True, "desc": "Select all images with buses", "tiles": 9, "image": "AAA"})
        return res({"clicked": 1})


class T(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"CAPTCHA_TILE_MODEL_OFF": "1"})
        p.start(); self.addCleanup(p.stop)

    def test_target_words(self):
        from agent_tester.tile_model import target
        self.assertEqual(target("Select all images with fire hydrants"), "Hydrant")
        self.assertEqual(target("Select all squares with traffic lights"), "Traffic Light")
        self.assertEqual(target("Select all images with buses"), "Bus")
        self.assertIsNone(target("Select all images with boats"))

    def test_local_model_answer_skips_llm(self):
        async def ask(img, q):
            raise AssertionError("LLM should not be asked")
        with mock.patch("agent_tester.tile_model.classify", return_value=[3]):
            b = FakeBrowser()
            self.assertTrue(asyncio.run(captcha.solve(b, "staging", ask=ask)))
            self.assertTrue(any("[3]" in c for c in b.calls))

    def test_tile_numbers(self):
        self.assertEqual(vision.tile_numbers("Tiles: [2, 5, 5, 12]", 9), [2, 5])
        self.assertEqual(vision.tile_numbers("1 and 3", 9), [1, 3])
        self.assertEqual(vision.tile_numbers("[]", 9), [])

    def test_job_routing(self):
        s = Settings.load()
        with mock.patch.dict(os.environ, {"LLM_VISION_MODEL": "qwen2.5vl:3b", "LLM_VISION_BASE_URL": "http://localhost:11434/v1"}):
            self.assertEqual(job_model(s, "vision")[:2], ("http://localhost:11434/v1", "qwen2.5vl:3b"))
            self.assertEqual(job_model(s, "judge")[1], s.llm_model)

    def test_solves_and_clicks_picked_tiles(self):
        async def ask(img, q):
            self.assertIn("buses", q)
            return "[2, 4]"
        b = FakeBrowser()
        self.assertTrue(asyncio.run(captcha.solve(b, "staging", ask=ask)))
        self.assertTrue(any("[2, 4]" in c for c in b.calls))

    def test_gives_up_after_rounds(self):
        async def ask(img, q):
            return "[1]"
        b = FakeBrowser(passed_after=99)
        self.assertFalse(asyncio.run(captcha.solve(b, "staging", ask=ask, rounds=2)))
        self.assertEqual(b.verifies, 2)

    def test_never_on_production(self):
        b = FakeBrowser()
        self.assertFalse(asyncio.run(captcha.solve(b, "prod", ask=None)))
        self.assertEqual(b.calls, [])

    def test_model_error_falls_back(self):
        async def ask(img, q):
            raise OSError("down")
        self.assertFalse(asyncio.run(captcha.solve(FakeBrowser(), "staging", ask=ask)))


if __name__ == "__main__":
    unittest.main()
