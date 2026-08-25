"""Price rules.

A no-code builder means a human types an arithmetic expression into a form, and
that expression is then executed by a background worker holding credentials that
can rewrite the whole catalogue. ``eval()`` is therefore not an option: it would
turn a price field into remote code execution.

Instead the expression is parsed with :mod:`ast` and walked against a strict
allow-list of node types. Anything outside plain arithmetic over declared
variables -- attribute access, calls to arbitrary names, comprehensions, imports --
is rejected at save time, not at run time.
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_CMP_OPS = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}

# Deliberately small. Every entry is a pure numeric function with no side effects.
_FUNCS = {
    "min": min,
    "max": max,
    "round": round,
    "abs": abs,
    "ceil": math.ceil,
    "floor": math.floor,
    "sqrt": math.sqrt,
}


class PriceRuleError(ValueError):
    pass


def _eval(node: ast.AST, names: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, names)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise PriceRuleError(f"only numeric constants are allowed, got {node.value!r}")

    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        raise PriceRuleError(
            f"unknown name {node.id!r}; available: {', '.join(sorted(names)) or '(none)'}"
        )

    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise PriceRuleError(f"operator {type(node.op).__name__} is not allowed")
        right = _eval(node.right, names)
        if op in (operator.truediv, operator.floordiv, operator.mod) and right == 0:
            raise PriceRuleError("division by zero")
        return op(_eval(node.left, names), right)

    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise PriceRuleError(f"unary {type(node.op).__name__} is not allowed")
        return op(_eval(node.operand, names))

    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise PriceRuleError("chained comparisons are not allowed")
        op = _CMP_OPS.get(type(node.ops[0]))
        if op is None:
            raise PriceRuleError("comparison operator is not allowed")
        return op(_eval(node.left, names), _eval(node.comparators[0], names))

    if isinstance(node, ast.IfExp):  # a if cond else b
        return _eval(node.body, names) if _eval(node.test, names) else _eval(node.orelse, names)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            allowed = ", ".join(sorted(_FUNCS))
            raise PriceRuleError(f"only these functions may be called: {allowed}")
        if node.keywords:
            raise PriceRuleError("keyword arguments are not allowed")
        return _FUNCS[node.func.id](*[_eval(a, names) for a in node.args])

    raise PriceRuleError(f"{type(node).__name__} is not allowed in a price expression")


def evaluate(expr: str, variables: dict[str, Any]) -> float:
    if not expr or not expr.strip():
        raise PriceRuleError("expression is empty")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise PriceRuleError(f"could not parse expression: {exc.msg}") from exc
    result = _eval(tree, variables)
    if not isinstance(result, (int, float)) or isinstance(result, bool):
        raise PriceRuleError(f"expression produced {type(result).__name__}, expected a number")
    if result != result or result in (float("inf"), float("-inf")):  # NaN / inf
        raise PriceRuleError("expression produced a non-finite number")
    return float(result)


def compute_price(rule: dict, axes: dict[str, Any]) -> tuple[int, int]:
    """Return ``(price, discount)`` for one SKU.

    ``rule`` shape::

        {"type": "formula",
         "expr": "w * l * ratio",
         "vars": {"ratio": 0.00010027},
         "min_price": 100,
         "round_to": 1,
         "discount": 0}
    """
    kind = rule.get("type", "formula")
    discount = int(rule.get("discount", 0) or 0)
    if not 0 <= discount <= 99:
        raise PriceRuleError("discount must be between 0 and 99")

    if kind == "constant":
        price = float(rule.get("value", 0))
    elif kind == "formula":
        names: dict[str, Any] = {**(rule.get("vars") or {}), **axes}
        price = evaluate(rule.get("expr", ""), names)
    else:
        raise PriceRuleError(f"unknown price rule type {kind!r}")

    round_to = int(rule.get("round_to", 1) or 1)
    if round_to > 1:
        price = round(price / round_to) * round_to

    min_price = rule.get("min_price")
    if min_price is not None:
        price = max(float(min_price), price)
    max_price = rule.get("max_price")
    if max_price is not None:
        price = min(float(max_price), price)

    price_int = int(round(price))
    if price_int <= 0:
        raise PriceRuleError(f"computed price {price_int} is not positive")
    return price_int, discount
