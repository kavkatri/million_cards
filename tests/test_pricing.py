import pytest

from app.engine.pricing import PriceRuleError, compute_price, evaluate

# The live 0.3 line prices as width * length * ratio.
FILM_RULE = {"type": "formula", "expr": "w * l * ratio", "vars": {"ratio": 0.00010027}}


def test_formula_uses_axes_and_vars():
    assert evaluate("w * l * ratio", {"w": 100, "l": 200, "ratio": 0.5}) == 10_000


def test_compute_price_rounds_and_floors():
    rule = {**FILM_RULE, "min_price": 100, "round_to": 10}
    price, discount = compute_price(rule, {"w": 27, "l": 107})
    assert price == 100  # tiny sizes floor at the minimum
    assert discount == 0


def test_round_to_snaps_to_multiple():
    rule = {"type": "formula", "expr": "w * l", "round_to": 50}
    price, _ = compute_price(rule, {"w": 30, "l": 41})  # 1230 -> 1250
    assert price % 50 == 0


def test_conditional_expression_supported():
    assert evaluate("100 if w > 50 else 50", {"w": 60}) == 100
    assert evaluate("100 if w > 50 else 50", {"w": 10}) == 50


def test_allowed_functions():
    assert evaluate("max(10, min(5, 20))", {}) == 10
    assert evaluate("ceil(1.2)", {}) == 2


# --- the security-relevant half -------------------------------------------
# A price formula is typed into a web form by a user and then executed by a
# worker holding credentials that can rewrite the entire catalogue. These must
# all be rejected at parse time, never evaluated.


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('id')",
        "().__class__.__bases__[0].__subclasses__()",
        "open('/etc/passwd').read()",
        "exec('x=1')",
        "[x for x in range(10)]",
        "lambda: 1",
        "w.__class__",
        "'string'",
        "print(1)",
    ],
)
def test_dangerous_expressions_are_refused(expr):
    with pytest.raises(PriceRuleError):
        evaluate(expr, {"w": 1})


def test_unknown_name_is_refused():
    with pytest.raises(PriceRuleError, match="unknown name"):
        evaluate("w * missing", {"w": 1})


def test_division_by_zero_is_refused():
    with pytest.raises(PriceRuleError, match="zero"):
        evaluate("w / 0", {"w": 1})


def test_non_positive_price_is_refused():
    with pytest.raises(PriceRuleError, match="not positive"):
        compute_price({"type": "formula", "expr": "w - w"}, {"w": 5})


def test_invalid_discount_refused():
    with pytest.raises(PriceRuleError, match="discount"):
        compute_price({**FILM_RULE, "discount": 150}, {"w": 10, "l": 10})


def test_syntax_error_reported_clearly():
    with pytest.raises(PriceRuleError, match="could not parse"):
        evaluate("w * * l", {"w": 1, "l": 1})
