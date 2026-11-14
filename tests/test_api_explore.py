"""Explorer endpoints (cohort conditions, cohort vs book metrics, application list) on the scored fixture."""
import json
import math

import duckdb
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("lightgbm")
pytest.importorskip("sklearn")

from hcdw.api.app import create_app  # noqa: E402
from hcdw.api.explore import FIELDS, OPS  # noqa: E402
from tests.conftest import N_CUSTOMERS  # noqa: E402
from tests.test_api_dss import client, model_dir, scored_path  # noqa: E402,F401  (shared fixtures)

GROUPS = {"Customer", "Product", "Credit history", "Risk & decision", "Outcome"}
METRIC_KEYS = ["n_applications", "n_historical", "n_new", "share_of_book", "total_credit", "auto_approve_rate",
               "manual_review_rate", "reject_rate", "remaining_risk", "blocked_risk", "manual_review",
               "outcome_by_decision"]


@pytest.fixture(scope="module")
def ref(scored_path):
    """The reference frame, derived in pandas from the raw view (independent of the API's SQL)."""
    con = duckdb.connect(str(scored_path), read_only=True)
    try:
        df = con.execute("SELECT * FROM mart.v_application_wide").df()
    finally:
        con.close()
    df["rule_grade"] = df["rule_grade"].astype(object).where(df["rule_grade"].notna(), "No rule hit")
    df["region_rating_client"] = df["region_rating_client"].astype(str)
    df["decision_label"] = df["decision"].map({"approve": "Approve", "review": "Manual review", "reject": "Reject"})
    df["loan_set"] = np.where(df["is_train"], "Historical", "New")
    defaulted = (df["target"] == 1).fillna(False).to_numpy(bool)
    df["outcome"] = np.select([~df["is_train"].to_numpy(bool), defaulted], ["Unknown", "Defaulted"], "Repaid")
    return df


def _metrics(client, conditions):
    r = client.post("/api/explore/metrics", json={"conditions": conditions})
    assert r.status_code == 200, r.text
    return r.json()


def _expected(df: pd.DataFrame) -> dict:
    hist = df[df["is_train"]]
    approved = hist[hist["decision"] == "approve"]
    review = hist[hist["decision"] == "review"]
    defaults = hist[hist["target"] == 1]
    decided = df["decision"].notna().sum()  # decisions: every scored row; outcomes: historical only

    def ratio(a, b):
        return a / b if b else None

    return {
        "n_applications": len(df), "n_historical": len(hist), "n_new": len(df) - len(hist),
        "total_credit": float(df["amt_credit"].sum()),
        "auto_approve_rate": ratio((df["decision"] == "approve").sum(), decided),
        "manual_review_rate": ratio((df["decision"] == "review").sum(), decided),
        "reject_rate": ratio((df["decision"] == "reject").sum(), decided),
        "outcome_by_decision": {
            d: {"good": int(((hist["decision"] == d) & (hist["target"] == 0)).sum()),
                "bad": int(((hist["decision"] == d) & (hist["target"] == 1)).sum())}
            for d in ("approve", "review", "reject")},
        "remaining_risk": {
            "approved_historical": len(approved), "approved_defaults": int((approved["target"] == 1).sum()),
            "approved_default_rate": ratio((approved["target"] == 1).sum(), len(approved)),
            # credit at risk of the approved loans: every scored row (PD exists for new ones too)
            "approved_expected_loss": float((df.loc[df["decision"] == "approve", "recommended_credit"]
                                             * df.loc[df["decision"] == "approve", "pd"]).sum()),
        },
        "blocked_risk": {
            "defaults_historical": len(defaults),
            "defaults_blocked": int(defaults["decision"].isin(["review", "reject"]).sum()),
            "blocked_share": ratio(defaults["decision"].isin(["review", "reject"]).sum(), len(defaults)),
        },
        "manual_review": {
            "review_historical": len(review), "review_repaid": int((review["target"] == 0).sum()),
            "review_repaid_share": ratio((review["target"] == 0).sum(), len(review)),
            "review_defaulted": int((review["target"] == 1).sum()),
            "review_defaulted_share": ratio((review["target"] == 1).sum(), len(review)),
        },
    }


