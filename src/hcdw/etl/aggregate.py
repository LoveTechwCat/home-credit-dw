"""Aggregate: collapse the 1..N child tables to one credit profile per applicant, and place each
application on the calendar (SQL in DuckDB)."""
from __future__ import annotations

from hcdw.config import Settings, get_settings
from hcdw.db import run_sql_file, sql_files

AGG_TABLES = [
    "agg_bureau_balance",
    "agg_bureau",
    "agg_previous",
    "agg_installments",
    "agg_pos",
    "agg_cc",
    "agg_credit_profile",
    "application_calendar",
]


def aggregate(con, settings: Settings | None = None) -> dict[str, int]:
    params = (settings or get_settings()).sql_params
    for path in sql_files("transform", prefix="1"):
        run_sql_file(con, path, params)
    return {t: con.execute(f"SELECT COUNT(*) FROM staging.{t}").fetchone()[0] for t in AGG_TABLES}
