"""Placeholder API keys and short/common values must not be used as secret-mask patterns."""
from __future__ import annotations

import os
import tempfile
import unittest

from agent_tester.redact import (
    SCRUB_CALL_SITES, effective_api_key, for_user, is_placeholder_secret,
    scrub_secrets, strip_redaction_tokens, warn_skipped_secrets,
)
from agent_tester.run_state import RunState


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

    def test_empty_null_not_used_as_mask(self):
        for placeholder in ("none", "empty", "null", ""):
            env = {"LLM_API_KEY": placeholder or "none"}
            t = scrub_secrets("none of the empty null fields matched", env)
            self.assertEqual(t, "none of the empty null fields matched")

    def test_scrub_call_sites_documented(self):
        self.assertIn("run_state.save", SCRUB_CALL_SITES)
        self.assertIn("progress._write", SCRUB_CALL_SITES)


if __name__ == "__main__":
    unittest.main()