def _assert_close(got, exp, path=""):
    if isinstance(exp, dict):
        for k, v in exp.items():
            _assert_close(got[k], v, f"{path}.{k}")
    elif exp is None:
        assert got is None, path
    else:
        assert got == pytest.approx(float(exp), rel=1e-9, abs=1e-9), path


# ---- fields catalog -----------------------------------------------------------------------------
def test_fields_catalog(client):
    fields = client.get("/api/explore/fields").json()["fields"]
    by_key = {f["key"]: f for f in fields}
    assert set(by_key) == set(FIELDS) and len(fields) == len(FIELDS)
    for f in fields:
        assert f["group"] in GROUPS and f["label"] and f["ops"] == list(OPS[f["type"]])
        if f["type"] == "category":
            assert isinstance(f["values"], list)
        elif f["type"] in ("number", "date"):
            assert f["min"] <= f["max"]
        else:
            assert f["type"] == "boolean" and "values" not in f and "min" not in f
    assert by_key["decision"]["values"] == ["Approve", "Manual review", "Reject"]
    assert by_key["loan_set"]["values"] == ["Historical", "New"]
    assert by_key["outcome"]["values"] == ["Repaid", "Defaulted", "Unknown"]
    assert by_key["region_rating_client"]["values"] == ["1", "2", "3"]
    assert by_key["rule_grade"]["values"][0] == "No rule hit"
    assert by_key["age_bucket"]["values"][0] == "18-24"  # category_order, not alphabetical
    assert by_key["has_current_overdue"]["type"] == "boolean" and by_key["pd"]["group"] == "Risk & decision"


# ---- operators vs a pandas reference ---------------------------------------------------------------
CASES = [
    ({"field": "income_type", "op": "is", "value": "Working"}, lambda d: d["income_type"] == "Working"),
    ({"field": "income_type", "op": "is_not", "value": "Working"}, lambda d: d["income_type"] != "Working"),
    ({"field": "age_bucket", "op": "is_one_of", "value": ["18-24", "25-34"]},
     lambda d: d["age_bucket"].isin(["18-24", "25-34"])),
    ({"field": "age_bucket", "op": "is_not_one_of", "value": ["18-24", "25-34"]},
     lambda d: ~d["age_bucket"].isin(["18-24", "25-34"])),
    ({"field": "region_rating_client", "op": "is", "value": "2"}, lambda d: d["region_rating_client"] == "2"),
    ({"field": "rule_grade", "op": "is", "value": "No rule hit"}, lambda d: d["rule_grade"] == "No rule hit"),
    ({"field": "decision", "op": "is", "value": "Manual review"}, lambda d: d["decision"] == "review"),
    ({"field": "loan_set", "op": "is", "value": "New"}, lambda d: ~d["is_train"]),
    ({"field": "outcome", "op": "is", "value": "Defaulted"}, lambda d: d["is_train"] & (d["target"] == 1)),
    ({"field": "occupation_type", "op": "exists"}, lambda d: d["occupation_type"].notna()),
    ({"field": "occupation_type", "op": "missing", "value": None}, lambda d: d["occupation_type"].isna()),
    ({"field": "amt_credit", "op": "gt", "value": 500000}, lambda d: d["amt_credit"] > 500000),
    ({"field": "amt_credit", "op": "gte", "value": 500000}, lambda d: d["amt_credit"] >= 500000),
    ({"field": "amt_credit", "op": "lt", "value": 500000}, lambda d: d["amt_credit"] < 500000),
    ({"field": "amt_credit", "op": "lte", "value": 500000}, lambda d: d["amt_credit"] <= 500000),
    ({"field": "amt_credit", "op": "between", "value": [200000, 600000]},
     lambda d: d["amt_credit"].between(200000, 600000)),
    ({"field": "cnt_children", "op": "eq", "value": 0}, lambda d: d["cnt_children"] == 0),
    ({"field": "cnt_children", "op": "neq", "value": 0}, lambda d: d["cnt_children"] != 0),
    ({"field": "late_payment_ratio", "op": "exists"}, lambda d: d["late_payment_ratio"].notna()),
    ({"field": "late_payment_ratio", "op": "missing"}, lambda d: d["late_payment_ratio"].isna()),
    ({"field": "late_payment_ratio", "op": "neq", "value": 0},  # "not equal" keeps missing values
     lambda d: d["late_payment_ratio"].isna() | (d["late_payment_ratio"] != 0)),
    ({"field": "pd", "op": "gte", "value": 0.13}, lambda d: d["pd"] >= 0.13),
    ({"field": "has_current_overdue", "op": "is", "value": True}, lambda d: d["has_current_overdue"]),
    ({"field": "downgraded", "op": "is", "value": False}, lambda d: ~d["downgraded"].astype(bool)),
    ({"field": "has_current_overdue", "op": "exists"}, lambda d: d["has_current_overdue"].notna()),
]


