"""DSS endpoints (docs/dss_design.md 7) on a scored copy of the fixture warehouse, model trained into tmp."""
import dataclasses
import json
import math
import os
import shutil
import time
from datetime import datetime, timedelta

import duckdb
import pandas as pd
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("lightgbm")
pytest.importorskip("sklearn")

from dss.score import score_all  # noqa: E402
from dss.train import train  # noqa: E402
from hcdw.api import app as api  # noqa: E402
from hcdw.api.app import SIMULATOR_FIELDS, create_app  # noqa: E402
from hcdw.config import GRADES, get_settings  # noqa: E402
from hcdw.etl.load import build_marts  # noqa: E402
from tests.conftest import N_CUSTOMERS  # noqa: E402

ASSESSMENT_KEYS = [
    "application_id", "is_train", "target", "pd", "pd_source", "score_grade", "rule_grade", "final_grade",
    "grade_source", "downgraded", "summary", "decision", "decision_label", "max_credit_to_income", "amt_income",
    "amt_credit", "credit_to_income", "recommended_credit", "shap", "rule_hits", "missing_fields",
    "rules_not_evaluated", "profile",
]
PROFILE_KEYS = [
    "age_years", "income_type", "occupation_type", "education_type", "family_status", "housing_type",
    "contract_type", "region_rating_client", "credit_history_status", "ext_source_mean", "years_employed",
    "cnt_children", "cnt_fam_members", "amt_income", "amt_credit", "amt_annuity", "amt_goods_price",
    "cnt_bureau_active", "max_dpd_days", "late_payment_ratio", "prev_approval_rate", "has_current_overdue",
    "application_date", "outcome_date", "months_to_outcome",
]
PROFILE_DATES = ("application_date", "outcome_date")


@pytest.fixture(scope="module")
def model_dir(built, tmp_path_factory):
    d = tmp_path_factory.mktemp("api_models")
    con = duckdb.connect(str(built[0]), read_only=True)
    try:
        train(con, model_dir=d)
    finally:
        con.close()
    return d


@pytest.fixture(scope="module")
def scored_path(built, model_dir, tmp_path_factory):
    """score_all on a COPY of the fixture warehouse; small segments keep their own rate (min n = 5) so the
    tiny fixture has segment-rule hits too."""
    path = tmp_path_factory.mktemp("api_scored") / "scored.duckdb"
    shutil.copy(built[0], path)
    s = get_settings()
    settings = dataclasses.replace(s, raw={**s.raw, "dss": {**s.raw["dss"], "segment_min_n": 5}})
    con = duckdb.connect(str(path))
    try:
        build_marts(con, settings)
        score_all(con, model_dir=model_dir, settings=settings)
    finally:
        con.close()
    return path


@pytest.fixture(scope="module")
def client(scored_path, model_dir):
    with TestClient(create_app(dw_path=scored_path, model_dir=model_dir)) as c:
        yield c


@pytest.fixture(scope="module")
def wide(client, scored_path):
    """Warehouse rows next to their stored assessment (read through the API's own connection file)."""
    con = duckdb.connect(str(scored_path), read_only=True)
    try:
        return con.execute("""
            SELECT w.*, a.pd_source, a.n_rule_hits, a.grade_source
            FROM mart.v_application_wide w JOIN dw.fact_risk_assessment a USING (application_id)
            ORDER BY application_id""").df()
    finally:
        con.close()


def _ids(wide, *, train: bool, n: int, with_hits: bool = False) -> list[int]:
    part = wide[wide["is_train"] == train]
    if with_hits:
        part = part.sort_values("n_rule_hits", ascending=False, kind="stable")
    return [int(x) for x in part["application_id"].head(n)]


