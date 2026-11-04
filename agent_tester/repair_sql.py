"""Sort the SQL that SuiteCRM/SugarCRM Quick Repair and Rebuild proposes ("Differences found between database and
vardefs" + Execute). Adding what the new code needs (new columns, tables, indexes) is the normal next step after a
deploy; anything that drops, changes or rewrites existing data always needs a person."""
from __future__ import annotations

import re

ADDITIVE = "execute_repair_sql"                 # pre-approvable for staging/preprod at the plan gate
DESTRUCTIVE = "repair_sql_destructive"          # always a one-off approval, never automatic

_ADD = re.compile(r"""(?isx)^\s*(
      create\s+table\b.*
    | create\s+(unique\s+)?index\b.*
    | alter\s+table\s+\S+\s+(
          (add\s+(column\s+)?(?!(primary|constraint|foreign)\b)\S+(\s+\S+)*)            # ADD [COLUMN] x ...
        | (add\s+(unique\s+)?(index|key)\b.*)
      )
)\s*;?\s*$""")
_BAD = re.compile(r"(?i)\b(drop|modify|change|rename|truncate|delete|update|replace|insert|grant|revoke)\b")


def statements(sql: str) -> list[str]:
    """Split on ';' outside quotes and drop comments and blank lines."""
    sql = re.sub(r"(?m)^\s*(--|#|/\*).*$", "", sql or "")
    out, cur, q = [], [], None
    for ch in sql:
        if q:
            q = None if ch == q else q
        elif ch in "'\"`":
            q = ch
        elif ch == ";":
            out.append("".join(cur)); cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return [" ".join(s.split()) for s in out if s.strip()]


def _parts(stmt: str) -> list[str]:
    """ALTER TABLE t ADD a ..., ADD b ...  ->  one ALTER per clause, so a hidden MODIFY can't ride along."""
    m = re.match(r"(?is)^(alter\s+table\s+\S+)\s+(.*)$", stmt)
    if not m:
        return [stmt]
    clauses, depth, cur = [], 0, []
    for ch in m.group(2):
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            clauses.append("".join(cur)); cur = []
            continue
        cur.append(ch)
    clauses.append("".join(cur))
    return [f"{m.group(1)} {c.strip()}" for c in clauses if c.strip()]


def classify(sql: str) -> dict:
    """{'action_type', 'additive': [...], 'risky': [...]}; empty SQL -> action_type None (nothing to execute)."""
    add, risky = [], []
    for st in statements(sql):
        parts = _parts(st)
        ok = all(_ADD.match(p) and not _BAD.search(re.sub(r"(?i)\b(default|comment)\s+('[^']*'|\S+)", "", p))
                 for p in parts)
        (add if ok else risky).append(st)
    kind = None if not (add or risky) else DESTRUCTIVE if risky else ADDITIVE
    return {"action_type": kind, "additive": add, "risky": risky}