@pytest.mark.parametrize("condition,predicate", CASES, ids=[f"{c['field']}-{c['op']}" for c, _ in CASES])
def test_each_operator_filters_like_the_reference(client, ref, condition, predicate):
    expected = ref[predicate(ref).fillna(False).astype(bool)]
    m = _metrics(client, [condition])
    assert m["cohort"]["n_applications"] == len(expected)
    page = client.post("/api/explore/applications", json={"conditions": [condition], "page_size": 100,
                                                          "sort": {"field": "application_id", "dir": "asc"}}).json()
    assert page["total"] == len(expected)
    assert [r["application_id"] for r in page["rows"]] == sorted(expected["application_id"])[:100]


def test_conditions_are_anded(client, ref):
    conds = [{"field": "credit_history_status", "op": "is", "value": "Clean history"},
             {"field": "amt_credit", "op": "gt", "value": 300000}]
    m = _metrics(client, conds)
    expected = ref[(ref["credit_history_status"] == "Clean history") & (ref["amt_credit"] > 300000)]
    assert m["cohort"]["n_applications"] == len(expected) > 0


# ---- metric definitions --------------------------------------------------------------------------
def test_book_metrics_match_the_reference(client, ref):
    m = _metrics(client, [])
    assert list(m["book"]) == METRIC_KEYS and list(m["cohort"]) == METRIC_KEYS
    assert m["book"]["share_of_book"] is None and m["book"]["n_applications"] == N_CUSTOMERS
    _assert_close(m["book"], _expected(ref))
    assert m["cohort"]["share_of_book"] == 1.0  # no conditions: the cohort is the book


def test_cohort_metrics_match_the_reference(client, ref):
    conds = [{"field": "final_grade", "op": "is_one_of", "value": ["C", "D", "E"]}]
    m = _metrics(client, conds)
    part = ref[ref["final_grade"].isin(["C", "D", "E"])]
    _assert_close(m["cohort"], _expected(part))
    _assert_close(m["book"], _expected(ref))
    assert m["cohort"]["share_of_book"] == pytest.approx(len(part) / len(ref))
    # every historical default is either approved (remaining risk) or blocked
    book = m["book"]
    assert book["remaining_risk"]["approved_defaults"] + book["blocked_risk"]["defaults_blocked"] \
        == book["blocked_risk"]["defaults_historical"]
    mr = book["manual_review"]
    assert mr["review_repaid"] + mr["review_defaulted"] == mr["review_historical"] > 0


def test_empty_cohort(client):
    m = _metrics(client, [{"field": "amt_credit", "op": "gt", "value": 1e12}])
    c = m["cohort"]
    assert c["n_applications"] == c["n_historical"] == c["n_new"] == 0
    assert c["share_of_book"] == 0.0 and c["total_credit"] == 0.0
    assert c["auto_approve_rate"] is None and c["remaining_risk"]["approved_default_rate"] is None
    assert c["remaining_risk"]["approved_expected_loss"] == 0.0
    assert c["manual_review"]["review_repaid_share"] is None and c["blocked_risk"]["blocked_share"] is None
    page = client.post("/api/explore/applications",
                       json={"conditions": [{"field": "amt_credit", "op": "gt", "value": 1e12}]}).json()
    assert page["total"] == 0 and page["rows"] == []


