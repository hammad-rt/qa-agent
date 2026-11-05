"""Arithmetic the judge must not do itself: the pack's `invariants` (e.g. wallet W = L - U) are computed in code
from the values a case recorded, and the result goes to the judge as a fact."""
from __future__ import annotations

import ast
import re

TOL = 0.005


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(v or "").replace("\u2212", "-"))
    return float(m.group(0).replace(",", "")) if m else None


def _pick(values: dict, pattern: str):
    """The one recorded value whose name matches; None when none or several match (e.g. W0 and W1)."""
    hits = [(k, _num(v)) for k, v in values.items() if re.search(pattern, str(k), re.I) and _num(v) is not None]
    return hits[0] if len(hits) == 1 else None


_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b}


def _eval(node, env):
    if isinstance(node, ast.Name):
        return env[node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval(node.operand, env)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left, env), _eval(node.right, env))
    raise ValueError("unsupported expression")


def _fmt(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".") if abs(x - round(x)) > 1e-9 else str(int(round(x)))


def check(expr: str, env: dict) -> tuple[bool, str]:
    """expr like 'W == L - U' or 'W >= 0'. Returns (holds, worked text)."""
    tree = ast.parse(expr, mode="eval").body
    if not (isinstance(tree, ast.Compare) and len(tree.ops) == 1):
        raise ValueError("need one comparison")
    left, right = _eval(tree.left, env), _eval(tree.comparators[0], env)
    op = tree.ops[0]
    holds = {ast.Eq: abs(left - right) <= TOL, ast.GtE: left >= right - TOL, ast.LtE: left <= right + TOL,
             ast.Gt: left > right + TOL, ast.Lt: left < right - TOL}[type(op)]
    sym = {ast.Eq: "=", ast.GtE: ">=", ast.LtE: "<=", ast.Gt: ">", ast.Lt: "<"}[type(op)]
    rhs = ast.unparse(tree.comparators[0])
    lhs = ast.unparse(tree.left)
    sub = lambda e: re.sub(r"\b([A-Za-z_]\w*)\b", lambda m: _fmt(env[m.group(1)]) if m.group(1) in env else m.group(1), e)
    work = f"{lhs} {sym} {rhs}: {sub(lhs)} vs {sub(rhs)} = {_fmt(left)} vs {_fmt(right)}"
    if sym == "=":
        work += f" (difference {_fmt(left - right)})"
    return holds, work + (" -> holds" if holds else " -> does NOT hold")


def computed_checks(invariants: list[dict], values: dict, text: str = "") -> list[str]:
    """Worked results for every invariant whose values the case recorded (and, if `when` is set, whose case
    text mentions it)."""
    out = []
    for inv in invariants or []:
        if inv.get("when") and not re.search(inv["when"], text or "", re.I):
            continue
        env, names = {}, {}
        for var, pat in (inv.get("values") or {}).items():
            hit = _pick(values, pat)
            if hit is None:
                break
            names[var], env[var] = hit
        else:
            got = ", ".join(f"{v} = {names[v]} = {_fmt(env[v])}" for v in env)
            for expr in inv.get("checks") or []:
                try:
                    ok, work = check(expr, env)
                except (ValueError, KeyError, SyntaxError):
                    continue
                out.append(f"{inv.get('name', expr)}: {got}; {work}")
    return out
