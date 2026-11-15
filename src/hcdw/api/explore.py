"""Explorer: build a cohort with Kibana-style conditions and compare it with the whole book.

Answers "where is risk concentrated, and which cohorts should the policy handle differently?":
- remaining risk: defaults among auto-approved historical loans;
- manual-review load: historical loans sent to manual review whose outcome says they could have been
  decided automatically (repaid -> auto-approve, defaulted -> auto-reject; review is assumed right).

Safety: field keys and operators are whitelisted before they reach SQL; every value is a bound parameter.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from dss.features import FEATURE_LABELS
from hcdw.api.queries import (CREDIT_HISTORY_STATUSES, DIMENSIONS, NO_RULE_HIT, WIDE_VIEW, QueryError, Warehouse,
                              category_order)
from hcdw.config import GRADES, get_settings

MAX_LIST = 200
MAX_CONDITIONS = 50
MAX_COLUMNS = 12
MAX_PAGE_SIZE = 100
MAX_PAGE = 1_000_000          # keeps OFFSET far inside BIGINT
GROUPS = ("Customer", "Product", "Credit history", "Risk & decision", "Outcome")
OPS = {
    "category": ("is", "is_not", "is_one_of", "is_not_one_of", "exists", "missing"),
    "number": ("eq", "neq", "gt", "gte", "lt", "lte", "between", "exists", "missing"),
    "boolean": ("is", "exists", "missing"),
    # same range operators as numbers; values are ISO dates "YYYY-MM-DD"
    "date": ("eq", "neq", "gt", "gte", "lt", "lte", "between", "exists", "missing"),
}
DECISION_CODES = ("approve", "review", "reject")
LOAN_SETS = ("Historical", "New")
OUTCOMES = ("Repaid", "Defaulted", "Unknown")
ROW_BASE = ("application_id", "loan_set", "final_grade", "decision", "pd", "outcome")


class ExploreError(QueryError):
    """An invalid condition, column or sort (the API answers 400)."""


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str
    group: str
    type: str                                 # category | number | boolean | date
    sql: str                                  # expression over mart.v_application_wide (trusted)
    domain: tuple[str, ...] | None = None     # fixed member list (else: the data's members)


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _fields() -> dict[str, FieldDef]:
    labels = get_settings().dss["decision_labels"]
    decision_sql = "CASE decision " + " ".join(f"WHEN {_q(c)} THEN {_q(labels[c])}" for c in DECISION_CODES) + " END"
    dim, feat = DIMENSIONS, FEATURE_LABELS
    cat, num, boo = "category", "number", "boolean"
    rows = [
        # Customer
        ("age_bucket", dim["age_bucket"], "Customer", cat, "age_bucket", None),
        ("income_bracket", dim["income_bracket"], "Customer", cat, "income_bracket", None),
        ("income_type", dim["income_type"], "Customer", cat, "income_type", None),
        ("occupation_type", dim["occupation_type"], "Customer", cat, "occupation_type", None),
        ("education_type", dim["education_type"], "Customer", cat, "education_type", None),
        ("family_status", dim["family_status"], "Customer", cat, "family_status", None),
        ("gender", dim["gender"], "Customer", cat, "gender", None),
        ("housing_type", dim["housing_type"], "Customer", cat, "housing_type", None),
        ("region_rating_client", feat["region_rating_client"], "Customer", cat,
         "CAST(region_rating_client AS VARCHAR)", ("1", "2", "3")),
        ("population_density_band", dim["population_density_band"], "Customer", cat, "population_density_band", None),
        ("age_years", feat["age_years"], "Customer", num, "age_years", None),
        ("amt_income", feat["amt_income"], "Customer", num, "amt_income", None),
        ("years_employed", feat["years_employed"], "Customer", num, "years_employed", None),
        ("cnt_children", feat["cnt_children"], "Customer", num, "cnt_children", None),
        # Product
        ("contract_type", dim["contract_type"], "Product", cat, "contract_type", None),
        ("application_date", "Application date", "Product", "date", "application_date", None),
        ("application_month", dim["application_month"], "Product", cat, "application_month", None),
        ("application_quarter", dim["application_quarter"], "Product", cat, "application_quarter", None),
        ("application_year", dim["application_year"], "Product", cat, "CAST(application_year AS VARCHAR)", None),
        ("appr_weekday", dim["appr_weekday"], "Product", cat, "appr_weekday", None),
        ("amt_credit", feat["amt_credit"], "Product", num, "amt_credit", None),
        ("amt_annuity", feat["amt_annuity"], "Product", num, "amt_annuity", None),
        ("credit_to_income", feat["credit_to_income"], "Product", num, "credit_to_income", None),
        ("debt_to_income", feat["debt_to_income"], "Product", num, "debt_to_income", None),
        # Credit history
        ("credit_history_status", dim["credit_history_status"], "Credit history", cat, "credit_history_status",
         CREDIT_HISTORY_STATUSES),
        ("bureau_active_bucket", dim["bureau_active_bucket"], "Credit history", cat, "bureau_active_bucket", None),
        ("max_dpd_bucket", dim["max_dpd_bucket"], "Credit history", cat, "max_dpd_bucket", None),
        ("prev_approval_bucket", dim["prev_approval_bucket"], "Credit history", cat, "prev_approval_bucket", None),
        ("ext_source_mean", feat["ext_source_mean"], "Credit history", num, "ext_source_mean", None),
        ("cnt_bureau_active", feat["cnt_bureau_active"], "Credit history", num, "cnt_bureau_active", None),
        ("max_dpd_days", feat["max_dpd_days"], "Credit history", num, "max_dpd_days", None),
        ("late_payment_ratio", feat["late_payment_ratio"], "Credit history", num, "late_payment_ratio", None),
        ("prev_approval_rate", feat["prev_approval_rate"], "Credit history", num, "prev_approval_rate", None),
        ("has_current_overdue", "Currently overdue in the Credit Bureau", "Credit history", boo,
         "has_current_overdue", None),
        # Risk & decision
        ("score_grade", dim["score_grade"], "Risk & decision", cat, "score_grade", GRADES),
        ("rule_grade", dim["rule_grade"], "Risk & decision", cat,
         f"CASE WHEN final_grade IS NOT NULL THEN COALESCE(rule_grade, {_q(NO_RULE_HIT)}) END",
         (NO_RULE_HIT, *GRADES)),
        ("final_grade", dim["final_grade"], "Risk & decision", cat, "final_grade", GRADES),
        ("decision", "Decision", "Risk & decision", cat, decision_sql, tuple(labels[c] for c in DECISION_CODES)),
        ("pd", "Probability of default", "Risk & decision", num, "pd", None),
        ("recommended_credit", "Recommended credit", "Risk & decision", num, "recommended_credit", None),
        ("downgraded", "Downgraded by rules", "Risk & decision", boo, "downgraded", None),
        # Outcome
        ("loan_set", "Loan set", "Outcome", cat,
         f"CASE WHEN is_train THEN {_q(LOAN_SETS[0])} ELSE {_q(LOAN_SETS[1])} END", LOAN_SETS),
        ("outcome", "Outcome", "Outcome", cat,
         "CASE WHEN NOT is_train OR target IS NULL THEN 'Unknown' WHEN target = 1 THEN 'Defaulted' ELSE 'Repaid' END",
         OUTCOMES),
        ("outcome_date", "Outcome date", "Outcome", "date", "outcome_date", None),
        ("months_to_outcome", "Months to outcome", "Outcome", num, "months_to_outcome", None),
    ]
    return {k: FieldDef(k, label, group, typ, sql, domain) for k, label, group, typ, sql, domain in rows}


FIELDS = _fields()


def source_sql() -> str:
    """One row per application with every field under its key, plus the internal columns the metrics need."""
    cols = ", ".join(f"{f.sql} AS {f.key}" for f in FIELDS.values())
    return (f"SELECT application_id, is_train, target, decision AS decision_code, {cols} "
            f"FROM {WIDE_VIEW}")


# ---- request bodies (shape only: 422; values are checked by `compile_conditions`: 400) ------------------
class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    field: str
    op: str
    value: Any = None


class CohortQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    conditions: list[Condition] = Field(default_factory=list)


class SortSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    field: str = "pd"
    dir: str = "desc"


class ApplicationsQuery(CohortQuery):
    columns: list[str] = Field(default_factory=list)
    sort: SortSpec = Field(default_factory=SortSpec)
    page: int = 1
    page_size: int = 50


# ---- condition compiler -------------------------------------------------------------------------------
def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _number(field: str, op: str, v: Any) -> float:
    if not _is_number(v):
        raise ExploreError(f"{field} {op}: value must be a number, got {_show(v)}.")
    try:
        x = float(v)  # an int beyond the float range (10**400) overflows
    except OverflowError:
        x = math.inf
    if not math.isfinite(x):
        raise ExploreError(f"{field} {op}: value must be a finite number.")
    return x


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _iso_date(field: str, op: str, v: Any) -> date:
    """A calendar date given as "YYYY-MM-DD" exactly (bound as a DATE parameter)."""
    if not isinstance(v, str) or not _ISO_DATE.fullmatch(v):
        raise ExploreError(f"{field} {op}: value must be a date as YYYY-MM-DD, got {_show(v)}.")
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise ExploreError(f"{field} {op}: {_show(v)} is not a valid date.") from None


def _string_list(field: str, op: str, v: Any) -> list[str]:
    if not isinstance(v, list) or not v:
        raise ExploreError(f"{field} {op}: value must be a non-empty list of strings.")
    if len(v) > MAX_LIST:
        raise ExploreError(f"{field} {op}: at most {MAX_LIST} values, got {len(v)}.")
    if not all(isinstance(x, str) for x in v):
        raise ExploreError(f"{field} {op}: every value must be a string.")
    return list(dict.fromkeys(v))


def _show(v: Any) -> str:
    text = repr(v)
    return text if len(text) <= 60 else text[:57] + "..."


def compile_condition(c: Condition) -> tuple[str, list]:
    """(SQL predicate over the field's column, bound parameters); ExploreError on anything invalid."""
    f = FIELDS.get(c.field)
    if f is None:
        raise ExploreError(f"Unknown field {_show(c.field)}.")
    if c.op not in OPS[f.type]:
        raise ExploreError(f"Operator {_show(c.op)} does not apply to {f.type} field {f.key}. "
                           f"Use one of: {', '.join(OPS[f.type])}.")
    col, op, v = f.key, c.op, c.value
    if op in ("exists", "missing"):
        if v is not None:
            raise ExploreError(f"{col} {op}: value must be null.")
        return f"{col} IS {'NOT ' if op == 'exists' else ''}NULL", []
    if f.type == "category":
        if op in ("is", "is_not"):
            if not isinstance(v, str):
                raise ExploreError(f"{col} {op}: value must be a string, got {_show(v)}.")
            # "is not" keeps rows where the field is missing (Kibana semantics)
            return (f"{col} = ?" if op == "is" else f"{col} IS DISTINCT FROM ?"), [v]
        values = _string_list(col, op, v)
        marks = ", ".join("?" for _ in values)
        return (f"{col} IN ({marks})" if op == "is_one_of" else f"({col} IS NULL OR {col} NOT IN ({marks}))"), values
    if f.type == "boolean":
        if not isinstance(v, bool):
            raise ExploreError(f"{col} {op}: value must be true or false, got {_show(v)}.")
        return f"{col} = ?", [v]
    parse = _iso_date if f.type == "date" else _number
    if op == "between":
        if not isinstance(v, list) or len(v) != 2:
            raise ExploreError(f"{col} between: value must be [low, high].")
        lo, hi = parse(col, op, v[0]), parse(col, op, v[1])
        if lo > hi:
            raise ExploreError(f"{col} between: low ({lo}) must not exceed high ({hi}).")
        return f"{col} BETWEEN ? AND ?", [lo, hi]
    x = parse(col, op, v)
    sql = {"eq": f"{col} = ?", "neq": f"{col} IS DISTINCT FROM ?", "gt": f"{col} > ?", "gte": f"{col} >= ?",
           "lt": f"{col} < ?", "lte": f"{col} <= ?"}[op]
    return sql, [x]


def compile_conditions(conditions: list[Condition]) -> tuple[str, list]:
    """AND of every condition as a WHERE clause ('' when there is none)."""
    if len(conditions) > MAX_CONDITIONS:
        raise ExploreError(f"At most {MAX_CONDITIONS} conditions, got {len(conditions)}.")
    parts, params = [], []
    for c in conditions:
        sql, p = compile_condition(c)
        parts.append(sql)
        params += p
    return ("WHERE " + " AND ".join(parts) if parts else ""), params


def _finite(v: Any) -> Any:
    return None if isinstance(v, float) and not math.isfinite(v) else v


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


class Explorer:
    def __init__(self, wh: Warehouse) -> None:
        self.wh = wh
        self.fields = lru_cache(maxsize=1)(self.fields)
        self._book = lru_cache(maxsize=1)(self._book)

    # ---- GET /api/explore/fields ----------------------------------------------------------------------
    def fields(self) -> dict:
        src = source_sql()
        numbers = [f.key for f in FIELDS.values() if f.type in ("number", "date")]
        ranges = self.wh.dicts(f"SELECT {', '.join(f'MIN({k}) AS min_{k}, MAX({k}) AS max_{k}' for k in numbers)} "
                               f"FROM ({src})")[0]
        order = category_order()
        out = []
        for f in FIELDS.values():
            item: dict[str, Any] = {"key": f.key, "label": f.label, "group": f.group, "type": f.type,
                                    "ops": list(OPS[f.type])}
            if f.type == "category":
                if f.domain is not None and f.key not in DIMENSIONS:
                    item["values"] = list(f.domain)
                else:
                    # a dashboard dimension lists exactly the members /api/meta and the cube show, so an
                    # Analytics drill-through always maps to Explorer values; the domain only sets the order
                    seen = [str(r["v"]) for r in self.wh.dicts(
                        f"SELECT DISTINCT {f.key} AS v FROM ({src}) WHERE {f.key} IS NOT NULL")]
                    known = list(f.domain) if f.domain is not None else order.get(f.key, [])
                    item["values"] = [v for v in known if v in seen] + sorted(v for v in seen if v not in known)
            elif f.type in ("number", "date"):  # dates: ISO strings
                item["min"] = _finite(_plain(ranges[f"min_{f.key}"]))
                item["max"] = _finite(_plain(ranges[f"max_{f.key}"]))
            out.append(item)
        return {"fields": out}

    # ---- POST /api/explore/metrics --------------------------------------------------------------------
    def metrics(self, q: CohortQuery) -> dict:
        where, params = compile_conditions(q.conditions)
        book = self._book()
        cohort = self._metrics(where, tuple(params))
        cohort["share_of_book"] = _ratio(cohort["n_applications"], book["n_applications"])
        return {"cohort": cohort, "book": book}

    def _book(self) -> dict:
        return self._metrics("", ())

    def _metrics(self, where: str, params: tuple) -> dict:
        r = self.wh.dicts(f"""
            WITH c AS (SELECT * FROM ({source_sql()}) {where})
            SELECT
                COUNT(*)                                                               AS n,
                COUNT(*) FILTER (WHERE is_train)                                       AS n_hist,
                COUNT(*) FILTER (WHERE NOT is_train)                                   AS n_new,
                COALESCE(SUM(amt_credit), 0)                                           AS total_credit,
                -- decision mix over every scored row (historical and new); outcome counts stay historical
                COUNT(decision_code)                                                   AS n_decided,
                COUNT(*) FILTER (WHERE decision_code = 'approve')                      AS n_approve,
                COUNT(*) FILTER (WHERE decision_code = 'review')                       AS n_review,
                COUNT(*) FILTER (WHERE decision_code = 'reject')                       AS n_reject,
                COUNT(*) FILTER (WHERE is_train AND decision_code = 'approve' AND target = 0) AS approve_good,
                COUNT(*) FILTER (WHERE is_train AND decision_code = 'reject' AND target = 0) AS reject_good,
                COUNT(*) FILTER (WHERE is_train AND decision_code = 'reject' AND target = 1) AS reject_bad,
                COUNT(*) FILTER (WHERE is_train AND decision_code = 'approve')         AS approved_hist,
                COUNT(*) FILTER (WHERE is_train AND decision_code = 'approve' AND target = 1) AS approved_def,
                COALESCE(SUM(recommended_credit * pd)
                         FILTER (WHERE decision_code = 'approve'), 0)                  AS approved_el,
                COUNT(*) FILTER (WHERE is_train AND target = 1)                        AS defaults_hist,
                COUNT(*) FILTER (WHERE is_train AND target = 1
                                 AND decision_code IN ('review', 'reject'))            AS defaults_blocked,
                COUNT(*) FILTER (WHERE is_train AND decision_code = 'review')          AS review_hist,
                COUNT(*) FILTER (WHERE is_train AND decision_code = 'review' AND target = 0) AS review_repaid,
                COUNT(*) FILTER (WHERE is_train AND decision_code = 'review' AND target = 1) AS review_defaulted
            FROM c""", params)[0]
        n = r
        return {
            "n_applications": n["n"],
            "n_historical": n["n_hist"],
            "n_new": n["n_new"],
            "share_of_book": None,
            "total_credit": float(n["total_credit"]),
            "auto_approve_rate": _ratio(n["n_approve"], n["n_decided"]),
            "manual_review_rate": _ratio(n["n_review"], n["n_decided"]),
            "reject_rate": _ratio(n["n_reject"], n["n_decided"]),
            "remaining_risk": {
                "approved_historical": n["approved_hist"],
                "approved_defaults": n["approved_def"],
                "approved_default_rate": _ratio(n["approved_def"], n["approved_hist"]),
                "approved_expected_loss": float(n["approved_el"]),
            },
            "blocked_risk": {
                "defaults_historical": n["defaults_hist"],
                "defaults_blocked": n["defaults_blocked"],
                "blocked_share": _ratio(n["defaults_blocked"], n["defaults_hist"]),
            },
            "manual_review": {
                "review_historical": n["review_hist"],
                "review_repaid": n["review_repaid"],
                "review_repaid_share": _ratio(n["review_repaid"], n["review_hist"]),
                "review_defaulted": n["review_defaulted"],
                "review_defaulted_share": _ratio(n["review_defaulted"], n["review_hist"]),
            },
            # historical loans by decision and outcome (good = repaid, bad = defaulted)
            "outcome_by_decision": {
                "approve": {"good": n["approve_good"], "bad": n["approved_def"]},
                "review": {"good": n["review_repaid"], "bad": n["review_defaulted"]},
                "reject": {"good": n["reject_good"], "bad": n["reject_bad"]},
            },
        }

    # ---- POST /api/explore/applications ---------------------------------------------------------------
    def applications(self, q: ApplicationsQuery) -> dict:
        where, params = compile_conditions(q.conditions)
        if len(q.columns) > MAX_COLUMNS:
            raise ExploreError(f"At most {MAX_COLUMNS} columns, got {len(q.columns)}.")
        unknown = [c for c in q.columns if c not in FIELDS]
        if unknown:
            raise ExploreError(f"Unknown column(s): {', '.join(_show(c) for c in unknown)}.")
        if q.sort.field not in FIELDS and q.sort.field != "application_id":
            raise ExploreError(f"Unknown sort field {_show(q.sort.field)}.")
        if q.sort.dir not in ("asc", "desc"):
            raise ExploreError("sort.dir must be 'asc' or 'desc'.")
        if not 1 <= q.page <= MAX_PAGE:
            raise ExploreError(f"page must be between 1 and {MAX_PAGE}.")
        if not 1 <= q.page_size <= MAX_PAGE_SIZE:
            raise ExploreError(f"page_size must be between 1 and {MAX_PAGE_SIZE}.")

        cols = list(dict.fromkeys([*ROW_BASE, *q.columns]))
        cohort = f"(SELECT * FROM ({source_sql()}) {where})"
        total = int(self.wh.dicts(f"SELECT COUNT(*) AS n FROM {cohort}", tuple(params))[0]["n"])
        # application_id breaks ties, so pages never overlap or skip rows
        order = f"{q.sort.field} {q.sort.dir.upper()} NULLS LAST, application_id ASC"
        rows = self.wh.dicts(
            f"SELECT {', '.join(cols)} FROM {cohort} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, q.page_size, (q.page - 1) * q.page_size))
        return {"total": total, "page": q.page, "page_size": q.page_size,
                "rows": [{k: _finite(_plain(v)) for k, v in r.items()} for r in rows]}


def _plain(v: Any) -> Any:
    """Dates as ISO strings; Decimal and other DB scalars as JSON-native numbers."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, date):
        return v.isoformat()
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)
