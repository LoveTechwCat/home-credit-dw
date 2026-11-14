"""Data-quality checks on the loaded warehouse. Results go to data/warehouse/dq_report.json."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from hcdw.config import GRADES, Settings, get_settings

# Composite keys are tuples.
PRIMARY_KEYS: dict[str, str | tuple[str, ...]] = {
    "dim_time": "date_key",
    "dim_customer": "customer_key",
    "dim_credit_history": "credit_history_key",
    "dim_contract": "contract_key",
    "dim_region": "region_key",
    "dim_bureau_category": "bureau_category_key",
    "fact_loan_application": "application_id",
    "fact_bureau_credit": "bureau_credit_id",
    "dim_risk_grade": "grade",
    "dim_rule": "rule_code",
    "fact_risk_assessment": "application_id",
    "fact_rule_hit": ("application_id", "rule_code"),
}

FOREIGN_KEYS = [
    ("fact_loan_application", "customer_key", "dim_customer", "customer_key"),
    ("fact_loan_application", "credit_history_key", "dim_credit_history", "credit_history_key"),
    ("fact_loan_application", "contract_key", "dim_contract", "contract_key"),
    ("fact_loan_application", "region_key", "dim_region", "region_key"),
    ("fact_loan_application", "application_date_key", "dim_time", "date_key"),
    ("fact_bureau_credit", "customer_key", "dim_customer", "customer_key"),
    ("fact_bureau_credit", "bureau_category_key", "dim_bureau_category", "bureau_category_key"),
    ("fact_bureau_credit", "open_date_key", "dim_time", "date_key"),
    ("fact_bureau_credit", "close_date_key", "dim_time", "date_key"),  # nullable: NULLs are not orphans
    ("fact_loan_application", "outcome_date_key", "dim_time", "date_key"),  # nullable: result not known
    ("fact_risk_assessment", "application_id", "fact_loan_application", "application_id"),
    ("fact_risk_assessment", "score_grade", "dim_risk_grade", "grade"),
    ("fact_risk_assessment", "rule_grade", "dim_risk_grade", "grade"),
    ("fact_risk_assessment", "final_grade", "dim_risk_grade", "grade"),
    ("fact_rule_hit", "application_id", "fact_risk_assessment", "application_id"),
    ("fact_rule_hit", "rule_code", "dim_rule", "rule_code"),
    ("fact_rule_hit", "grade", "dim_risk_grade", "grade"),
]

# Rate -> grade, the same left-closed lookup the mart views use.
_GRADE_OF = "(SELECT g.grade FROM dw.dim_risk_grade g WHERE {rate} >= g.min_rate AND (g.max_rate IS NULL OR {rate} < g.max_rate))"

NULL_PROFILE_COLUMNS = [
    "target", "amt_annuity", "amt_goods_price", "debt_to_income",
    "ext_source_mean", "prev_approval_rate", "late_payment_ratio",
]


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


def _one(con, sql: str):
    return con.execute(sql).fetchone()[0]


def _close(a: float, b: float, rel: float = 1e-9) -> bool:
    return abs(a - b) <= rel * max(abs(a), abs(b), 1.0)


def run_checks(con, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    checks: list[Check] = []

    for table, pk in PRIMARY_KEYS.items():
        cols = (pk,) if isinstance(pk, str) else pk
        dupes = _one(con, f"SELECT COUNT(*) - COUNT(DISTINCT ({', '.join(cols)})) FROM dw.{table}")
        nulls = _one(con, f"SELECT COUNT(*) FROM dw.{table} WHERE {' OR '.join(f'{c} IS NULL' for c in cols)}")
        checks.append(Check(f"pk_unique:{table}", dupes == 0 and nulls == 0,
                            f"{dupes} duplicate, {nulls} null keys"))

    for fact, col, dim, dim_col in FOREIGN_KEYS:
        orphans = _one(con, f"""
            SELECT COUNT(*) FROM dw.{fact} f
            WHERE f.{col} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM dw.{dim} d WHERE d.{dim_col} = f.{col})""")
        checks.append(Check(f"fk:{fact}.{col}->{dim}", orphans == 0, f"{orphans} orphan rows"))

    staged = _one(con, "SELECT COUNT(*) FROM staging.application")
    loaded = _one(con, "SELECT COUNT(*) FROM dw.fact_loan_application")
    checks.append(Check("completeness:applications", staged == loaded,
                        f"staging {staged} vs fact {loaded}"))

    bad_target = _one(con, "SELECT COUNT(*) FROM dw.fact_loan_application WHERE NOT is_train AND target IS NOT NULL")
    checks.append(Check("target_only_on_train", bad_target == 0, f"{bad_target} test rows with TARGET"))

    # Reconciliation: the money that went in is the money that came out.
    src_credit = _one(con, "SELECT COALESCE(SUM(amt_credit), 0) FROM staging.application")
    dw_credit = _one(con, "SELECT COALESCE(SUM(amt_credit), 0) FROM dw.fact_loan_application")
    checks.append(Check("reconcile:sum_amt_credit", _close(src_credit, dw_credit),
                        f"staging {src_credit:,.0f} vs fact {dw_credit:,.0f}"))

    # Bureau rows can only be loaded for applicants we know and with an open date.
    expected_b = _one(con, """
        SELECT COUNT(*) FROM staging.bureau b
        WHERE b.sk_id_bureau IS NOT NULL AND b.days_credit IS NOT NULL
          AND b.sk_id_curr IN (SELECT sk_id_curr FROM staging.application)""")
    loaded_b = _one(con, "SELECT COUNT(*) FROM dw.fact_bureau_credit")
    checks.append(Check("completeness:bureau_credits", expected_b == loaded_b,
                        f"loadable staging {expected_b} vs fact {loaded_b}"))
    src_b = _one(con, """
        SELECT COALESCE(SUM(amt_credit_sum), 0) FROM staging.bureau
        WHERE sk_id_bureau IS NOT NULL AND days_credit IS NOT NULL
          AND sk_id_curr IN (SELECT sk_id_curr FROM staging.application)""")
    dw_b = _one(con, "SELECT COALESCE(SUM(amt_credit_sum), 0) FROM dw.fact_bureau_credit")
    checks.append(Check("reconcile:sum_bureau_credit", _close(src_b, dw_b),
                        f"staging {src_b:,.0f} vs fact {dw_b:,.0f}"))

    checks += _date_checks(con, settings)
    checks += _dss_checks(con, settings, loaded)

    # Informational (always pass): what share of the source survives, and data-shape facts for the report.
    staged_b = _one(con, "SELECT COUNT(*) FROM staging.bureau")
    coverage = loaded_b / staged_b if staged_b else 1.0
    checks.append(Check("info:bureau_coverage", True,
                        f"{loaded_b}/{staged_b} bureau rows ({coverage:.1%}); the rest belong to unknown applicants"))
    closed, dated = con.execute("""
        SELECT COUNT(*) FILTER (WHERE g.credit_active = 'Closed'),
               COUNT(*) FILTER (WHERE g.credit_active = 'Closed' AND f.close_date_key IS NOT NULL)
        FROM dw.fact_bureau_credit f JOIN dw.dim_bureau_category g USING (bureau_category_key)""").fetchone()
    checks.append(Check("info:close_date_coverage", True, f"{dated}/{closed} closed credits have a usable close date"))
    rate = _one(con, "SELECT AVG(target) FROM dw.fact_loan_application WHERE is_train")
    checks.append(Check("info:train_default_rate", True,
                        "no labelled rows" if rate is None else f"{rate:.2%} of labelled applications defaulted"))

    total = max(loaded, 1)
    null_pct = {
        c: round(_one(con, f"SELECT COUNT(*) FROM dw.fact_loan_application WHERE {c} IS NULL") / total * 100, 2)
        for c in NULL_PROFILE_COLUMNS
    }
    row_counts = {t: _one(con, f"SELECT COUNT(*) FROM dw.{t}") for t in PRIMARY_KEYS}

    return {
        "passed": all(c.passed for c in checks),
        "checks": [asdict(c) for c in checks],
        "row_counts": row_counts,
        "null_pct_fact_loan_application": null_pct,
    }


def _date_checks(con, settings: Settings) -> list[Check]:
    """Optional application_dates source, validated like any source, and the dates derived from it.

    Without the source every row-level check sees 0 rows and passes; the absence is reported in the
    detail (the warehouse then places every application on the snapshot date)."""
    anchor = f"DATE '{settings.anchor_date}'"
    n_src = _one(con, "SELECT COUNT(*) FROM staging.application_dates")
    absent = "source absent: every application sits on the snapshot date"
    parsed = """(SELECT d.*, TRY_CAST(d.application_date AS DATE) AS app_d, TRY_CAST(d.outcome_date AS DATE) AS out_d,
                        a.is_train
                 FROM staging.application_dates d LEFT JOIN staging.application a USING (sk_id_curr))"""

    dupes, unknown = con.execute("""
        SELECT COUNT(*) - COUNT(DISTINCT sk_id_curr),
               COUNT(*) FILTER (WHERE sk_id_curr IS NULL
                                   OR sk_id_curr NOT IN (SELECT sk_id_curr FROM staging.application))
        FROM staging.application_dates""").fetchone()
    missing = _one(con, """SELECT COUNT(*) FROM staging.application
                           WHERE sk_id_curr NOT IN (SELECT sk_id_curr FROM staging.application_dates
                                                    WHERE sk_id_curr IS NOT NULL)""") if n_src else 0
    checks = [Check("source:application_dates_one_row_per_application", dupes == 0 and unknown == 0 and missing == 0,
                    absent if n_src == 0 else
                    f"{n_src} rows: {dupes} duplicate, {unknown} unknown applicant, {missing} applications without a row")]

    unparsed = _one(con, f"""SELECT COUNT(*) FROM {parsed}
                             WHERE app_d IS NULL OR (NULLIF(TRIM(outcome_date), '') IS NOT NULL AND out_d IS NULL)""")
    checks.append(Check("source:application_dates_parse", unparsed == 0,
                        absent if n_src == 0 else f"{unparsed} rows with a missing or unparseable date"))
    for name, cond, what in [
        ("source:outcome_not_before_application", "out_d < app_d", "outcome before its application"),
        ("source:dates_not_after_snapshot", f"app_d > {anchor} OR out_d > {anchor}", "date after the snapshot"),
        ("source:test_rows_have_no_outcome", "NOT is_train AND out_d IS NOT NULL", "new application with an outcome"),
    ]:
        bad = _one(con, f"SELECT COUNT(*) FROM {parsed} WHERE {cond}")
        checks.append(Check(name, bad == 0, absent if n_src == 0 else f"{bad} rows: {what}"))

    # Bureau offsets count from the application date, so no credit can open after it.
    late = _one(con, """
        SELECT COUNT(*) FROM dw.fact_bureau_credit b
        JOIN dw.fact_loan_application a USING (customer_key)
        WHERE b.open_date_key > a.application_date_key""")
    checks.append(Check("dates:bureau_open_not_after_application", late == 0,
                        f"{late} bureau credits opened after the application"))
    return checks


def _dss_checks(con, settings: Settings, n_applications: int) -> list[Check]:
    """DSS tables: grade/rule dimensions mirror settings; scoring results (written by `hcdw score`)
    are checked only when present, so a fresh ETL with empty fact_risk_assessment passes."""
    dss = settings.dss
    t = [float(x) for x in dss["grade_thresholds"]]
    expected_grades = [
        (g, rank, lo, hi, dss["policy"][g]["decision"], float(dss["policy"][g]["max_credit_to_income"]))
        for rank, (g, lo, hi) in enumerate(zip(GRADES, [0.0, *t], [*t, None]), start=1)
    ]
    grades = con.execute("""SELECT grade, grade_rank, min_rate, max_rate, decision, max_credit_to_income
                            FROM dw.dim_risk_grade ORDER BY grade_rank""").fetchall()
    rules = con.execute("SELECT rule_code, rule_group, sql_condition FROM dw.dim_rule ORDER BY 1").fetchall()
    expected_rules = sorted((r["code"], r["group"], r.get("sql")) for r in dss["rules"])
    checks = [Check("dss:grade_dims_match_settings", grades == expected_grades and rules == expected_rules,
                    f"{len(grades)} grades, {len(rules)} rules vs settings.dss")]

    scored = _one(con, "SELECT COUNT(*) FROM dw.fact_risk_assessment")
    checks.append(Check("completeness:risk_assessment", scored in (0, n_applications),
                        "not scored yet (run `hcdw score`)" if scored == 0
                        else f"{scored} of {n_applications} applications scored"))
    # Each check counts violating rows; all are 0 on empty tables.
    rules_sql = {
        "dss:score_grade_matches_pd":
            f"SELECT COUNT(*) FROM dw.fact_risk_assessment a WHERE a.score_grade IS DISTINCT FROM {_GRADE_OF.format(rate='a.pd')}",
        "dss:rule_grade_is_worst_hit": """
            SELECT COUNT(*) FROM dw.fact_risk_assessment a
            LEFT JOIN (SELECT application_id, COUNT(*) AS n, MAX(grade) AS worst
                       FROM dw.fact_rule_hit GROUP BY 1) h USING (application_id)
            WHERE a.n_rule_hits <> COALESCE(h.n, 0) OR a.rule_grade IS DISTINCT FROM h.worst""",
        "dss:final_not_better_than_score": """
            SELECT COUNT(*) FROM dw.fact_risk_assessment
            WHERE final_grade <> GREATEST(score_grade, COALESCE(rule_grade, score_grade))""",
        "dss:policy_matches_final_grade": """
            SELECT COUNT(*) FROM dw.fact_risk_assessment a JOIN dw.dim_risk_grade g ON g.grade = a.final_grade
            WHERE a.decision <> g.decision OR a.max_credit_to_income <> g.max_credit_to_income""",
        "dss:hit_grade_matches_rate":
            f"SELECT COUNT(*) FROM dw.fact_rule_hit h WHERE h.grade IS DISTINCT FROM {_GRADE_OF.format(rate='h.rate')}",
    }
    for name, sql in rules_sql.items():
        bad = _one(con, sql)
        checks.append(Check(name, bad == 0, f"{bad} of {scored} scored rows inconsistent"))
    return checks
