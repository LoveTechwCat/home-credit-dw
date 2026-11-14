"""Every SQL statement the dashboard runs lives here (OLAP layer of the web app).

Rules that keep this safe and fast:
  * Dimension and measure names are whitelisted (DIMENSIONS / MEASURES) before they are formatted
    into SQL; member values always travel as bound parameters.
  * Filters are a hashable tuple, so every query is memoised with lru_cache: the warehouse is a
    static snapshot, so a result never goes stale until the ETL rebuilds the file (restart the API).
  * One read-only DuckDB connection, one cursor per query (cursors are cheap and thread-safe to
    create; the connection object itself must not be shared across threads).
"""
from __future__ import annotations

import json
import math
from decimal import Decimal
import threading
from functools import lru_cache
from pathlib import Path

import duckdb
import pandas as pd

from hcdw.config import GRADES, SQL_DIR, get_settings
from hcdw.db import get_conn

WIDE_VIEW = "mart.v_application_wide"
NO_RULE_HIT = "No rule hit"
# What the dashboard queries read: the wide view, with a scored row's NULL rule_grade ("no rule violated",
# most rows) shown as its own member so it can be grouped and filtered. Unscored rows stay NULL.
DECISION_CODES = ("approve", "review", "reject")


def _sql_text(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def decision_labels() -> list[str]:
    """Display labels of approve / review / reject (settings dss.decision_labels), in that order."""
    labels = get_settings().dss["decision_labels"]
    return [labels[c] for c in DECISION_CODES]


# The decision dimension shows labels (Approve / Manual review / Reject), not the stored codes.
_DECISION_SQL = "CASE decision " + " ".join(
    f"WHEN {_sql_text(c)} THEN {_sql_text(lbl)}" for c, lbl in zip(DECISION_CODES, decision_labels())) + " END"
APPROVE_LABEL = decision_labels()[0]
APP_VIEW = (f"(SELECT * REPLACE (CASE WHEN final_grade IS NOT NULL THEN COALESCE(rule_grade, '{NO_RULE_HIT}') "
            f"END AS rule_grade, {_DECISION_SQL} AS decision) FROM {WIDE_VIEW}) AS app")
BUREAU_VIEW = "mart.v_bureau_credit_wide"
TOTAL = "ALL"
CREDIT_HISTORY_STATUSES = ("No bureau", "Clean history", "Current overdue")
GRADE_SCOPES = {"train": "f.is_train", "test": "NOT f.is_train", "all": "TRUE"}

# Columns the UI may group or filter by.
DIMENSIONS: dict[str, str] = {
    "age_bucket": "Age bucket",
    "income_bracket": "Income bracket",
    "income_type": "Employment type",
    "occupation_type": "Occupation",
    "education_type": "Education",
    "family_status": "Family status",
    "gender": "Gender",
    "contract_type": "Contract type",
    "housing_type": "Housing",
    "bureau_active_bucket": "Active bureau credits",
    "max_dpd_bucket": "Max days past due",
    "prev_approval_bucket": "Previous approval rate",
    "population_density_band": "Region density",
    "appr_weekday": "Application weekday",
    # DSS result (NULL until `hcdw score`; rule_grade NULL = no rule violated)
    "score_grade": "Score grade",
    "rule_grade": "Rule grade",
    "final_grade": "Final grade",
    "credit_history_status": "Credit history status",
    "decision": "Decision",
    # application calendar (members sort chronologically: YYYY, YYYY-Qn, YYYY-MM)
    "application_year": "Application year",
    "application_quarter": "Application quarter",
    "application_month": "Application month",
}
CHRONOLOGICAL_DIMS = ("application_year", "application_quarter", "application_month")
FILTER_DIMS = ["age_bucket", "income_bracket", "income_type", "gender", "contract_type", "education_type"]

# label, SQL expression, display kind
MEASURES: dict[str, tuple[str, str, str]] = {
    "default_rate": ("Default rate", "AVG(target) FILTER (WHERE is_train)", "percent"),
    "n_applications": ("Applications", "COUNT(*)", "count"),
    "overdue_share": ("Overdue share", "AVG(overdue_flag::INTEGER)", "percent"),
    "avg_credit": ("Avg credit", "AVG(amt_credit)", "money"),
    "total_credit": ("Total credit", "SUM(amt_credit)", "money"),
    "median_dti": ("Median debt-to-income", "MEDIAN(debt_to_income)", "ratio"),
    "avg_ext_source": ("Avg external score", "AVG(ext_source_mean)", "ratio"),
    "expected_loss": ("Credit at risk", "SUM(amt_credit * pd)", "money"),
    "approval_rate": ("Approval rate",
                      # APP_VIEW carries the decision label (see _DECISION_SQL)
                      f"AVG(CASE WHEN decision = {_sql_text(APPROVE_LABEL)} THEN 1.0 "
                      "WHEN decision IS NOT NULL THEN 0.0 END)",
                      "percent"),
}

Filters = tuple[tuple[str, tuple[str, ...]], ...]


def category_order() -> dict[str, list[str]]:
    s = get_settings()
    return {
        "age_bucket": s.bucket("age_years")[1] + ["Unknown"],
        "income_bracket": s.bucket("income")[1] + ["Unknown"],
        "max_dpd_bucket": ["No DPD", "1-30", "31-60", "61-90", "90+"],
        "bureau_active_bucket": ["No bureau record", "0", "1-2", "3-5", "6+"],
        "prev_approval_bucket": ["No previous", "No decision", "<50%", "50-99%", "100%"],
        "population_density_band": ["Low", "Medium", "High", "Unknown"],
        "appr_weekday": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"],
        "score_grade": list(GRADES),
        "rule_grade": [NO_RULE_HIT, *GRADES],
        "decision": decision_labels(),
        "final_grade": list(GRADES),
        "credit_history_status": list(CREDIT_HISTORY_STATUSES),
    }


class QueryError(ValueError):
    """A request named something the dashboard does not know (the API answers 400)."""


def check_dim(dim: str) -> str:
    if dim not in DIMENSIONS:
        raise QueryError(f"Unknown dimension '{dim}'")
    return dim


def make_filters(raw: dict[str, list[str]]) -> Filters:
    """Normalise {dim: [members]} into a sorted, hashable tuple (unknown dims are rejected)."""
    return tuple(sorted((check_dim(d), tuple(sorted(set(v)))) for d, v in raw.items() if v))


def where(filters: Filters, extra: str | None = None) -> tuple[str, tuple]:
    clauses, params = [], []
    for dim, values in filters:
        clauses.append(f"{check_dim(dim)} IN ({', '.join('?' for _ in values)})")
        params.extend(values)
    if extra:
        clauses.append(extra)
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), tuple(params)


