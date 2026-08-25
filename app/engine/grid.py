"""Expand a grid spec into the set of SKUs that *should* exist.

A line is defined by axes. Two integer ranges give the film case (width × length),
but an axis may equally be a list of discrete values (finish, colour), so the same
machinery covers lines that are not purely dimensional.

Bounds are **inclusive** on both ends, because the spec is authored by a human in
a form: "10 to 120" should mean 111 values, not 110.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

# A grid is materialised into the database, so an accidental extra axis or a
# step of 0.1 must not quietly try to create tens of millions of rows.
MAX_GRID_CELLS = 2_000_000


class GridSpecError(ValueError):
    pass


@dataclass(slots=True)
class DesiredSku:
    axes: dict[str, Any]
    vendor_code: str


def _axis_values(axis: dict) -> list[Any]:
    name = axis.get("name")
    if not name:
        raise GridSpecError("every axis needs a 'name'")
    kind = axis.get("type", "range")

    if kind == "range":
        try:
            start = int(axis["start"])
            stop = int(axis["stop"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GridSpecError(f"axis {name!r}: range needs integer 'start' and 'stop'") from exc
        step = int(axis.get("step", 1))
        if step <= 0:
            raise GridSpecError(f"axis {name!r}: 'step' must be positive")
        if stop < start:
            raise GridSpecError(f"axis {name!r}: 'stop' is below 'start'")
        return list(range(start, stop + 1, step))  # inclusive

    if kind == "list":
        values = axis.get("values")
        if not isinstance(values, list) or not values:
            raise GridSpecError(f"axis {name!r}: 'values' must be a non-empty list")
        return list(values)

    raise GridSpecError(f"axis {name!r}: unknown type {kind!r}")


def axis_names(spec: dict) -> list[str]:
    return [a["name"] for a in spec.get("axes", []) if a.get("name")]


def grid_size(spec: dict) -> int:
    """Cell count without materialising anything."""
    axes = spec.get("axes") or []
    if not axes:
        raise GridSpecError("grid needs at least one axis")
    total = 1
    for axis in axes:
        total *= len(_axis_values(axis))
    return total


def expand(spec: dict, vendor_code_template: str) -> list[DesiredSku]:
    axes = spec.get("axes") or []
    if not axes:
        raise GridSpecError("grid needs at least one axis")

    size = grid_size(spec)
    if size > MAX_GRID_CELLS:
        raise GridSpecError(
            f"grid expands to {size:,} cells, above the {MAX_GRID_CELLS:,} guard. "
            "Narrow a range or split the line."
        )

    names = [a["name"] for a in axes]
    value_lists = [_axis_values(a) for a in axes]

    out: list[DesiredSku] = []
    seen: set[str] = set()
    for combo in itertools.product(*value_lists):
        values = dict(zip(names, combo, strict=True))
        try:
            vendor_code = vendor_code_template.format(**values)
        except KeyError as exc:
            raise GridSpecError(
                f"vendor code template refers to {exc} which is not an axis "
                f"(axes are: {', '.join(names)})"
            ) from exc
        if vendor_code in seen:
            # Two cells collapsing onto one vendor code would make the line
            # unreconcilable: the diff could never decide which cell is which.
            raise GridSpecError(
                f"vendor code template produces duplicate code {vendor_code!r}. "
                "Include every axis in the template."
            )
        seen.add(vendor_code)
        out.append(DesiredSku(axes=values, vendor_code=vendor_code))
    return out


def validate(spec: dict, vendor_code_template: str) -> dict:
    """Check a spec without expanding it. Used by the builder UI for live feedback."""
    size = grid_size(spec)
    names = axis_names(spec)
    sample_axes = {}
    for axis in spec.get("axes", []):
        sample_axes[axis["name"]] = _axis_values(axis)[0]
    try:
        sample = vendor_code_template.format(**sample_axes)
    except KeyError as exc:
        raise GridSpecError(
            f"vendor code template refers to {exc} which is not an axis "
            f"(axes are: {', '.join(names)})"
        ) from exc
    return {"cells": size, "axes": names, "sample_vendor_code": sample}