def _value(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return v.item() if hasattr(v, "item") else v


# ---- GET /api/assessment/{id} -----------------------------------------------------------------
def test_assessment_follows_the_contract(client, wide):
    app_id = _ids(wide, train=True, n=1, with_hits=True)[0]
    a = client.get(f"/api/assessment/{app_id}").json()
    assert list(a) == ASSESSMENT_KEYS
    stored = wide.set_index("application_id").loc[app_id]
    assert a["application_id"] == app_id and a["is_train"] is True and a["pd_source"] == "oof"
    assert a["target"] == int(stored["target"])
    assert a["pd"] == pytest.approx(stored["pd"], abs=1e-12)
    assert (a["score_grade"], a["rule_grade"], a["final_grade"]) == (
        stored["score_grade"], _value(stored["rule_grade"]), stored["final_grade"])
    assert a["decision_label"] in {"Approve", "Manual review", "Reject"}
    assert a["missing_fields"] == [] and a["rules_not_evaluated"] == []
    assert 0 < len(a["shap"]["up"]) + len(a["shap"]["down"]) and len(a["shap"]["up"]) <= 5
    assert len(a["rule_hits"]) == stored["n_rule_hits"] > 0
    ranks = [GRADES.index(h["grade"]) for h in a["rule_hits"]]
    assert ranks == sorted(ranks, reverse=True)
    for h in a["rule_hits"]:
        assert set(h) == {"code", "group", "label", "rate", "grade", "segment_key", "rate_key"}
        if h["group"] == "segment":
            assert h["label"].startswith("Segment: ") and h["segment_key"] and h["rate_key"]
    assert a["summary"].startswith(("Final grade equals", "Downgraded from"))


def test_assessment_of_a_test_row_uses_the_full_model(client, wide):
    app_id = _ids(wide, train=False, n=1)[0]
    a = client.get(f"/api/assessment/{app_id}").json()
    assert a["is_train"] is False and a["target"] is None and a["pd_source"] == "model"


def test_assessment_unknown_id_is_404(client):
    assert client.get("/api/assessment/999999999").status_code == 404
    assert client.post("/api/assessment/what-if", json={"application_id": 999999999}).status_code == 404


# ---- POST /api/assessment/what-if ---------------------------------------------------------------
def test_what_if_without_overrides_reproduces_the_stored_assessment(client, wide):
    ids = _ids(wide, train=True, n=6, with_hits=True) + _ids(wide, train=True, n=4) + _ids(wide, train=False, n=4)
    for app_id in ids:
        r = client.post("/api/assessment/what-if", json={"application_id": app_id, "overrides": {}}).json()
        base, scen = r["base"], r["scenario"]
        assert abs(scen["pd"] - base["pd"]) < 1e-9, app_id
        for k in ("pd_source", "score_grade", "rule_grade", "final_grade", "grade_source", "downgraded", "decision",
                  "summary", "rule_hits", "credit_to_income", "target", "is_train"):
            assert scen[k] == base[k], (app_id, k)
        assert scen["recommended_credit"] == pytest.approx(base["recommended_credit"], abs=1e-6)
        assert scen["shap"] == base["shap"]


def test_what_if_recomputes_ratios_from_the_new_amounts(client, wide):
    row = wide[wide["is_train"]].iloc[0]
    app_id, income = int(row["application_id"]), float(row["amt_income"])
    r = client.post("/api/assessment/what-if",
                    json={"application_id": app_id, "overrides": {"amt_income": income / 2}}).json()
    assert r["overrides"] == {"amt_income": income / 2}
    assert r["scenario"]["amt_income"] == income / 2
    assert r["scenario"]["credit_to_income"] == pytest.approx(2 * r["base"]["credit_to_income"])
    assert r["scenario"]["pd_source"] == "oof"


@pytest.mark.parametrize("overrides", [
    {"ext_source_mean": 0.9},             # not in the whitelist
    {"gender": "M"},
    {"late_payment_ratio": 0},
    {"amt_income": -1},                   # invalid values
    {"amt_income": None},
    {"region_rating_client": 4},
    {"income_type": "Martian"},
])
def test_what_if_rejects_unknown_or_invalid_overrides(client, wide, overrides):
    app_id = _ids(wide, train=True, n=1)[0]
    r = client.post("/api/assessment/what-if", json={"application_id": app_id, "overrides": overrides})
    assert r.status_code == 400
    assert next(iter(overrides)) in r.json()["detail"]


# ---- POST /api/simulator/score ------------------------------------------------------------------
def _simulator_body(row) -> dict:
    return {f: _value(row[f]) for f in SIMULATOR_FIELDS}


def test_simulator_with_a_real_row_grades_like_the_assessment(client, wide):
    # test rows: stored PD comes from the full model, the one the simulator uses
    test_rows = wide[~wide["is_train"]].sort_values("n_rule_hits", ascending=False, kind="stable").head(8)
    assert test_rows["n_rule_hits"].max() > 0
    for _, row in test_rows.iterrows():
        sim = client.post("/api/simulator/score", json=_simulator_body(row))
        assert sim.status_code == 200, sim.json()
        sim = sim.json()
        ref = client.get(f"/api/assessment/{int(row['application_id'])}").json()
        assert sim["pd"] == pytest.approx(ref["pd"], abs=1e-9)
        for k in ("score_grade", "rule_grade", "final_grade", "decision", "summary"):
            assert sim[k] == ref[k], k
        assert [h["code"] for h in sim["rule_hits"]] == [h["code"] for h in ref["rule_hits"]]
        assert sim["application_id"] is None and sim["target"] is None and sim["pd_source"] == "model"


def test_simulator_missing_late_payment_ratio_is_not_evaluated(client, wide):
    row = wide[~wide["is_train"]].iloc[0]
    body = {**_simulator_body(row), "late_payment_ratio": None}
    r = client.post("/api/simulator/score", json=body).json()
    assert "late_payment_ratio" in r["missing_fields"]
    not_evaluated = {x["code"]: x for x in r["rules_not_evaluated"]}
    assert {"BR-RF-02", "BR-RF-05"} <= set(not_evaluated)
    assert not_evaluated["BR-RF-02"]["missing"] == ["late_payment_ratio"]
    assert not {"BR-RF-02", "BR-RF-05"} & {h["code"] for h in r["rule_hits"]}


def test_simulator_late_payer_hits_the_rule_when_it_is_graded(client, scored_path, wide):
    con = duckdb.connect(str(scored_path), read_only=True)
    try:
        graded = {c for c, g in con.execute("SELECT rule_code, grade FROM mart.rule_stats").fetchall() if g}
    finally:
        con.close()
    row = wide[~wide["is_train"]].iloc[0]
    r = client.post("/api/simulator/score", json={**_simulator_body(row), "late_payment_ratio": 0.9,
                                                  "max_dpd_days": 120}).json()
    codes = {h["code"] for h in r["rule_hits"]}
    assert {"BR-RF-02", "BR-RF-03", "BR-RF-05"} & graded <= codes
    if codes:
        assert GRADES.index(r["final_grade"]) >= max(GRADES.index(h["grade"]) for h in r["rule_hits"])


def test_simulator_with_only_the_amounts(client):
    r = client.post("/api/simulator/score", json={"amt_income": 135000, "amt_credit": 244998})
    assert r.status_code == 200
    a = r.json()
    assert list(a) == ASSESSMENT_KEYS
    assert a["application_id"] is None and a["is_train"] is None and a["pd_source"] == "model"
    assert a["final_grade"] in GRADES and 0 <= a["pd"] <= 1
    assert a["missing_fields"] == [f for f in SIMULATOR_FIELDS if f not in ("amt_income", "amt_credit")]
    assert a["rule_hits"] == []
    assert {x["code"] for x in a["rules_not_evaluated"]} == {
        "BR-SEG-01", "BR-RF-01", "BR-RF-02", "BR-RF-03", "BR-RF-04", "BR-RF-05"}
    assert a["credit_to_income"] == pytest.approx(244998 / 135000)


def test_simulator_derives_history_fields(client):
    a = client.post("/api/simulator/score", json={"amt_income": 1e5, "amt_credit": 2e5,
                                                  "has_current_overdue": True}).json()
    assert "credit_history_status" not in a["missing_fields"]  # overdue now => 'Current overdue'
    b = client.post("/api/simulator/score", json={"amt_income": 1e5, "amt_credit": 2e5,
                                                  "credit_history_status": "No bureau"}).json()
    assert "has_current_overdue" not in b["missing_fields"] and "cnt_bureau_active" not in b["missing_fields"]
    assert "BR-RF-04" not in {x["code"] for x in b["rules_not_evaluated"]}
    c = client.post("/api/simulator/score", json={"amt_income": 1e5, "amt_credit": 2e5, "income_type": ""}).json()
    assert "income_type" in c["missing_fields"]  # blank string = missing


@pytest.mark.parametrize("body,status", [
    ({"amt_income": 1e5, "amt_credit": 2e5, "credit_history_status": "No bureau", "cnt_bureau_active": 2}, 400),
    ({"amt_income": 1e5, "amt_credit": 2e5, "credit_history_status": "Clean history", "has_current_overdue": True},
     400),
    ({"amt_income": 1e5, "amt_credit": 2e5, "income_type": "Martian"}, 400),
    ({"amt_credit": 2e5}, 422),                                           # required field missing
    ({"amt_income": 0, "amt_credit": 2e5}, 422),
    ({"amt_income": 1e5, "amt_credit": 2e5, "gender": "F"}, 422),        # not an input
    ({"amt_income": 1e5, "amt_credit": 2e5, "credit_to_income": 2}, 422),  # derived, not an input
    ({"amt_income": 1e5, "amt_credit": 2e5, "age_years": 130}, 422),     # outside the segment domain
    ({"amt_income": 1e5, "amt_credit": 2e5, "region_rating_client": 4}, 422),
    ({"amt_income": 1e5, "amt_credit": 2e5, "credit_history_status": "Unknown"}, 422),
])
def test_simulator_rejects_conflicting_or_invalid_input(client, body, status):
    assert client.post("/api/simulator/score", json=body).status_code == status


def test_simulator_options_list_rules_and_members(client):
    o = client.get("/api/simulator/options").json()
    assert o["model_available"] is True
    assert o["region_rating_client"] == [1, 2, 3]
    assert o["income_type"] and o["occupation_type"]
    assert all(set(r) == {"code", "group", "label", "requires"} for r in o["rules"])


# ---- grades, policy, global explanations --------------------------------------------------------
def test_grades_distribution(client, wide):
    assert client.get("/api/grades").json()["scope"] == "all"  # default: every scored application
    g = client.get("/api/grades", params={"scope": "train"}).json()
    n_train = int(wide["is_train"].sum())
    assert g["scope"] == "train" and g["n"] == n_train
    assert [p["grade"] for p in g["policy"]] == list(GRADES) and g["policy"][-1]["max_rate"] is None
    for kind in ("score", "final"):
        rows = g["distribution"][kind]
        assert [r["grade"] for r in rows] == list(GRADES)
        assert sum(r["n"] for r in rows) == n_train
        assert sum(r["share"] for r in rows) == pytest.approx(1.0)
    assert [r["grade"] for r in g["distribution"]["rule"]] == [*GRADES, None]
    assert g["approval_rate"] + g["referral_rate"] + g["reject_rate"] == pytest.approx(1.0)
    train = wide[wide["is_train"]]
    assert g["downgraded_share"] == pytest.approx(train["downgraded"].mean())

    t = client.get("/api/grades", params={"scope": "test"}).json()
    assert t["n"] == N_CUSTOMERS - n_train
    assert all(r["default_rate"] is None for r in t["distribution"]["final"]) and t["approved_default_rate"] is None
    assert client.get("/api/grades", params={"scope": "all"}).json()["n"] == N_CUSTOMERS
    assert client.get("/api/grades", params={"scope": "everything"}).status_code == 400


def test_policy_simulation_with_the_default_policy_matches_grades(client):
    grades = client.get("/api/grades").json()
    s = client.post("/api/policy/simulate", json={}).json()
    assert s["n"] == grades["n"]
    assert s["approval_rate"] == pytest.approx(grades["approval_rate"], abs=1e-12)
    assert [r["grade"] for r in s["by_grade"]] == list(GRADES)
    assert [r["n"] for r in s["by_grade"]] == [r["n"] for r in grades["distribution"]["final"]]
    # the configured policy sent explicitly gives the same answer
    explicit = {
        "thresholds": grades["thresholds"],
        "policy": {p["grade"]: {"decision": p["decision"], "max_credit_to_income": p["max_credit_to_income"]}
                   for p in grades["policy"]},
        "grade_type": "final",
    }
    assert client.post("/api/policy/simulate", json=explicit).json() == s


def test_policy_simulation_with_a_looser_policy(client):
    base = client.post("/api/policy/simulate", json={}).json()
    loose = client.post("/api/policy/simulate", json={"policy": {
        "A": {"decision": "approve", "max_credit_to_income": 8},
        "B": {"decision": "approve", "max_credit_to_income": 6},
        "C": {"decision": "approve", "max_credit_to_income": 4},
        "D": {"decision": "review", "max_credit_to_income": 3},
        "E": {"decision": "reject", "max_credit_to_income": 0}}}).json()
    assert loose["approval_rate"] >= base["approval_rate"]
    score = client.post("/api/policy/simulate", json={"grade_type": "score"}).json()
    assert score["grade_type"] == "score" and score["n"] == base["n"]


@pytest.mark.parametrize("body", [
    {"thresholds": [0.2, 0.1, 0.3, 0.4]},
    {"thresholds": [0.01, 0.07]},
    {"thresholds": 5},
    {"thresholds": "abc"},
    {"policy": {"A": {"decision": "approve"}}},
    {"policy": {g: {"decision": "maybe", "max_credit_to_income": 1} for g in GRADES}},
    {"grade_type": "rule"},
    {"threshold": [0.01, 0.07, 0.13, 0.2]},   # typo -> unknown key
])
def test_policy_simulation_rejects_invalid_policies(client, body):
    assert client.post("/api/policy/simulate", json=body).status_code == 400


def test_global_explanation(client):
    e = client.get("/api/explain/global").json()
    assert 0 < e["auc_oof"] < 1 and e["n_train"] > 0 and e["gender_in_features"] is False
    assert e["features"] and set(e["features"][0]) == {"feature", "label", "mean_abs_shap"}
    assert sum(b["n"] for b in e["calibration"]) == e["n_train"]


# ---- OLAP by grade ------------------------------------------------------------------------------
def test_grade_dimensions_in_by_and_pivot(client, wide):
    rows = client.get("/api/by/final_grade").json()
    assert sum(r["n_applications"] for r in rows) == N_CUSTOMERS
    members = [r["member"] for r in rows]
    assert members == [g for g in GRADES if g in members]
    p = client.get("/api/pivot", params={"row": "final_grade", "col": "credit_history_status",
                                         "measure": "approval_rate"}).json()
    grand = next(c for c in p["cells"] if c["row_member"] == "ALL" and c["col_member"] == "ALL")
    assert grand["value"] == pytest.approx((wide["decision"] == "approve").mean(), abs=1e-9)
    el = client.get("/api/pivot", params={"row": "score_grade", "col": "rule_grade",
                                          "measure": "expected_loss"}).json()
    grand = next(c for c in el["cells"] if c["row_member"] == "ALL" and c["col_member"] == "ALL")
    assert grand["value"] == pytest.approx((wide["amt_credit"] * wide["pd"]).sum(), rel=1e-6)
    h = client.get("/api/hierarchy", params={"levels": "final_grade,score_grade"})
    assert h.status_code == 200 and sum(r["n_applications"] for r in h.json()) == N_CUSTOMERS


def test_rule_grade_null_is_the_no_rule_hit_member(client, wide):
    n_clean = int(wide["rule_grade"].isna().sum())
    assert 0 < n_clean < N_CUSTOMERS
    rows = client.get("/api/by/rule_grade").json()
    assert rows[0]["member"] == "No rule hit" and rows[0]["n_applications"] == n_clean
    assert None not in [r["member"] for r in rows]
    assert client.get("/api/meta").json()["members"]["rule_grade"][0] == "No rule hit"
    kpis = client.get("/api/kpis", params={"rule_grade": "No rule hit"}).json()
    assert kpis["n_applications"] == n_clean
    p = client.get("/api/pivot", params={"row": "rule_grade", "col": "final_grade",
                                         "measure": "n_applications"}).json()
    assert p["rows"][0] == "No rule hit"
    # the displayed SQL names the plain view (the rewrite it runs on is noted in a comment)
    assert "FROM mart.v_application_wide" in p["sql"] and "REPLACE" not in p["sql"]
    assert p["sql"].startswith("-- rule_grade NULL")
    h = client.get("/api/hierarchy", params={"levels": "rule_grade"}).json()
    assert {r["rule_grade"] for r in h} >= {"No rule hit"}


# ---- 503s -------------------------------------------------------------------------------------
def test_dss_endpoints_are_503_without_a_model(scored_path, wide, tmp_path):
    app_id = _ids(wide, train=True, n=1)[0]
    with TestClient(create_app(dw_path=scored_path, model_dir=tmp_path)) as c:
        assert c.get(f"/api/assessment/{app_id}").status_code == 503
        assert c.post("/api/assessment/what-if", json={"application_id": app_id}).status_code == 503
        r = c.post("/api/simulator/score", json={"amt_income": 1e5, "amt_credit": 2e5})
        assert r.status_code == 503 and "hcdw train" in r.json()["detail"]
        assert c.get("/api/explain/global").status_code == 503
        assert c.get("/api/simulator/options").json()["model_available"] is False
        assert c.get("/api/grades").status_code == 200  # stored results need no model


def test_dss_endpoints_are_503_before_scoring(built, model_dir):
    with TestClient(create_app(dw_path=built[0], model_dir=model_dir)) as c:
        con = duckdb.connect(str(built[0]), read_only=True)
        try:
            app_id = con.execute("SELECT MIN(application_id) FROM dw.fact_loan_application").fetchone()[0]
        finally:
            con.close()
        for r in (c.get(f"/api/assessment/{app_id}"), c.get("/api/grades"),
                  c.post("/api/policy/simulate", json={}),
                  c.post("/api/assessment/what-if", json={"application_id": app_id})):
            assert r.status_code == 503 and "hcdw score" in r.json()["detail"]
        assert c.post("/api/simulator/score", json={"amt_income": 1e5, "amt_credit": 2e5}).status_code == 200


# ---- input hardening: non-finite, huge, wrongly typed numbers -----------------------------------
def _post_raw(client, url: str, text: str):
    return client.post(url, content=text, headers={"content-type": "application/json"})


@pytest.mark.parametrize("text", [
    '{"amt_income": 1e400, "amt_credit": 1e400}',
    '{"amt_income": Infinity, "amt_credit": 200000}',
    '{"amt_income": 100000, "amt_credit": NaN}',
    '{"amt_income": 100000, "amt_credit": 1e308}',
    '{"amt_income": 0.1, "amt_credit": 100000000}',      # tiny income would blow up the ratios
    '{"amt_income": true, "amt_credit": 200000}',         # strict: no bools or strings as numbers
    '{"amt_income": "100000", "amt_credit": 200000}',
    '{"amt_income": 100000, "amt_credit": 200000, "has_current_overdue": "yes"}',
    '{"amt_income": 100000, "amt_credit": 200000, "cnt_children": 1000}',
    '{"amt_income": 100000, "amt_credit": 200000, "max_dpd_days": 1e9}',
    '{"amt_income": 100000, "amt_credit": 200000, "late_payment_ratio": NaN}',
])
def test_simulator_refuses_non_finite_or_huge_numbers(client, text):
    r = _post_raw(client, "/api/simulator/score", text)
    assert r.status_code == 422
    assert json.loads(r.text)["detail"][0]["msg"]  # a readable error, even when the input was NaN


def test_simulator_extreme_but_valid_amounts_stay_numbers(client):
    a = client.post("/api/simulator/score", json={"amt_income": 1, "amt_credit": 1e9}).json()
    for k in ("pd", "max_credit_to_income", "amt_income", "amt_credit", "credit_to_income", "recommended_credit"):
        assert isinstance(a[k], float) and math.isfinite(a[k]), k
    assert a["credit_to_income"] == pytest.approx(1e9)


@pytest.mark.parametrize("value", ["1e400", "Infinity", "-Infinity", "NaN", "1e308", "true", '"180000"'])
def test_what_if_refuses_non_finite_or_huge_amounts(client, wide, value):
    app_id = _ids(wide, train=True, n=1)[0]
    r = _post_raw(client, "/api/assessment/what-if",
                  f'{{"application_id": {app_id}, "overrides": {{"amt_income": {value}}}}}')
    assert r.status_code == 400
    assert "amt_income" in json.loads(r.text)["detail"]


def test_what_if_request_refuses_unknown_top_level_keys(client, wide):
    app_id = _ids(wide, train=True, n=1)[0]
    r = client.post("/api/assessment/what-if", json={"application_id": app_id, "override": {"amt_income": 1e5}})
    assert r.status_code == 422


def test_what_if_ratio_uses_the_income_cap(client, scored_path):
    con = duckdb.connect(str(scored_path), read_only=True)
    try:
        cap = con.execute("SELECT MAX(amt_credit / credit_to_income) FROM dw.fact_loan_application "
                          "WHERE credit_to_income > 0").fetchone()[0]
        app_id, income, credit = con.execute(
            "SELECT application_id, amt_income, amt_credit FROM dw.fact_loan_application WHERE amt_income > ? "
            "ORDER BY application_id LIMIT 1", [cap * 1.01]).fetchone()
    finally:
        con.close()
    r = client.post("/api/assessment/what-if",
                    json={"application_id": int(app_id), "overrides": {"amt_credit": credit * 2}}).json()
    assert r["scenario"]["credit_to_income"] == pytest.approx(credit * 2 / cap, rel=1e-12)
    assert r["scenario"]["credit_to_income"] == pytest.approx(2 * r["base"]["credit_to_income"], rel=1e-12)
    assert r["scenario"]["credit_to_income"] > credit * 2 / income * 1.01  # not the uncapped income


def test_what_if_annuity_only_keeps_credit_to_income(client, wide):
    row = wide[wide["is_train"]].iloc[1]
    r = client.post("/api/assessment/what-if", json={
        "application_id": int(row["application_id"]), "overrides": {"amt_annuity": float(row["amt_annuity"]) * 3}})
    assert r.status_code == 200
    r = r.json()
    assert r["scenario"]["credit_to_income"] == r["base"]["credit_to_income"]  # bit for bit: not recomputed


@pytest.mark.parametrize("policy", [
    {"A": {"decision": "approve", "max_credit_to_income": 8, "x": 1}},   # unknown key inside a grade
    {"A": {"decision": "approve", "max_credit_to_income": "8"}},
    {"A": {"decision": "approve", "max_credit_to_income": True}},
    {"A": {"decision": "approve", "max_credit_to_income": 1e400}},
    {"A": {"decision": "approve", "max_credit_to_income": -1}},
    {"A": "approve"},
    ["approve"],
])
def test_policy_simulation_refuses_malformed_grade_entries(client, policy):
    full = {g: {"decision": "review", "max_credit_to_income": 1} for g in GRADES}
    body = {"policy": {**full, **policy}} if isinstance(policy, dict) else {"policy": policy}
    r = client.post("/api/policy/simulate", content=json.dumps(body), headers={"content-type": "application/json"})
    assert r.status_code == 400 and json.loads(r.text)["detail"].startswith("Invalid policy")


# ---- stale scores, messages, unexpected errors -------------------------------------------------
def _model_copy(model_dir, dest, trained_at=None):
    shutil.copytree(model_dir, dest)
    if trained_at is not None:
        metrics = json.loads((dest / "metrics.json").read_text())
        metrics["trained_at"] = trained_at
        (dest / "metrics.json").write_text(json.dumps(metrics))
    return dest


def test_assessment_is_503_when_the_model_is_newer_than_the_scores(scored_path, model_dir, wide, tmp_path):
    later = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
    newer = _model_copy(model_dir, tmp_path / "models", trained_at=later)
    app_id = _ids(wide, train=True, n=1)[0]
    with TestClient(create_app(dw_path=scored_path, model_dir=newer)) as c:
        for r in (c.get(f"/api/assessment/{app_id}"),
                  c.post("/api/assessment/what-if", json={"application_id": app_id})):
            assert r.status_code == 503 and r.json()["detail"] == api.STALE_SCORES
        assert c.get("/api/grades").status_code == 200  # a consistent snapshot of the stored scores
        assert c.post("/api/simulator/score", json={"amt_income": 1e5, "amt_credit": 2e5}).status_code == 200


@pytest.mark.parametrize("trained_at", [None, "not a date", 42])
def test_file_mtimes_or_unreadable_trained_at_never_mean_stale(scored_path, model_dir, wide, tmp_path, trained_at):
    # a Google Drive copy rewrites mtimes: model files look an hour newer than the scores
    models = _model_copy(model_dir, tmp_path / "models", trained_at=trained_at)
    future = time.time() + 3600
    for f in models.iterdir():
        os.utime(f, (future, future))
    app_id = _ids(wide, train=True, n=1)[0]
    with TestClient(create_app(dw_path=scored_path, model_dir=models)) as c:
        assert c.get(f"/api/assessment/{app_id}").status_code == 200


@pytest.mark.parametrize("message", [api.NO_MODEL, api.NOT_SCORED, api.STALE_SCORES, api.OTHER_DATA])
def test_503_messages_are_one_sentence_with_the_fix(message):
    assert message.endswith(".") and ". " not in message and ".." not in message
    assert "stop `hcdw serve`" in message and "then start it again" in message


def test_unexpected_errors_are_a_generic_500(scored_path, model_dir, monkeypatch):
    def boom():
        raise ValueError("internal detail that must not leak")

    monkeypatch.setattr(api.grading, "load_policy", boom)
    with TestClient(create_app(dw_path=scored_path, model_dir=model_dir), raise_server_exceptions=False) as c:
        r = c.get("/api/grades")
    assert r.status_code == 500 and r.json() == {"detail": "Internal server error."}


# ---- applicant profile, KPI expected loss -------------------------------------------------------
def _assert_json_native(profile: dict) -> None:
    assert list(profile) == PROFILE_KEYS
    for k, v in profile.items():
        assert v is None or isinstance(v, (str, bool, int, float)), k
        if isinstance(v, float):
            assert math.isfinite(v), k


def test_assessment_profile_is_the_warehouse_row(client, wide):
    app_id = _ids(wide, train=True, n=1, with_hits=True)[0]
    profile = client.get(f"/api/assessment/{app_id}").json()["profile"]
    _assert_json_native(profile)
    row = wide.set_index("application_id").loc[app_id]
    for k in PROFILE_KEYS:
        if k in PROFILE_DATES:
            expected = None if pd.isna(row[k]) else pd.Timestamp(row[k]).date().isoformat()
        else:
            expected = _value(row[k])
        if isinstance(expected, float):
            assert profile[k] == pytest.approx(expected), k
        else:
            assert profile[k] == expected, k
    assert isinstance(profile["has_current_overdue"], bool) and isinstance(profile["region_rating_client"], int)


def test_what_if_profiles_show_the_overrides(client, wide):
    row = wide[wide["is_train"]].iloc[2]
    app_id = int(row["application_id"])
    r = client.post("/api/assessment/what-if", json={
        "application_id": app_id, "overrides": {"amt_income": 123456.0, "occupation_type": None}}).json()
    assert r["base"]["profile"]["amt_income"] == pytest.approx(float(row["amt_income"]))
    assert r["scenario"]["profile"]["amt_income"] == 123456.0
    assert r["scenario"]["profile"]["occupation_type"] is None
    assert r["scenario"]["profile"]["age_years"] == r["base"]["profile"]["age_years"]
    _assert_json_native(r["scenario"]["profile"])


def test_simulator_profile_holds_input_and_derived_values(client):
    a = client.post("/api/simulator/score", json={
        "amt_income": 150000, "amt_credit": 300000, "credit_history_status": "No bureau",
        "region_rating_client": 2, "income_type": "Working"}).json()
    p = a["profile"]
    _assert_json_native(p)
    assert p["amt_income"] == 150000.0 and p["region_rating_client"] == 2 and p["income_type"] == "Working"
    assert p["has_current_overdue"] is False and p["cnt_bureau_active"] == 0  # derived from 'No bureau'
    assert p["age_years"] is None and p["late_payment_ratio"] is None  # missing stays null


def test_kpis_expected_loss_on_scored_rows(client, wide):
    k = client.get("/api/kpis").json()
    assert k["expected_loss"] == pytest.approx((wide["amt_credit"] * wide["pd"]).sum(), rel=1e-9)
    part = wide[wide["final_grade"] == "E"]
    ke = client.get("/api/kpis", params={"final_grade": "E"}).json()
    assert ke["expected_loss"] == pytest.approx((part["amt_credit"] * part["pd"]).sum(), rel=1e-9)
    rows = client.get("/api/by/final_grade").json()
    assert sum(r["total_credit"] for r in rows) == pytest.approx(wide["amt_credit"].sum())


def test_policy_simulation_passes_credit_volumes_through(client, scored_path, wide):
    from dss import policy as dss_policy
    from dss.grading import load_policy

    con = duckdb.connect(str(scored_path), read_only=True)
    try:
        direct = dss_policy.simulate(con, load_policy())
    finally:
        con.close()
    s = client.post("/api/policy/simulate", json={}).json()
    assert s.keys() == direct.keys()
    for k in ("approved_credit", "approved_credit_requested", "review_credit", "approved_expected_loss"):
        assert isinstance(s[k], float) and math.isfinite(s[k]) and s[k] == pytest.approx(direct[k]), k
    for got, exp in zip(s["by_grade"], direct["by_grade"]):
        assert got.keys() == exp.keys() and {"credit_volume", "requested_credit", "expected_loss"} <= set(got)
        assert got["expected_loss"] == pytest.approx(exp["expected_loss"])
        assert got["credit_volume"] == pytest.approx(exp["credit_volume"])
        assert got["requested_credit"] == pytest.approx(exp["requested_credit"])
    # with the configured policy the approved volume is what `hcdw score` stored for approved train rows
    approved = wide[wide["decision"] == "approve"]  # every scored application
    assert s["approved_credit"] == pytest.approx(approved["recommended_credit"].sum())
    assert s["n"] == len(wide) and s["n_historical"] == int(wide["is_train"].sum())


def test_kpi_summary_scope_and_ignores_filters(client, scored_path, wide):
    """Overview KPIs = mart.kpi_portfolio_summary: counts, decisions, credit and credit at risk on every
    scored application; default rates on the historical (outcome-known) rows only (pandas reference)."""
    con = duckdb.connect(str(scored_path), read_only=True)
    try:
        cur = con.execute("SELECT * FROM mart.kpi_portfolio_summary")
        columns = [d[0] for d in cur.description]
    finally:
        con.close()
    s = client.get("/api/kpis/summary").json()
    assert list(s) == columns
    con = duckdb.connect(str(scored_path), read_only=True)
    try:  # the endpoint is the view (single source of truth)
        view = dict(zip(columns, con.execute("SELECT * FROM mart.kpi_portfolio_summary").fetchone()))
    finally:
        con.close()
    for k, v in view.items():
        assert s[k] == (pytest.approx(v, rel=1e-12) if isinstance(v, float) else v), k
    hist = wide[wide["is_train"]]
    assert s["n_applications"] == s["n_scored"] == len(wide) == N_CUSTOMERS
    assert s["n_labelled"] == len(hist)
    assert s["approval_rate"] == pytest.approx((wide["decision"] == "approve").mean())
    assert s["referral_rate"] == pytest.approx((wide["decision"] == "review").mean())
    assert s["reject_rate"] == pytest.approx((wide["decision"] == "reject").mean())
    assert s["total_credit"] == pytest.approx(wide["amt_credit"].sum())
    assert s["expected_loss"] == pytest.approx((wide["amt_credit"] * wide["pd"]).sum())
    assert s["avg_pd"] == pytest.approx(wide["pd"].mean())
    assert s["downgraded_share"] == pytest.approx(wide["downgraded"].mean())
    assert s["default_rate"] == pytest.approx(hist["target"].mean())
    assert s["approved_default_rate"] == pytest.approx(hist.loc[hist["decision"] == "approve", "target"].mean())
    assert all(isinstance(v, (int, float)) and math.isfinite(v) for v in s.values())
    filtered = client.get("/api/kpis/summary", params={"final_grade": "E", "gender": "F", "bogus": "1"})
    assert filtered.status_code == 200 and filtered.json() == s


def test_overview_explorer_playground_and_cube_agree(client):
    """One scope everywhere: the unfiltered Explorer book, the configured policy ("Current") and the cube
    reproduce the Overview KPIs."""
    s = client.get("/api/kpis/summary").json()
    book = client.post("/api/explore/metrics", json={"conditions": []}).json()["book"]
    assert book["n_applications"] == s["n_applications"]
    assert book["auto_approve_rate"] == pytest.approx(s["approval_rate"], abs=1e-12)
    assert book["manual_review_rate"] == pytest.approx(s["referral_rate"], abs=1e-12)
    assert book["reject_rate"] == pytest.approx(s["reject_rate"], abs=1e-12)
    assert book["total_credit"] == pytest.approx(s["total_credit"], rel=1e-12)
    current = client.post("/api/policy/simulate", json={}).json()
    assert current["n"] == s["n_applications"] and current["n_historical"] == s["n_labelled"]
    assert current["approval_rate"] == pytest.approx(s["approval_rate"], abs=1e-12)
    assert current["referral_rate"] == pytest.approx(s["referral_rate"], abs=1e-12)
    assert current["approved_default_rate"] == pytest.approx(s["approved_default_rate"], abs=1e-12)
    cube = client.post("/api/analytics/cube", json={"measure": "approval_rate"}).json()
    assert cube["n"] == s["n_applications"]
    assert cube["cells"][0]["value"] == pytest.approx(s["approval_rate"], abs=1e-12)
    el = client.post("/api/analytics/cube", json={"measure": "expected_loss"}).json()["cells"][0]["value"]
    assert el == pytest.approx(s["expected_loss"], rel=1e-12)



# ---- policy playground: PD distribution and transitions ---------------------------------------------
def test_pd_distribution_endpoint(client, wide):
    r = client.get("/api/policy/pd-distribution")
    assert r.status_code == 200
    d = r.json()
    assert list(d) == ["n", "bin_width", "bins"] and d["bin_width"] == 0.005 and len(d["bins"]) == 61
    assert list(d["bins"][0]) == ["lo", "hi", "n", "defaults", "default_rate", "credit", "credit_at_risk"]
    assert d["n"] == sum(b["n"] for b in d["bins"]) == \
        client.post("/api/policy/simulate", json={}).json()["n_historical"]  # historical only
    train = wide[wide["is_train"]]
    assert sum(b["defaults"] for b in d["bins"]) == int(train["target"].sum())
    assert sum(b["credit"] for b in d["bins"]) == pytest.approx(train["amt_credit"].sum())
    assert client.get("/api/policy/pd-distribution").json() == d  # cached, same answer


def test_policy_simulate_transitions_via_api(client, wide):
    assert client.post("/api/policy/simulate", json={}).json()["transitions"] == []
    grades = client.get("/api/grades").json()
    policy = {p["grade"]: {"decision": p["decision"], "max_credit_to_income": p["max_credit_to_income"]}
              for p in grades["policy"]}
    policy["C"]["decision"] = "approve"
    tr = client.post("/api/policy/simulate", json={"policy": policy}).json()["transitions"]
    n_c = next(r["n"] for r in grades["distribution"]["final"] if r["grade"] == "C")
    assert [(r["from_grade"], r["to_grade"], r["from_decision"], r["to_decision"], r["n"]) for r in tr] == \
        ([("C", "C", "review", "approve", n_c)] if n_c else [])



# ---- application / outcome dates in the profile ---------------------------------------------------------
def test_profile_carries_the_application_and_outcome_dates(client, wide):
    hist = wide[wide["is_train"]].iloc[0]
    new = wide[~wide["is_train"]].iloc[0]
    p = client.get(f"/api/assessment/{int(hist['application_id'])}").json()["profile"]
    assert p["application_date"] == pd.Timestamp(hist["application_date"]).date().isoformat()
    assert p["outcome_date"] == pd.Timestamp(hist["outcome_date"]).date().isoformat()
    assert p["outcome_date"] > p["application_date"] and isinstance(p["months_to_outcome"], int)
    assert 1 <= p["months_to_outcome"] <= 12 and p["months_to_outcome"] == int(hist["months_to_outcome"])
    q = client.get(f"/api/assessment/{int(new['application_id'])}").json()["profile"]
    assert q["application_date"] == pd.Timestamp(new["application_date"]).date().isoformat()
    assert q["outcome_date"] is None and q["months_to_outcome"] is None
    r = client.post("/api/assessment/what-if", json={"application_id": int(hist["application_id"]),
                                                     "overrides": {"amt_income": 99999.0}}).json()
    assert r["scenario"]["profile"]["application_date"] == p["application_date"]
    assert r["scenario"]["profile"]["months_to_outcome"] == p["months_to_outcome"]


def test_simulator_profile_has_no_dates(client):
    p = client.post("/api/simulator/score", json={"amt_income": 1e5, "amt_credit": 2e5}).json()["profile"]
    assert (p["application_date"], p["outcome_date"], p["months_to_outcome"]) == (None, None, None)


# ---- rules list and per-rule simulation ------------------------------------------------------------
def test_rules_endpoint_matches_rule_stats_and_config(client, scored_path):
    from dss.policy import rule_configs

    rules = client.get("/api/rules").json()
    configs = rule_configs()
    assert [r["code"] for r in rules] == [c["code"] for c in configs]
    con = duckdb.connect(str(scored_path), read_only=True)
    try:
        stats = {r[0]: r[1:] for r in con.execute(
            "SELECT rule_code, n_violators, default_rate, grade FROM mart.rule_stats").fetchall()}
    finally:
        con.close()
    for r, c in zip(rules, configs):
        assert list(r) == ["code", "group", "label", "param", "n_violators", "defaults", "default_rate", "grade"]
        assert (r["group"], r["label"], r["param"]) == (c["group"], c["label"], c.get("param"))
        n, rate, grade = stats[r["code"]]
        assert (r["n_violators"], r["grade"]) == (n, grade) and r["default_rate"] == pytest.approx(rate)
        assert r["defaults"] == round(n * rate)
    assert next(r for r in rules if r["code"] == "BR-RF-03")["param"]["unit"] == "days"
    assert next(r for r in rules if r["code"] == "BR-RF-04")["param"] is None


def test_policy_simulate_with_rules_via_api(client):
    base = client.post("/api/policy/simulate", json={}).json()
    assert client.post("/api/policy/simulate", json={"rules": {"BR-RF-02": {"value": 0.2}}}).json() == base
    assert [list(r) for r in base["rules"]][0] == ["code", "enabled", "value", "n_violators", "n_violators_all",
                                                     "default_rate", "grade"]
    moved = client.post("/api/policy/simulate", json={"rules": {"BR-RF-02": {"value": 0.3}}}).json()
    r02 = {r["code"]: r for r in moved["rules"]}["BR-RF-02"]
    base02 = {r["code"]: r for r in base["rules"]}["BR-RF-02"]
    assert r02["value"] == 0.3 and r02["n_violators"] <= base02["n_violators"]
    off = client.post("/api/policy/simulate", json={"rules": {c: {"enabled": False} for c in
                                                              [r["code"] for r in base["rules"]]}}).json()
    score = client.post("/api/policy/simulate", json={"grade_type": "score"}).json()
    strip = lambda d: {k: v for k, v in d.items() if k not in ("rules", "grade_type")}  # noqa: E731
    assert strip(off) == strip(score)


@pytest.mark.parametrize("rules", [
    {"BR-XX-99": {}}, {"BR-RF-04": {"value": 1}}, {"BR-RF-02": {"value": 2}}, {"BR-RF-02": {"value": "0.3"}},
    {"BR-RF-02": {"value": True}}, {"BR-RF-02": {"enabled": 1}}, {"BR-RF-02": {"on": False}}, ["BR-RF-02"],
])
def test_policy_simulate_rejects_bad_rules(client, rules):
    r = client.post("/api/policy/simulate", json={"rules": rules})
    assert r.status_code == 400 and r.json()["detail"].startswith("Invalid rules")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e400"])
def test_policy_simulate_rejects_non_finite_rule_values(client, value):
    body = '{"rules": {"BR-RF-02": {"value": %s}}}' % value
    r = client.post("/api/policy/simulate", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 400
