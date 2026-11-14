"""Load: rebuild dw (dimensions, then facts) and mart views from staging."""
from __future__ import annotations

from hcdw.config import GRADES, SQL_DIR, Settings
from hcdw.db import run_sql_file, sql_files

DW_TABLES = [
    "dim_time",
    "dim_customer",
    "dim_credit_history",
    "dim_contract",
    "dim_region",
    "dim_bureau_category",
    "fact_loan_application",
    "fact_bureau_credit",
    "dim_risk_grade",
    "dim_rule",
    "fact_risk_assessment",  # empty after the ETL; filled by `hcdw score`
    "fact_rule_hit",
]


def load(con, settings: Settings) -> dict[str, int]:
    params = settings.sql_params
    # Full refresh: the source is a static snapshot, so drop and rebuild.
    con.execute("DROP SCHEMA IF EXISTS mart CASCADE")
    con.execute("DROP SCHEMA IF EXISTS dw CASCADE")
    run_sql_file(con, SQL_DIR / "ddl" / "00_schemas.sql")
    run_sql_file(con, SQL_DIR / "ddl" / "02_dimensions.sql", params)
    run_sql_file(con, SQL_DIR / "ddl" / "03_facts.sql", params)
    run_sql_file(con, SQL_DIR / "ddl" / "04_dss.sql", params)
    load_dss_dimensions(con, settings)

    for prefix in ("2", "3"):  # 2x = dimensions, 3x = facts
        for path in sql_files("transform", prefix=prefix):
            run_sql_file(con, path, params)
    build_marts(con, settings)
    return {t: con.execute(f"SELECT COUNT(*) FROM dw.{t}").fetchone()[0] for t in DW_TABLES}


def load_dss_dimensions(con, settings: Settings) -> None:
    """Fill dw.dim_risk_grade and dw.dim_rule from settings.dss (bound parameters, no string SQL)."""
    dss = settings.dss
    t = [float(x) for x in dss["grade_thresholds"]]
    lows, highs = [0.0, *t], [*t, None]
    rows = []
    for rank, grade in enumerate(GRADES, start=1):
        pol = dss["policy"][grade]
        rows.append((grade, rank, lows[rank - 1], highs[rank - 1], dss["grade_labels"][grade], pol["decision"],
                     dss["decision_labels"][pol["decision"]], float(pol["max_credit_to_income"])))
    con.executemany("INSERT INTO dw.dim_risk_grade VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.executemany(
        "INSERT INTO dw.dim_rule VALUES (?, ?, ?, ?, ?)",
        [(r["code"], r["group"], r["label"], r.get("sql"), list(r["requires"])) for r in dss["rules"]],
    )


def build_marts(con, settings: Settings) -> None:
    for path in sql_files("marts"):
        run_sql_file(con, path, settings.sql_params)
