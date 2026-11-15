"""Analytics cube: the OLAP operations on one aggregate (POST /api/analytics/cube).

- slice / dice: Explorer conditions (one value of one dimension / conditions on several);
- roll-up / drill-down: fewer / more GROUP BY levels, e.g. month -> quarter -> year (HIERARCHIES);
- pivot: swap `rows` and `cols`.

One query with GROUPING SETS returns the cells together with every row subtotal (each prefix of the
row dimensions), every column subtotal and the grand total; a total level is marked by null.
Dimensions and measures come from the dashboard whitelists; condition values are bound parameters.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hcdw.api.explore import Condition, ExploreError, compile_conditions, source_sql
from hcdw.api.queries import APP_VIEW, CHRONOLOGICAL_DIMS, DIMENSIONS, MEASURES, Warehouse, category_order

HIERARCHIES = {"time": ["application_year", "application_quarter", "application_month"]}
MISSING = "(missing)"  # a NULL member value, so that null can only mean "total"
MAX_ROWS, MAX_COLS = 2, 1


class CubeQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    conditions: list[Condition] = Field(default_factory=list)
    rows: list[str] = Field(default_factory=list)
    cols: list[str] = Field(default_factory=list)
    measure: str = "default_rate"


def _check(q: CubeQuery) -> None:
    if len(q.rows) > MAX_ROWS:
        raise ExploreError(f"rows takes at most {MAX_ROWS} dimensions, got {len(q.rows)}.")
    if len(q.cols) > MAX_COLS:
        raise ExploreError(f"cols takes at most {MAX_COLS} dimension, got {len(q.cols)}.")
    unknown = [d for d in (*q.rows, *q.cols) if d not in DIMENSIONS]
    if unknown:
        raise ExploreError(f"Unknown dimension(s): {', '.join(repr(d)[:40] for d in unknown)}.")
    dims = [*q.rows, *q.cols]
    if len(set(dims)) != len(dims):
        raise ExploreError("A dimension may appear only once across rows and cols.")
    if q.measure not in MEASURES:
        raise ExploreError(f"Unknown measure {q.measure!r:.40}. Use one of: {', '.join(MEASURES)}.")


def _grouping_sets(rows: list[str], cols: list[str]) -> str:
    """Every prefix of the row dimensions (row subtotals) x the column level or its total."""
    sets = []
    for k in range(len(rows), -1, -1):
        for c in ([cols, []] if cols else [[]]):
            sets.append("(" + ", ".join([*rows[:k], *c]) + ")")
    return ", ".join(sets)


class Cube:
    def __init__(self, wh: Warehouse) -> None:
        self.wh = wh

    def run(self, q: CubeQuery) -> dict:
        _check(q)
        where, params = compile_conditions(q.conditions)
        scope = f"WHERE application_id IN (SELECT application_id FROM ({source_sql()}) {where})" if where else ""
        dims = [*q.rows, *q.cols]
        label, expr, kind = MEASURES[q.measure]
        member = [f"CASE WHEN GROUPING({d}) = 1 THEN NULL ELSE COALESCE(CAST({d} AS VARCHAR), '{MISSING}') END "
                  f"AS m_{i}" for i, d in enumerate(dims)]
        group = f"GROUP BY GROUPING SETS ({_grouping_sets(q.rows, q.cols)})" if dims else ""
        sql = (f"SELECT {', '.join([*member, f'{expr} AS value', 'COUNT(*) AS n'])} "
               f"FROM {APP_VIEW} {scope} {group}")
        raw = self.wh.dicts(sql, tuple(params))
        nr = len(q.rows)
        cells = [{"r": [r[f"m_{i}"] for i in range(nr)], "c": r[f"m_{nr}"] if q.cols else None,
                  "value": _finite(r["value"]), "n": int(r["n"])} for r in raw]
        grand = next((c for c in cells if all(m is None for m in c["r"]) and c["c"] is None), None)
        return {
            "rows": list(q.rows), "cols": list(q.cols),
            "measure": {"key": q.measure, "label": label, "format": kind},
            "n": grand["n"] if grand else 0,
            "members": {d: self._members(d, i, nr, cells) for i, d in enumerate(dims)},
            "cells": cells,
            "hierarchies": HIERARCHIES,
        }

    @staticmethod
    def _members(dim: str, i: int, nr: int, cells: list[dict]) -> list[str]:
        """Members of one axis level, ordered like /api/by: time order, the dimension's natural order, or by size."""
        counts: dict[str, int] = {}
        for c in cells:
            if i < nr:  # row level i, at the finest row level and the column total (each row counted once)
                if c["c"] is not None or c["r"][i] is None or any(m is None for m in c["r"]):
                    continue
                m = c["r"][i]
            else:
                if c["c"] is None or any(m is not None for m in c["r"]):
                    continue
                m = c["c"]
            counts[m] = counts.get(m, 0) + c["n"]
        names = [m for m in counts if m != MISSING]
        if dim in CHRONOLOGICAL_DIMS:
            ordered = sorted(names)
        else:
            known = category_order().get(dim)
            if known:
                ordered = [m for m in known if m in counts] + sorted(m for m in names if m not in known)
            else:
                ordered = sorted(names, key=lambda m: (-counts[m], m))
        return ordered + ([MISSING] if MISSING in counts else [])


def _finite(v: Any) -> Any:
    """JSON-safe measure value: Decimal as float, inf/NaN as null."""
    if isinstance(v, Decimal):
        v = float(v)
    return None if isinstance(v, float) and not math.isfinite(v) else v
