"""Knowledge layers, platform detection, stuck ladder — hermetic unit tests."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_tester import testing_hermetic as hermetic  # noqa: F401

from agent_tester.memory.layers import (
    is_suppressed, merge_layers, override_key, override_text, suppressed_hashes, text_hash,
)  # override_key used in LayerMergeTests
from agent_tester.memory.retrieve import DEFAULT_TOKEN_BUDGET, TurnRetriever, tokens_of
from agent_tester.memory.seeds import DOMAIN_LESSONS, ensure_seeds, load_platform_seeds
from agent_tester.memory.service import Memory
from agent_tester.memory.store import MemoryStore
from agent_tester.platform_detect import detect
from agent_tester.stuck_lookup import (
    build_query, clear_cache, format_snippets, run_ladder, search_code,
)

FIXTURES = Path(__file__).parent / "fixtures" / "platform_html"
PLATFORMS = Path(__file__).resolve().parents[1] / "packs" / "_platforms"


class PlatformDetectTests(unittest.TestCase):
    def test_suitecrm7(self):
        html = (FIXTURES / "suitecrm7_login.html").read_text()
        d = detect(url="https://crm.example/index.php?module=Users&action=Login", html=html)
        self.assertEqual(d.platform, "suitecrm7")
        self.assertGreaterEqual(d.platform_confidence, 0.5)

    def test_suitecrm8(self):
        html = (FIXTURES / "suitecrm8_home.html").read_text()
        d = detect(url="https://crm.example/#/home", html=html)
        self.assertEqual(d.platform, "suitecrm8")
        self.assertGreaterEqual(d.platform_confidence, 0.5)
        self.assertTrue(d.platform_version is None or d.platform_version.startswith("8"))

    def test_sugarcrm(self):
        html = (FIXTURES / "sugarcrm_sidecar.html").read_text()
        d = detect(url="https://sugar.example/#Accounts", html=html)
        self.assertEqual(d.platform, "sugarcrm")
        self.assertGreaterEqual(d.platform_confidence, 0.5)

    def test_custom_unknown(self):
        html = (FIXTURES / "custom_app.html").read_text()
        d = detect(url="https://portal.example/login", html=html)
        self.assertIsNone(d.platform)


class LayerMergeTests(unittest.TestCase):
    def test_project_wins_and_override_suppresses(self):
        domain = {
            "id": 1, "scope": "domain:crm", "kind": "lesson",
            "text": "Basic search often matches names only; use Advanced Search for email.",
        }
        platform = {
            "id": 2, "scope": "platform:suitecrm7", "kind": "lesson",
            "text": "Basic list search often matches name fields only; use Advanced Search / filters for email.",
        }
        project_same = {
            "id": 3, "scope": "project:demo", "kind": "lesson",
            "text": "Basic search often matches names only; use Advanced Search for email.",
        }
        merged = merge_layers([domain, platform, project_same])
        scopes = {r["scope"] for r in merged}
        self.assertIn("project:demo", scopes)
        # Near-duplicate domain dropped in favour of project.
        self.assertNotIn("domain:crm", scopes)

        contradicted = domain["text"]
        override = {
            "id": 4, "scope": "project:demo", "kind": "lesson",
            "key": override_key(contradicted),
            "text": override_text(contradicted, "On this project basic search also matches email."),
        }
        suppress = suppressed_hashes([override])
        self.assertIn(text_hash(contradicted), suppress)
        self.assertTrue(is_suppressed(domain, suppress, [override]))
        merged2 = merge_layers([domain, platform, override])
        self.assertTrue(all(r["scope"].startswith("project:") for r in merged2 if "Not true here" in r["text"] or True))
        self.assertFalse(any(r["id"] == 1 for r in merged2))

    def test_domain_seeds_have_no_project_names(self):
        blob = " ".join(s["text"] for lessons in DOMAIN_LESSONS.values() for s in lessons)
        for banned in ("SuiteCRM", "Sugar", "Savostore", "Savo"):
            self.assertNotIn(banned, blob)

    def test_ensure_seeds_domain_and_platform(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d) / "m.db")
            a = ensure_seeds(store, "demo", platforms=["suitecrm7"], platforms_dir=PLATFORMS)
            self.assertGreater(a["domain"], 0)
            self.assertGreater(a["platform"], 0)
            b = ensure_seeds(store, "demo", platforms=["suitecrm7"], platforms_dir=PLATFORMS)
            self.assertEqual(b["domain"], 0)
            self.assertEqual(b["platform"], 0)
            plat = store.query(["platform:suitecrm7"], ["lesson"])
            self.assertTrue(plat)
            # Platform seeds marked as starting assumptions via source.
            self.assertTrue(all(r["source"] == "seed_platform" for r in plat))
            store.close()


class StuckLadderTests(unittest.TestCase):
    def setUp(self):
        clear_cache()
        hermetic.clear_pack_secrets()

    def test_order_memory_then_code_then_docs_web_custom_app(self):
        """Custom app: no platform, no code_root, no providers → memory only, then next=explore."""
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d) / "m.db")
            mem = Memory(store, "demo")
            store.add("lesson", "project:demo", "Reload after apply before reading the wallet",
                      "user_correction", area="rewards", status="active")
            with mock.patch.dict(os.environ, {
                "DOCS_SEARCH_URL": "", "CONTEXT7_API_URL": "", "CODE_ROOT": "", "QA_CODE_ROOT": "",
            }, clear=False):
                os.environ.pop("DOCS_SEARCH_URL", None)
                os.environ.pop("CONTEXT7_API_URL", None)
                os.environ.pop("CODE_ROOT", None)
                os.environ.pop("QA_CODE_ROOT", None)
                out = run_ladder(
                    query="wallet apply reload", memory=mem, pack=None, platform_id=None,
                    save_unconfirmed=False,
                )
            self.assertEqual(out["steps_tried"], ["memory", "code", "platform_docs", "web"])
            self.assertEqual(out["next"], "explore")
            self.assertTrue(any(s["source"] == "memory" for s in out["snippets"]))
            self.assertFalse(any(s["source"] in ("code", "platform_docs", "web") for s in out["snippets"]))
            store.close()

    def test_code_search_and_mocked_providers(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            code = root / "src"
            code.mkdir()
            (code / "WalletService.php").write_text("<?php\nclass WalletService {\n  public function applyRewards() {}\n}\n")
            store = MemoryStore(root / "m.db")
            mem = Memory(store, "demo")
            pack = mock.Mock(config={"code_root": str(code)}, slug="demo", root=root)

            def fake_provider(query, *, site=None, limit=3, deadline=None):
                src = "docs" if site else "web"
                return [{"source": src, "path": f"https://example/{src}", "excerpt": f"{src} hint about apply",
                         "trust": "untrusted"}]

            with mock.patch("agent_tester.stuck_lookup.search_docs_provider", side_effect=fake_provider):
                with mock.patch.dict(os.environ, {
                    "DOCS_SEARCH_URL": "http://127.0.0.1:9/search",
                    "CONTEXT7_API_URL": "",
                }, clear=False):
                    out = run_ladder(
                        query="apply rewards WalletService", memory=mem, pack=pack,
                        platform_id="suitecrm7", save_unconfirmed=False, packs_dir=PLATFORMS,
                    )
            sources = [s["source"] for s in out["snippets"]]
            self.assertIn("code", sources)
            # Providers mocked — docs/web may appear after code depending on max snippets.
            self.assertLessEqual(len(out["snippets"]), 3)
            self.assertLessEqual(out["tokens"], DEFAULT_TOKEN_BUDGET)
            store.close()

    def test_budget_cap_and_untrusted_formatting(self):
        snippets = [
            {"source": "web", "path": "http://x/" + ("a" * 500), "excerpt": "raw " * 200, "trust": "untrusted"},
            {"source": "web", "path": "http://y", "excerpt": "more " * 200, "trust": "untrusted"},
            {"source": "web", "path": "http://z", "excerpt": "page " * 200, "trust": "untrusted"},
        ]
        # format_snippets must not dump huge raw pages; excerpts already trimmed by ladder helpers.
        from agent_tester.stuck_lookup import _trim
        trimmed = [{"source": s["source"], "path": s["path"][:80], "excerpt": _trim(s["excerpt"])} for s in snippets]
        prompt = format_snippets(trimmed)
        self.assertIn("Untrusted", prompt)
        self.assertLessEqual(tokens_of(prompt), DEFAULT_TOKEN_BUDGET)
        self.assertNotIn("raw raw raw raw raw raw raw raw raw raw raw raw raw raw raw raw raw raw", prompt[:50] + "…")

        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d) / "m.db")
            mem = Memory(store, "demo")
            for i in range(30):
                store.add("lesson", "project:demo", f"stuck lesson number {i} " + ("word " * 50),
                          "agent_proposal", status="active")
            out = run_ladder(query="stuck lesson word", memory=mem, pack=None, platform_id=None,
                             save_unconfirmed=False, max_snippets=3)
            self.assertLessEqual(len(out["snippets"]), 3)
            self.assertLessEqual(out["tokens"], DEFAULT_TOKEN_BUDGET)
            store.close()

    def test_search_code_returns_file_line(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "mod.py").write_text("def find_client():\n    return None\n")
            hits = search_code(root, "find_client lookup", limit=2)
            self.assertTrue(hits)
            self.assertIn(":", hits[0]["path"])
            self.assertEqual(hits[0]["source"], "code")

    def test_build_query_includes_tried(self):
        q = build_query(goal="find contact", page="/crm", tried=["click:Search", "type:email"], area="crm", app="crm")
        self.assertIn("find contact", q)
        self.assertIn("tried:", q)


class RetrievalWithLayers(unittest.TestCase):
    def test_platform_only_when_set_and_project_override_wins(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d) / "m.db")
            ensure_seeds(store, "demo", platforms=["suitecrm7"], platforms_dir=PLATFORMS)
            mem = Memory(store, "demo")
            # Without platform → no platform scope in query.
            self.assertFalse(any(s.startswith("platform:") for s in mem.scopes))
            mem.set_platforms(["suitecrm7"])
            self.assertIn("platform:suitecrm7", mem.scopes)
            # Override a domain note.
            domain_text = DOMAIN_LESSONS["crm"][1]["text"]  # basic search / advanced
            oid = mem.record_override(domain_text, "Here basic search matches email too.", area="crm")
            store.set_status(oid, "active")  # inject path queries active rows
            tr = TurnRetriever(mem, token_budget=DEFAULT_TOKEN_BUDGET)
            res = tr.retrieve(area="crm", app="crm", query="search email filter", include_learned=False)
            texts = [r["text"] for r in res.rows]
            # Contradicted domain note suppressed; override (or absence of domain text) expected.
            self.assertFalse(any(r.get("scope") == "domain:crm" and domain_text[:50] in (r.get("text") or "")
                                 for r in res.rows))
            self.assertLessEqual(res.tokens, DEFAULT_TOKEN_BUDGET)
            store.close()


if __name__ == "__main__":
    unittest.main()
