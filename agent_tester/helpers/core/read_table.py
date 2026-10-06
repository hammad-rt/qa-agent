"""Read a table (or grid) on the current page into rows of cell text, from the accessibility snapshot."""
import re

HELPER = {"name": "read_table", "app": "any", "changes_data": False, "action_type": None,
          "description": "Read the rows of a table on the current page; optionally only rows containing some text.",
          "params": {"table": "optional table name", "contains": "optional text a row must contain",
                     "path": "optional page path to open first"},
          "needs_config": []}

_LINE = re.compile(r'^(\s*)-\s+(table|grid|row|cell|gridcell|columnheader|rowheader)(?:\s+"((?:[^"\\]|\\.)*)")?')


def parse_tables(text: str) -> list[dict]:
    tables, row, row_indent = [], None, -1
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        indent, role, name = len(m.group(1)), m.group(2), (m.group(3) or "").replace('\\"', '"')
        if role in ("table", "grid"):
            tables.append({"name": name, "header": [], "rows": []})
            row = None
        elif not tables:
            continue
        elif role == "row":
            row, row_indent = [], indent
            tables[-1]["rows"].append(row)
        elif row is not None and indent > row_indent:
            if role == "columnheader":
                tables[-1]["header"].append(name)
            row.append(name)
    for t in tables:
        t["rows"] = [r for r in t["rows"] if r and r != t["header"]]
    return tables


async def run(browser, ctx, table: str = "", contains: str = "", path: str = ""):
    if path:
        await browser.navigate(ctx.url(path))
    text = await browser.snapshot()
    tables = parse_tables(text)
    if table:
        tables = [t for t in tables if table.lower() in t["name"].lower()] or tables
    if not tables:
        return {"values": {"rows_found": 0}, "rows": [], "notes": "no table on the page"}
    t = tables[0]
    rows = [r for r in t["rows"] if not contains or any(contains.lower() in c.lower() for c in r)]
    return {"values": {"rows_found": len(rows)}, "header": t["header"],
            "rows": [dict(zip(t["header"], r)) if len(t["header"]) == len(r) else r for r in rows[:50]]}
