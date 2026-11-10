"""Deterministic SuiteCRM list/detail lookups (no LLM)."""
from __future__ import annotations

import unittest

from agent_tester.lookups.record import (
    detail_field, list_url_for_platform, match_row, record_id_from_row,
    rows_from_snapshot, suitecrm7_detail_url, suitecrm7_list_url,
)


class Urls(unittest.TestCase):
    def test_list_advanced_email(self):
        u = suitecrm7_list_url("https://crm.example/", "Contacts",
                               {"email1": "a@b.com"}, advanced=True)
        self.assertIn("module=Contacts", u)
        self.assertIn("query=true", u)
        self.assertIn("advanced_search", u)
        self.assertIn("email1_advanced=a%40b.com", u)

    def test_detail(self):
        u = suitecrm7_detail_url("https://crm.example", "AOS_Invoices", "abc-123")
        self.assertIn("action=DetailView", u)
        self.assertIn("record=abc-123", u)

    def test_platform_dispatch(self):
        u = list_url_for_platform("suitecrm7", "https://x/", "Contacts", {"name": "x"})
        self.assertIn("module=Contacts", u)


class Parse(unittest.TestCase):
    def test_table_and_match(self):
        snap = """
- table "Contacts":
  - row:
    - columnheader "Name"
    - columnheader "Email"
  - row:
    - cell "Ada"
    - cell "ada@example.com"
  - row:
    - cell "Bea"
    - cell "bea@example.com"
"""
        rows = rows_from_snapshot(snap)
        self.assertEqual(len(rows), 2)
        hit = match_row(rows, "email", "bea@example.com")
        self.assertEqual(hit["Email"], "bea@example.com")

    def test_record_id_from_href(self):
        rid = record_id_from_row({"Name": "x"}, "open index.php?module=Contacts&action=DetailView&record=deadbeef")
        self.assertEqual(rid, "deadbeef")

    def test_detail_field_by_label(self):
        snap = """
- main:
  - cell "Email Address" [ref=e1]
  - cell "sidney@example.com" [ref=e2]
  - cell "Current Rewards" [ref=e3]
  - cell "$120.00" [ref=e4]
"""
        self.assertEqual(detail_field(snap, "Email Address"), "sidney@example.com")
        self.assertEqual(detail_field(snap, "Current Rewards"), "$120.00")


class FakeBrowserLookup(unittest.TestCase):
    def test_find_contact_async(self):
        import asyncio
        from agent_tester.lookups import find_contact_by_email

        class B:
            def __init__(self):
                self.last_snapshot = ""
                self.urls = []
            async def navigate(self, url):
                self.urls.append(url)
                return "ok"
            async def snapshot(self):
                self.last_snapshot = (
                    '- table:\n  - row:\n    - columnheader "Name"\n    - columnheader "Email"\n'
                    '  - row:\n    - cell "Sid"\n    - cell "sid@x.com"\n'
                    'Page has record=abcdef12deadbeef\n'
                    '- cell "Email" [ref=e1]\n- cell "sid@x.com" [ref=e2]\n'
                )
                return self.last_snapshot
            async def screenshot(self, name):
                return name

        res = asyncio.run(find_contact_by_email(B(), base_url="https://crm/", email="sid@x.com"))
        self.assertTrue(res.ok, res.notes)
        self.assertEqual(res.record_id, "abcdef12deadbeef")


if __name__ == "__main__":
    unittest.main()
