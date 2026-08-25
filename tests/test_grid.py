import pytest

from app.engine.grid import GridSpecError, expand, grid_size, validate

# The live 0.3 and 0.7 film lines are width 10..120 by length 10..380. That is
# 111 x 371 = 41,181 cards, a figure confirmed against the marketplace. If a
# change to range handling ever makes this 41,070 or 41,292, the grid is being
# built off-by-one and every downstream diff is wrong.
FILM_GRID = {
    "axes": [
        {"name": "w", "type": "range", "start": 10, "stop": 120, "step": 1},
        {"name": "l", "type": "range", "start": 10, "stop": 380, "step": 1},
    ]
}
FILM_TEMPLATE = "{w} x {l} / прям / глян / 0,3"


def test_film_grid_matches_production_cardinality():
    assert grid_size(FILM_GRID) == 41_181


def test_bounds_are_inclusive():
    spec = {"axes": [{"name": "x", "type": "range", "start": 1, "stop": 3}]}
    assert [s.axes["x"] for s in expand(spec, "{x}")] == [1, 2, 3]


def test_vendor_codes_are_rendered_and_unique():
    skus = expand(FILM_GRID, FILM_TEMPLATE)
    assert len(skus) == 41_181
    assert len({s.vendor_code for s in skus}) == 41_181
    assert skus[0].vendor_code == "10 x 10 / прям / глян / 0,3"


def test_template_omitting_an_axis_is_rejected():
    # Two cells collapsing onto one vendor code would make the line
    # unreconcilable, so this must fail loudly at save time.
    with pytest.raises(GridSpecError, match="duplicate"):
        expand(FILM_GRID, "{w} / плёнка")


def test_template_referencing_unknown_axis_is_rejected():
    # Covers both real axes, so only the unknown name can be the complaint.
    with pytest.raises(GridSpecError, match="not an axis"):
        validate(FILM_GRID, "{w} x {l} x {depth}")


def test_validate_rejects_a_template_that_omits_an_axis():
    """The builder validates on every keystroke and must not call this valid.

    Before this check, a template missing an axis rendered a fine-looking sample
    code and passed. The line saved, and only much later did every cell collapse
    onto one vendor code.
    """
    with pytest.raises(GridSpecError, match="does not use l"):
        validate(FILM_GRID, "{w} / плёнка")


def test_omission_is_caught_statically_not_by_expanding():
    """A 41k-cell expansion per keystroke is not a live check."""
    huge = {
        "axes": [
            {"name": "a", "type": "range", "start": 1, "stop": 100_000},
            {"name": "b", "type": "range", "start": 1, "stop": 100_000},
        ]
    }
    # Guard would reject this on size if it tried to expand; it must fail on the
    # template instead, and instantly.
    with pytest.raises(GridSpecError, match="does not use"):
        validate(huge, "{a}")


def test_list_axis():
    spec = {
        "axes": [
            {"name": "w", "type": "range", "start": 1, "stop": 2},
            {"name": "finish", "type": "list", "values": ["глян", "мат"]},
        ]
    }
    skus = expand(spec, "{w}/{finish}")
    assert {s.vendor_code for s in skus} == {"1/глян", "1/мат", "2/глян", "2/мат"}


def test_oversized_grid_is_refused():
    spec = {
        "axes": [
            {"name": "a", "type": "range", "start": 1, "stop": 3000},
            {"name": "b", "type": "range", "start": 1, "stop": 3000},
        ]
    }
    with pytest.raises(GridSpecError, match="guard"):
        expand(spec, "{a}-{b}")


def test_validate_reports_cost_without_expanding():
    info = validate(FILM_GRID, FILM_TEMPLATE)
    assert info["cells"] == 41_181
    assert info["axes"] == ["w", "l"]
    assert info["sample_vendor_code"] == "10 x 10 / прям / глян / 0,3"


@pytest.mark.parametrize(
    "axis",
    [
        {"name": "x", "type": "range", "start": 5, "stop": 1},
        {"name": "x", "type": "range", "start": 1, "stop": 5, "step": 0},
        {"name": "x", "type": "list", "values": []},
        {"name": "x", "type": "spiral"},
    ],
)
def test_bad_axes_rejected(axis):
    with pytest.raises(GridSpecError):
        grid_size({"axes": [axis]})