# ---- validation ------------------------------------------------------------------------------------
@pytest.mark.parametrize("condition", [
    {"field": "nope", "op": "is", "value": "x"},
    {"field": "income_type; DROP TABLE dw.dim_customer", "op": "is", "value": "x"},
    {"field": "income_type", "op": "gt", "value": 1},
    {"field": "amt_credit", "op": "is", "value": 1},
    {"field": "has_current_overdue", "op": "is_one_of", "value": [True]},
    {"field": "income_type", "op": "is", "value": ["Working"]},
    {"field": "income_type", "op": "is", "value": 3},
    {"field": "income_type", "op": "is_one_of", "value": "Working"},
    {"field": "income_type", "op": "is_one_of", "value": []},
    {"field": "income_type", "op": "is_one_of", "value": ["x"] * 201},
    {"field": "income_type", "op": "is_one_of", "value": ["Working", 1]},
    {"field": "income_type", "op": "exists", "value": "Working"},
    {"field": "amt_credit", "op": "gt", "value": "100"},
    {"field": "amt_credit", "op": "gt", "value": True},
    {"field": "amt_credit", "op": "gt", "value": None},
    {"field": "amt_credit", "op": "between", "value": [5, 1]},
    {"field": "amt_credit", "op": "between", "value": [1, 2, 3]},
    {"field": "amt_credit", "op": "between", "value": 5},
    {"field": "has_current_overdue", "op": "is", "value": "true"},
])
def test_invalid_conditions_are_400(client, condition):
    for url in ("/api/explore/metrics", "/api/explore/applications"):
        r = client.post(url, json={"conditions": [condition]})
        assert r.status_code == 400, (url, r.text)
        assert r.json()["detail"]


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_non_finite_numbers_are_400(client, value):
    body = '{"conditions": [{"field": "amt_credit", "op": "gt", "value": %s}]}' % value
    r = client.post("/api/explore/metrics", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 400 and "finite" in json.loads(r.text)["detail"]


@pytest.mark.parametrize("body", [
    {"conditions": "income_type is Working"},
    {"conditions": [{"op": "is", "value": "x"}]},
    {"conditions": [{"field": "income_type", "op": "is", "value": "x", "extra": 1}]},
    {"conditions": [], "colums": ["pd"]},
    {"filters": []},
])
def test_malformed_bodies_are_422(client, body):
    assert client.post("/api/explore/metrics", json=body).status_code == 422


def test_injection_like_values_are_plain_values(client, ref):
    for value in ["x' OR 1=1 --", "Working'; DROP TABLE dw.fact_loan_application; --", "%", "*"]:
        m = _metrics(client, [{"field": "income_type", "op": "is", "value": value}])
        assert m["cohort"]["n_applications"] == 0
    assert _metrics(client, [])["book"]["n_applications"] == N_CUSTOMERS == len(ref)


def test_too_many_conditions_is_400(client):
    conds = [{"field": "amt_credit", "op": "gt", "value": 0}] * 51
    assert client.post("/api/explore/metrics", json={"conditions": conds}).status_code == 400


# ---- application list ----------------------------------------------------------------------------
BASE = ["application_id", "loan_set", "final_grade", "decision", "pd", "outcome"]


def test_applications_default_sort_columns_and_values(client, ref):
    r = client.post("/api/explore/applications", json={
        "conditions": [{"field": "loan_set", "op": "is", "value": "Historical"}],
        "columns": ["amt_credit", "rule_grade", "has_current_overdue", "region_rating_client", "pd"]}).json()
    assert r["total"] == int(ref["is_train"].sum()) and r["page"] == 1 and r["page_size"] == 50
    assert len(r["rows"]) == 50
    assert list(r["rows"][0]) == BASE + ["amt_credit", "rule_grade", "has_current_overdue", "region_rating_client"]
    pds = [x["pd"] for x in r["rows"]]
    assert pds == sorted(pds, reverse=True)  # default: pd desc
    by_id = ref.set_index("application_id")
    for x in r["rows"]:
        row = by_id.loc[x["application_id"]]
        assert x["loan_set"] == "Historical" and x["outcome"] == row_outcome(row)
        assert x["decision"] == {"approve": "Approve", "review": "Manual review", "reject": "Reject"}[row["decision"]]
        assert x["rule_grade"] == row["rule_grade"] and x["region_rating_client"] == row["region_rating_client"]
        assert isinstance(x["has_current_overdue"], bool) and x["amt_credit"] == pytest.approx(row["amt_credit"])
        assert all(not isinstance(v, float) or math.isfinite(v) for v in x.values())


def row_outcome(row) -> str:
    return "Defaulted" if row["target"] == 1 else "Repaid"


def test_applications_pagination_and_sort(client, ref):
    def page(n, size=40):
        return client.post("/api/explore/applications", json={
            "conditions": [], "sort": {"field": "amt_credit", "dir": "asc"}, "page": n, "page_size": size}).json()

    p1, p2, last = page(1), page(2), page(7)
    ids = [x["application_id"] for x in p1["rows"] + p2["rows"]]
    assert len(ids) == len(set(ids)) == 80
    expected = ref.sort_values(["amt_credit", "application_id"])["application_id"].tolist()
    assert ids == expected[:80]
    assert [x["application_id"] for x in last["rows"]] == expected[240:]
    assert page(99)["rows"] == [] and page(99)["total"] == N_CUSTOMERS


@pytest.mark.parametrize("body", [
    {"columns": [f"x{i}" for i in range(13)]},
    {"columns": ["amt_credit"] * 13},
    {"columns": ["not_a_field"]},
    {"columns": ["target"]},                 # internal column, not a field
    {"sort": {"field": "target", "dir": "asc"}},
    {"sort": {"field": "pd", "dir": "up"}},
    {"page": 0},
    {"page_size": 101},
    {"page_size": 0},
])
def test_invalid_application_requests_are_400(client, body):
    assert client.post("/api/explore/applications", json={"conditions": [], **body}).status_code == 400


# ---- before scoring --------------------------------------------------------------------------------
def test_explorer_before_scoring(built, model_dir):  # noqa: F811
    with TestClient(create_app(dw_path=built[0], model_dir=model_dir)) as c:
        assert c.get("/api/explore/fields").status_code == 200
        for url in ("/api/explore/metrics", "/api/explore/applications"):
            r = c.post(url, json={"conditions": []})
            assert r.status_code == 503 and "hcdw score" in r.json()["detail"]


def _assert_outcome_invariants(m: dict) -> None:
    obd = m["outcome_by_decision"]
    assert list(obd) == ["approve", "review", "reject"] and all(list(v) == ["good", "bad"] for v in obd.values())
    assert sum(v["good"] + v["bad"] for v in obd.values()) == m["n_historical"]
    assert obd["approve"]["bad"] == m["remaining_risk"]["approved_defaults"]
    assert obd["approve"]["good"] + obd["approve"]["bad"] == m["remaining_risk"]["approved_historical"]
    assert obd["review"]["good"] == m["manual_review"]["review_repaid"]
    assert obd["review"]["bad"] == m["manual_review"]["review_defaulted"]
    assert obd["review"]["bad"] + obd["reject"]["bad"] == m["blocked_risk"]["defaults_blocked"]


@pytest.mark.parametrize("conditions", [
    [], [{"field": "credit_history_status", "op": "is", "value": "No bureau"}],
    [{"field": "amt_credit", "op": "gt", "value": 1e12}],
])
def test_outcome_by_decision_invariants(client, conditions):
    m = _metrics(client, conditions)
    for part in (m["cohort"], m["book"]):
        _assert_outcome_invariants(part)


def test_decision_rates_cover_every_scored_row(client, ref):
    """B4: a cohort of new applications has decisions (no outcomes), so its rates are not null."""
    new = _metrics(client, [{"field": "loan_set", "op": "is", "value": "New"}])["cohort"]
    part = ref[~ref["is_train"]]
    assert new["n_historical"] == 0 and new["n_new"] == len(part) > 0
    assert new["auto_approve_rate"] == pytest.approx((part["decision"] == "approve").mean())
    assert new["manual_review_rate"] == pytest.approx((part["decision"] == "review").mean())
    assert new["reject_rate"] == pytest.approx((part["decision"] == "reject").mean())
    # outcome-based figures stay historical: nothing known yet for new applications
    assert new["remaining_risk"]["approved_historical"] == 0 and new["remaining_risk"]["approved_default_rate"] is None
    assert sum(v["good"] + v["bad"] for v in new["outcome_by_decision"].values()) == 0
    # the book's rates cover every scored row, Overview's (kpis/summary) the historical ones only
    book = _metrics(client, [])["book"]
    assert book["auto_approve_rate"] == pytest.approx((ref["decision"] == "approve").mean())
    overview = client.get("/api/kpis/summary").json()  # Overview uses the same all-rows scope
    assert overview["approval_rate"] == pytest.approx(book["auto_approve_rate"], abs=1e-12)



# ---- huge integers and strict body types --------------------------------------------------------
@pytest.mark.parametrize("value", ["1" + "0" * 400, "-1" + "0" * 400, "[0, 1%s]" % ("0" * 400)])
def test_huge_integers_in_conditions_are_400(client, value):
    op = "between" if value.startswith("[") else "gt"
    body = '{"conditions": [{"field": "amt_income", "op": "%s", "value": %s}]}' % (op, value)
    for url in ("/api/explore/metrics", "/api/explore/applications"):
        r = client.post(url, content=body, headers={"content-type": "application/json"})
        assert r.status_code == 400 and "finite" in r.json()["detail"], url


@pytest.mark.parametrize("page", [10**6 + 1, 10**18, 10**30])
def test_huge_pages_are_400(client, page):
    r = client.post("/api/explore/applications", json={"conditions": [], "page": page})
    assert r.status_code == 400 and "page" in r.json()["detail"]


def test_largest_allowed_page_is_empty_not_an_error(client):
    r = client.post("/api/explore/applications", json={"conditions": [], "page": 10**6, "page_size": 100})
    assert r.status_code == 200 and r.json()["rows"] == [] and r.json()["total"] == N_CUSTOMERS


@pytest.mark.parametrize("body", [
    {"page_size": True}, {"page": True}, {"page": "2"}, {"page_size": 10.0},
    {"sort": {"field": "pd", "dir": True}}, {"columns": [1]},
    {"conditions": [{"field": 1, "op": "is", "value": "x"}]},
])
def test_wrong_body_types_are_422(client, body):
    assert client.post("/api/explore/applications", json={"conditions": [], **body}).status_code == 422



# ---- application / outcome dates -----------------------------------------------------------------------
DATE_OPS = ["eq", "neq", "gt", "gte", "lt", "lte", "between", "exists", "missing"]


def test_date_fields_in_the_catalog(client, ref):
    by_key = {f["key"]: f for f in client.get("/api/explore/fields").json()["fields"]}
    for key in ("application_date", "outcome_date"):
        f = by_key[key]
        assert f["type"] == "date" and f["ops"] == DATE_OPS and "values" not in f
        assert len(f["min"]) == len(f["max"]) == 10 and f["min"] <= f["max"]  # ISO dates
    assert by_key["application_date"]["min"] == ref["application_date"].min().date().isoformat()
    assert by_key["months_to_outcome"]["type"] == "number" and by_key["months_to_outcome"]["group"] == "Outcome"
    for key in ("application_month", "application_quarter", "application_year"):
        f = by_key[key]
        assert f["type"] == "category" and f["values"] == sorted(f["values"]) and len(f["values"]) > 1
    assert by_key["application_year"]["values"] == sorted(ref["application_year"].astype(str).unique())


def _day(s: str):
    return pd.Timestamp(s)


DATE_CASES = [
    ({"field": "application_date", "op": "gte", "value": "2016-06-01"},
     lambda d: d["application_date"] >= _day("2016-06-01")),
    ({"field": "application_date", "op": "lt", "value": "2016-06-01"},
     lambda d: d["application_date"] < _day("2016-06-01")),
    ({"field": "application_date", "op": "between", "value": ["2016-01-01", "2016-12-31"]},
     lambda d: d["application_date"].between(_day("2016-01-01"), _day("2016-12-31"))),
    ({"field": "application_date", "op": "eq", "value": "2016-03-15"},
     lambda d: d["application_date"] == _day("2016-03-15")),
    ({"field": "outcome_date", "op": "missing"}, lambda d: d["outcome_date"].isna()),
    ({"field": "outcome_date", "op": "lte", "value": "2017-01-31"}, lambda d: d["outcome_date"] <= _day("2017-01-31")),
    ({"field": "months_to_outcome", "op": "lte", "value": 3}, lambda d: d["months_to_outcome"] <= 3),
    ({"field": "months_to_outcome", "op": "eq", "value": 12}, lambda d: d["months_to_outcome"] == 12),
    ({"field": "application_year", "op": "is", "value": "2016"}, lambda d: d["application_year"] == 2016),
    ({"field": "application_quarter", "op": "is_one_of", "value": ["2016-Q1", "2016-Q2"]},
     lambda d: d["application_quarter"].isin(["2016-Q1", "2016-Q2"])),
    ({"field": "application_month", "op": "is_not", "value": "2016-01"}, lambda d: d["application_month"] != "2016-01"),
]


@pytest.mark.parametrize("condition,predicate", DATE_CASES, ids=[f"{c['field']}-{c['op']}" for c, _ in DATE_CASES])
def test_date_conditions_filter_like_the_reference(client, ref, condition, predicate):
    expected = ref[predicate(ref).fillna(False).astype(bool)]
    assert _metrics(client, [condition])["cohort"]["n_applications"] == len(expected)


def test_date_condition_cases_are_not_vacuous(ref):
    assert ref["outcome_date"].isna().sum() == (~ref["is_train"]).sum() > 0  # new loans: no outcome yet
    assert ((ref["application_date"] >= _day("2016-06-01")) & (ref["application_date"] < _day("2017-06-01"))).any()
    assert (ref["months_to_outcome"] <= 3).any() and (ref["months_to_outcome"] == 12).any()


@pytest.mark.parametrize("condition", [
    {"field": "application_date", "op": "gte", "value": "2016-13-01"},
    {"field": "application_date", "op": "gte", "value": "2016-02-30"},
    {"field": "application_date", "op": "gte", "value": "2016/01/01"},
    {"field": "application_date", "op": "gte", "value": "20160101"},
    {"field": "application_date", "op": "gte", "value": "2016-01-01T00:00:00"},
    {"field": "application_date", "op": "gte", "value": 20160101},
    {"field": "application_date", "op": "between", "value": ["2017-01-01", "2016-01-01"]},
    {"field": "application_date", "op": "between", "value": ["2016-01-01"]},
    {"field": "application_date", "op": "is", "value": "2016-01-01"},
    {"field": "outcome_date", "op": "lt", "value": "2016-01-01'; DROP TABLE dw.dim_time; --"},
])
def test_invalid_date_conditions_are_400(client, condition):
    r = client.post("/api/explore/metrics", json={"conditions": [condition]})
    assert r.status_code == 400 and r.json()["detail"]


def test_date_columns_are_selectable_and_sortable(client, ref):
    cols = ["application_date", "outcome_date", "months_to_outcome", "application_month", "application_quarter",
            "application_year"]
    r = client.post("/api/explore/applications", json={
        "conditions": [], "columns": cols, "sort": {"field": "application_date", "dir": "asc"},
        "page_size": 100}).json()
    assert list(r["rows"][0]) == BASE + cols
    expected = ref.sort_values(["application_date", "application_id"])["application_id"].tolist()[:100]
    assert [x["application_id"] for x in r["rows"]] == expected
    by_id = ref.set_index("application_id")
    for x in r["rows"]:
        row = by_id.loc[x["application_id"]]
        assert x["application_date"] == row["application_date"].date().isoformat()
        assert x["application_month"] == x["application_date"][:7]
        assert x["application_year"] == x["application_date"][:4]
        if pd.isna(row["outcome_date"]):
            assert x["outcome_date"] is None and x["months_to_outcome"] is None
        else:
            assert x["outcome_date"] == row["outcome_date"].date().isoformat()
            assert x["months_to_outcome"] == int(row["months_to_outcome"])
    desc = client.post("/api/explore/applications", json={
        "conditions": [], "sort": {"field": "outcome_date", "dir": "desc"}, "columns": ["outcome_date"],
        "page_size": 100, "page": 3}).json()
    dates = [x["outcome_date"] for x in desc["rows"]]
    present = [d for d in dates if d is not None]
    assert present == sorted(present, reverse=True) and dates[len(present):] == [None] * (len(dates) - len(present))
