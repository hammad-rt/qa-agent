"""record_value and calculate: every number comes from the page, all math happens here."""
from __future__ import annotations

import ast
import operator as op
from datetime import datetime, timezone

_OPS = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
        ast.USub: op.neg, ast.UAdd: op.pos, ast.Mod: op.mod, ast.Pow: op.pow}
_FUNCS = {"min": min, "max": max, "round": round, "abs": abs}


class CalcError(ValueError):
    pass


def calculate(expr: str, values: dict[str, float]) -> float:
    """Safely evaluate arithmetic over named recorded values, e.g. "0.10 * shipping_unpaid"."""
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.Name):
            if n.id not in values:
                raise CalcError(f"unknown value '{n.id}' (record it first)")
            return float(values[n.id])
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise CalcError(f"not allowed in calculate: {ast.dump(n)[:60]}")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise CalcError(str(e)) from e
    return float(ev(tree))


def parse_money(text: str) -> float:
    """'$1,234.50' / 'KES 50,000' / '-$20.50' -> float."""
    import re
    t = text.replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    if not m:
        raise ValueError(f"no number in {text!r}")
    v = float(m.group())
    if "-" in t.split(m.group())[0] and v > 0:
        v = -v
    return v


def record(state_values: dict, name: str, value, source: str, evidence: str | None = None) -> dict:
    entry = {"value": value, "source": source, "evidence": evidence,
             "at": datetime.now(timezone.utc).isoformat()}
    state_values[name] = entry
    return entry