class Warehouse:
    """Lazily opened read-only connection to the warehouse."""

    def __init__(self, dw_path: Path | None = None) -> None:
        self.dw_path = dw_path
        self._con: duckdb.DuckDBPyConnection | None = None
        self._lock = threading.Lock()

    def connection(self) -> duckdb.DuckDBPyConnection:
        with self._lock:
            if self._con is None:
                s = get_settings()
                if self.dw_path is not None:
                    self._con = get_conn("local", read_only=True, path=self.dw_path)
                else:
                    self._con = get_conn(read_only=s.dw_target == "local")
            return self._con

    def df(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        return self.connection().cursor().execute(sql, list(params)).df()

    def dicts(self, sql: str, params: tuple = ()) -> list[dict]:
        """Rows as dicts of native Python values, at full float precision (no JSON round trip)."""
        cur = self.connection().cursor()
        try:
            cur.execute(sql, list(params))
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]
        finally:
            cur.close()

    def close(self) -> None:
        with self._lock:
            if self._con is not None:
                self._con.close()
                self._con = None


def records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-safe records (NaN -> null, numpy scalars -> Python, dates -> ISO)."""
    return json.loads(df.to_json(orient="records", date_format="iso"))


def row(df: pd.DataFrame) -> dict:
    out = records(df)
    return out[0] if out else {}


class Queries:
    """All dashboard queries, memoised per (filters, args)."""

    def __init__(self, wh: Warehouse) -> None:
        self.wh = wh
        # lru_cache on bound methods would pin `self`; wrap per instance instead.
        for name in ("members", "kpis", "by_dimension", "cross", "score_curve", "dti_by_outcome",
                     "hierarchy", "bureau_kpis", "bureau_trend", "bureau_by_type", "pivot", "run_olap",
                     "warehouse_info", "n_scored", "last_scored_at", "grades", "kpi_summary",
                     "pd_distribution", "rule_list"):
            setattr(self, name, lru_cache(maxsize=512)(getattr(self, name)))

    # ---- metadata -----------------------------------------------------------------------------
    def members(self, dim: str) -> list[str]:
        check_dim(dim)
        values = self.wh.df(f"SELECT DISTINCT {dim} AS v FROM {APP_VIEW} WHERE {dim} IS NOT NULL")["v"]
        values = [str(v) for v in values]
        order = category_order().get(dim)
        if order:
            return [v for v in order if v in values] + sorted(v for v in values if v not in order)
        return sorted(values)

    def warehouse_info(self) -> dict:
        counts = self.wh.df("""
            SELECT schema_name, table_name, estimated_size AS n_rows
            FROM duckdb_tables() WHERE schema_name = 'dw' ORDER BY table_name""")
        return {"tables": records(counts)}

    # ---- application fact ---------------------------------------------------------------------
    def kpis(self, filters: Filters) -> dict:
        w, p = where(filters)
        return row(self.wh.df(f"""
            SELECT
                COUNT(*)                                AS n_applications,
                COUNT(*) FILTER (WHERE is_train)        AS n_labelled,
                AVG(target) FILTER (WHERE is_train)     AS default_rate,
                MEDIAN(debt_to_income)                  AS median_dti,
                AVG(overdue_flag::INTEGER)              AS overdue_share,
                AVG(amt_credit)                         AS avg_credit,
                SUM(amt_credit)                         AS total_credit,
                AVG(ext_source_mean)                    AS avg_ext_source,
                SUM(amt_credit * pd)                    AS expected_loss  -- scored rows only; NULL before scoring
            FROM {APP_VIEW} {w}""", p))

    def pd_distribution(self) -> dict:
        """PD histogram of the scored historical loans (dss.policy.pd_distribution); cached until restart."""
        from dss.policy import pd_distribution

        cur = self.wh.connection().cursor()
        try:
            return pd_distribution(cur)
        finally:
            cur.close()

    def rule_list(self) -> list[dict]:
        """dss.rules (config order) joined with mart.rule_stats: violators, defaults, default rate, grade."""
        from dss.policy import rule_configs

        stats = {r["rule_code"]: r for r in self.wh.dicts(
            "SELECT rule_code, n_violators, default_rate, grade FROM mart.rule_stats")}
        out = []
        for r in rule_configs():
            st = stats.get(r["code"], {})
            n, rate = int(st.get("n_violators") or 0), st.get("default_rate")
            out.append({"code": r["code"], "group": r["group"], "label": r["label"], "param": r.get("param"),
                        "n_violators": n, "defaults": int(round(n * rate)) if rate is not None else 0,
                        "default_rate": rate, "grade": st.get("grade")})
        return out

    def kpi_summary(self) -> dict:
        """Overview KPIs = mart.kpi_portfolio_summary, the single source (counts, decisions, credit and credit
        at risk on every scored application; default rates on the labelled rows). Non-finite -> null."""
        out = self.wh.dicts("SELECT * FROM mart.kpi_portfolio_summary")[0]
        return {k: None if isinstance(v, float) and not math.isfinite(v) else
                float(v) if isinstance(v, Decimal) else v for k, v in out.items()}

    def by_dimension(self, dim: str, filters: Filters) -> list[dict]:
        check_dim(dim)
        w, p = where(filters)
        df = self.wh.df(f"""
            SELECT
                CAST({dim} AS VARCHAR)                  AS member,
                COUNT(*)                                AS n_applications,
                COUNT(*) FILTER (WHERE is_train)        AS n_labelled,
                AVG(target) FILTER (WHERE is_train)     AS default_rate,
                AVG(overdue_flag::INTEGER)              AS overdue_share,
                AVG(amt_credit)                         AS avg_credit,
                MEDIAN(debt_to_income)                  AS median_dti,
                SUM(amt_credit)                         AS total_credit
            FROM {APP_VIEW} {w}
            GROUP BY 1""", p)
        return records(_ordered(df, dim))

    def cross(self, dim_x: str, dim_y: str, filters: Filters) -> list[dict]:
        check_dim(dim_x), check_dim(dim_y)
        w, p = where(filters)
        return records(self.wh.df(f"""
            SELECT CAST({dim_x} AS VARCHAR) AS x, CAST({dim_y} AS VARCHAR) AS y,
                   COUNT(*) FILTER (WHERE is_train)    AS n_labelled,
                   AVG(target) FILTER (WHERE is_train) AS default_rate,
                   SUM(amt_credit)                     AS total_credit
            FROM {APP_VIEW} {w}
            GROUP BY ALL""", p))

    def score_curve(self, filters: Filters, bins: int = 20) -> list[dict]:
        """Default rate per external-score bin (labelled rows): the risk curve of EXT_SOURCE."""
        w, p = where(filters, "is_train AND ext_source_mean IS NOT NULL")
        n = int(bins)
        return records(self.wh.df(f"""
            SELECT
                LEAST(FLOOR(ext_source_mean * {n}), {n - 1})::INTEGER   AS bin,
                (LEAST(FLOOR(ext_source_mean * {n}), {n - 1}) + 0.5) / {n} AS score_mid,
                COUNT(*)                                                  AS n_labelled,
                AVG(target)                                               AS default_rate
            FROM {APP_VIEW} {w}
            GROUP BY ALL ORDER BY bin""", p))

    def dti_by_outcome(self, filters: Filters) -> list[dict]:
        """Five-number summary of debt-to-income for repaid vs defaulted loans (exact, no sample)."""
        w, p = where(filters, "is_train AND debt_to_income IS NOT NULL")
        return records(self.wh.df(f"""
            SELECT
                CASE WHEN target = 1 THEN 'Default' ELSE 'Repaid' END AS outcome,
                COUNT(*)                                 AS n,
                QUANTILE_CONT(debt_to_income, 0.05)      AS p05,
                QUANTILE_CONT(debt_to_income, 0.25)      AS p25,
                QUANTILE_CONT(debt_to_income, 0.50)      AS p50,
                QUANTILE_CONT(debt_to_income, 0.75)      AS p75,
                QUANTILE_CONT(debt_to_income, 0.95)      AS p95
            FROM {APP_VIEW} {w}
            GROUP BY 1 ORDER BY 1 DESC""", p))

    def hierarchy(self, levels: tuple[str, ...], filters: Filters) -> list[dict]:
        for d in levels:
            check_dim(d)
        cols = ", ".join(f"CAST({d} AS VARCHAR) AS {d}" for d in levels)
        w, p = where(filters)
        return records(self.wh.df(f"""
            SELECT {cols},
                   COUNT(*)                            AS n_applications,
                   AVG(target) FILTER (WHERE is_train) AS default_rate
            FROM {APP_VIEW} {w}
            GROUP BY ALL""", p))

    # ---- bureau fact (drill-across through dim_customer) --------------------------------------
    def _bureau_scope(self, filters: Filters) -> tuple[str, tuple]:
        if not filters:
            return "", ()
        w, p = where(filters)
        return f"WHERE customer_key IN (SELECT customer_key FROM {APP_VIEW} {w})", p

    def bureau_kpis(self, filters: Filters) -> dict:
        w, p = self._bureau_scope(filters)
        return row(self.wh.df(f"""
            SELECT
                COUNT(*)                                                       AS n_credits,
                COUNT(DISTINCT customer_key)                                   AS n_customers,
                AVG((credit_active = 'Active')::INTEGER)                       AS active_share,
                SUM(amt_credit_sum_debt) FILTER (WHERE credit_active = 'Active') AS active_debt,
                AVG((credit_day_overdue > 0)::INTEGER)                         AS overdue_share,
                MEDIAN(credit_duration_days)                                   AS median_duration_days
            FROM {BUREAU_VIEW} {w}""", p))

    def bureau_trend(self, filters: Filters, grain: str = "quarter") -> list[dict]:
        period = {
            "month": "year_month",
            "quarter": "year || '-Q' || quarter",
            "year": "CAST(year AS VARCHAR)",
        }[grain]
        w, p = self._bureau_scope(filters)
        return records(self.wh.df(f"""
            SELECT {period} AS period, credit_active,
                   COUNT(*) AS credits_opened,
                   SUM(amt_credit_sum) AS credit_sum
            FROM {BUREAU_VIEW} {w}
            GROUP BY ALL
            ORDER BY period""", p))

    def bureau_by_type(self, filters: Filters, top: int = 6) -> list[dict]:
        w, p = self._bureau_scope(filters)
        return records(self.wh.df(f"""
            WITH b AS (SELECT * FROM {BUREAU_VIEW} {w}),
            top_types AS (SELECT credit_type FROM b GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT {int(top)})
            SELECT CASE WHEN credit_type IN (SELECT credit_type FROM top_types) THEN credit_type
                        ELSE 'Other' END AS credit_type,
                   credit_active,
                   COUNT(*) AS n_credits
            FROM b
            GROUP BY ALL
            ORDER BY n_credits DESC""", p))

    # ---- OLAP explorer ------------------------------------------------------------------------
    @staticmethod
    def pivot_sql(row_dim: str, col_dim: str, measure: str, where_sql: str, source: str = APP_VIEW) -> str:
        check_dim(row_dim), check_dim(col_dim)
        if row_dim == col_dim:
            raise QueryError("Rows and columns must be different dimensions")
        if measure not in MEASURES:
            raise QueryError(f"Unknown measure '{measure}'")
        expr = MEASURES[measure][1]
        return f"""SELECT
    CASE WHEN GROUPING({row_dim}) = 1 THEN '{TOTAL}' ELSE CAST({row_dim} AS VARCHAR) END AS row_member,
    CASE WHEN GROUPING({col_dim}) = 1 THEN '{TOTAL}' ELSE CAST({col_dim} AS VARCHAR) END AS col_member,
    {expr} AS value,
    COUNT(*) AS n_applications
FROM {source} {where_sql}
GROUP BY CUBE ({row_dim}, {col_dim})"""

    def pivot(self, row_dim: str, col_dim: str, measure: str, filters: Filters) -> dict:
        w, p = where(filters)
        cells = records(self.wh.df(self.pivot_sql(row_dim, col_dim, measure, w), p))
        rows = self._axis(row_dim, {c["row_member"] for c in cells})
        cols = self._axis(col_dim, {c["col_member"] for c in cells})
        # shown in the OLAP page: the plain view instead of the rule_grade-rewriting subquery it runs on
        shown = f"-- rule_grade NULL (no rule violated) is shown as '{NO_RULE_HIT}'\n" \
                + self.pivot_sql(row_dim, col_dim, measure, w, source=WIDE_VIEW)
        return {"sql": shown, "params": list(p), "rows": rows, "cols": cols, "cells": cells}

    def _axis(self, dim: str, members: set[str]) -> list[str]:
        body = [m for m in members if m != TOTAL]
        known = category_order().get(dim, [])
        ordered = [m for m in known if m in body] + sorted(m for m in body if m not in known)
        return ordered + ([TOTAL] if TOTAL in members else [])

    @staticmethod
    def olap_files() -> list[str]:
        return [p.name for p in sorted((SQL_DIR / "olap").glob("*.sql"))]

    @classmethod
    def olap_sql(cls, name: str) -> str:
        if name not in cls.olap_files():  # never a user-supplied path
            raise QueryError(f"Unknown OLAP query '{name}'")
        return (SQL_DIR / "olap" / name).read_text(encoding="utf-8")

    def run_olap(self, name: str) -> dict:
        df = self.wh.df(self.olap_sql(name))
        return {"columns": [str(c) for c in df.columns], "rows": records(df)}

    # ---- DSS results (dw.fact_risk_assessment / dw.fact_rule_hit, written by `hcdw score`) ----
    def n_scored(self) -> int:
        return int(self.wh.df("SELECT COUNT(*) AS n FROM dw.fact_risk_assessment")["n"].iloc[0])

    def last_scored_at(self):
        """When `hcdw score` last wrote the assessments (naive local time, to the second); None if never."""
        return self.wh.dicts("SELECT MAX(scored_at) AS t FROM dw.fact_risk_assessment")[0]["t"]

    def application(self, application_id: int, columns: tuple[str, ...]) -> pd.DataFrame:
        """One application's row of the wide view (columns are trusted names from dss, never user input)."""
        return self.wh.df(f"SELECT {', '.join(columns)} FROM {WIDE_VIEW} WHERE application_id = ?",
                          (int(application_id),))

    def stored_assessment(self, application_id: int) -> dict:
        rows = self.wh.dicts("""
            SELECT pd, pd_source, score_grade, rule_grade, final_grade, grade_source, downgraded,
                   n_rule_hits, decision, max_credit_to_income, recommended_credit
            FROM dw.fact_risk_assessment WHERE application_id = ?""", (int(application_id),))
        return rows[0] if rows else {}

    def stored_rule_hits(self, application_id: int) -> list[dict]:
        return self.wh.dicts("""
            SELECT h.rule_code AS code, d.rule_group AS "group", d.label, h.rate, h.grade,
                   h.segment_key, h.rate_key
            FROM dw.fact_rule_hit h JOIN dw.dim_rule d USING (rule_code)
            WHERE h.application_id = ?""", (int(application_id),))

    def grades(self, scope: str) -> dict:
        """Grade distribution (score / rule / final) and decision mix of the stored assessments.

        Default rates use labelled rows only, so they are null for scope=test.
        """
        if scope not in GRADE_SCOPES:
            raise QueryError(f"scope must be one of {sorted(GRADE_SCOPES)}, got {scope!r}")
        base = f"""
            WITH a AS (
                SELECT a.*, f.target, f.is_train
                FROM dw.fact_risk_assessment a JOIN dw.fact_loan_application f USING (application_id)
                WHERE {GRADE_SCOPES[scope]})"""
        counts = self.wh.dicts(base + """
            SELECT 'score' AS grade_type, score_grade AS grade, COUNT(*) AS n,
                   AVG(target) FILTER (WHERE is_train) AS default_rate FROM a GROUP BY ALL
            UNION ALL
            SELECT 'rule', rule_grade, COUNT(*), AVG(target) FILTER (WHERE is_train) FROM a GROUP BY ALL
            UNION ALL
            SELECT 'final', final_grade, COUNT(*), AVG(target) FILTER (WHERE is_train) FROM a GROUP BY ALL""")
        totals = self.wh.dicts(base + """
            SELECT COUNT(*)                                        AS n,
                   AVG(downgraded::INTEGER)                        AS downgraded_share,
                   AVG((decision = 'approve')::INTEGER)            AS approval_rate,
                   AVG((decision = 'review')::INTEGER)             AS referral_rate,
                   AVG((decision = 'reject')::INTEGER)             AS reject_rate,
                   AVG(target) FILTER (WHERE is_train AND decision = 'approve') AS approved_default_rate
            FROM a""")[0]
        n = int(totals["n"])
        cells = {(r["grade_type"], r["grade"]): r for r in counts}
        distribution = {}
        for grade_type in ("score", "rule", "final"):
            grades = [*GRADES, None] if grade_type == "rule" else list(GRADES)
            distribution[grade_type] = [
                {"grade": g, "n": int(c["n"]) if c else 0, "share": (c["n"] / n if c else 0.0) if n else 0.0,
                 "default_rate": c["default_rate"] if c else None}
                for g in grades for c in [cells.get((grade_type, g))]
            ]
        return {"scope": scope, "n": n, "distribution": distribution,
                **{k: totals[k] for k in ("downgraded_share", "approval_rate", "referral_rate", "reject_rate",
                                          "approved_default_rate")}}


def _ordered(df: pd.DataFrame, dim: str) -> pd.DataFrame:
    if dim in CHRONOLOGICAL_DIMS:  # the zero-padded labels sort by time; NULL last
        return df.sort_values("member", na_position="last")
    order = category_order().get(dim)
    if not order:
        return df.sort_values("n_applications", ascending=False)
    rank = {v: i for i, v in enumerate(order)}
    return (df.assign(_r=df["member"].map(rank).fillna(len(order)))
              .sort_values(["_r", "member"]).drop(columns="_r"))
