"""Analytics cube (POST /api/analytics/cube): slice, dice, roll-up, drill-down and pivot on the scored fixture."""
import duckdb
import numpy as np
import pytest

pytest.importorskip("lightgbm")
pytest.importorskip("sklearn")

from tests.conftest import N_CUSTOMERS  # noqa: E402
from tests.test_api_dss import client, model_dir, scored_path  # noqa: E402,F401  (shared fixtures)

GRADES = ["A", "B", "C", "D", "E"]


@pytest.fixture(scope="module")
def wide(scored_path):
    con = duckdb.connect(str(scored_path), read_only=True)
    try:
        df = con.execute("SELECT * FROM mart.v_application_wide").df()
    finally:
        con.close()
    df["rule_grade"] = df["rule_grade"].astype(object).where(df["rule_grade"].notna(), "No rule hit")
    df["application_year"] = df["application_year"].astype(str)
    return df


def _cube(client, **body):
    r = client.post("/api/analytics/cube", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _cell(cube, r, c):
    return next(x for x in cube["cells"] if x["r"] == r and x["c"] == c)


def test_contract_and_meta_hierarchies(client):
    cube = _cube(client, rows=["age_bucket"], cols=["final_grade"], measure="n_applications")
    assert list(cube) == ["rows", "cols", "measure", "n", "members", "cells", "hierarchies"]
    assert cube["measure"] == {"key": "n_applications", "label": "Applications", "format": "count"}
    assert cube["n"] == N_CUSTOMERS and set(cube["members"]) == {"age_bucket", "final_grade"}
    assert all(set(c) == {"r", "c", "value", "n"} for c in cube["cells"])
    hierarchies = {"time": ["application_year", "application_quarter", "application_month"]}
    assert cube["hierarchies"] == hierarchies and client.get("/api/meta").json()["hierarchies"] == hierarchies


def test_totals_add_up(client, wide):
    cube = _cube(client, rows=["age_bucket"], cols=["final_grade"], measure="total_credit")
    rows, cols = cube["members"]["age_bucket"], cube["members"]["final_grade"]
    body = {(x["r"][0], x["c"]): x for x in cube["cells"] if x["r"][0] is not None and x["c"] is not None}
    for r in rows:  # row subtotals
        assert _cell(cube, [r], None)["value"] == pytest.approx(sum(body[(r, c)]["value"] for c in cols
                                                                    if (r, c) in body))
    for c in cols:  # column subtotals
        assert _cell(cube, [None], c)["value"] == pytest.approx(sum(body[(r, c)]["value"] for r in rows
                                                                    if (r, c) in body))
    grand = _cell(cube, [None], None)
    assert grand["value"] == pytest.approx(wide["amt_credit"].sum()) and grand["n"] == N_CUSTOMERS
    assert sum(x["n"] for x in body.values()) == N_CUSTOMERS


def test_default_rate_cells_and_weighted_totals_match_the_reference(client, wide):
    cube = _cube(client, rows=["credit_history_status"], cols=["final_grade"], measure="default_rate")
    train = wide[wide["is_train"]]
    for x in cube["cells"]:
        part = train
        if x["r"][0] is not None:
            part = part[part["credit_history_status"] == x["r"][0]]
        if x["c"] is not None:
            part = part[part["final_grade"] == x["c"]]
        expected = part["target"].mean() if len(part) else None
        assert (x["value"] is None) == (expected is None or np.isnan(expected))
        if x["value"] is not None:
            assert x["value"] == pytest.approx(expected)
    # the grand total is the labelled-weighted mean of the row totals, not their plain mean
    grand = _cell(cube, [None], None)["value"]
    weights = train.groupby("credit_history_status")["target"].agg(["mean", "size"])
    assert grand == pytest.approx((weights["mean"] * weights["size"]).sum() / weights["size"].sum())


def test_slice_and_dice_equal_independent_counts(client, wide):
    sliced = _cube(client, rows=["age_bucket"], measure="n_applications",
                   conditions=[{"field": "credit_history_status", "op": "is", "value": "Clean history"}])
    part = wide[wide["credit_history_status"] == "Clean history"]
    assert sliced["n"] == len(part) > 0
    counts = part["age_bucket"].value_counts()
    for x in sliced["cells"]:
        if x["r"][0] is not None:
            assert x["value"] == x["n"] == counts[x["r"][0]]
    diced = _cube(client, rows=["final_grade"], cols=["contract_type"], measure="n_applications", conditions=[
        {"field": "age_bucket", "op": "is_one_of", "value": ["25-34", "35-44"]},
        {"field": "amt_credit", "op": "between", "value": [100000, 800000]}])
    part = wide[wide["age_bucket"].isin(["25-34", "35-44"]) & wide["amt_credit"].between(100000, 800000)]
    assert diced["n"] == len(part) > 0
    for x in diced["cells"]:
        if x["r"][0] is not None and x["c"] is not None:
            assert x["n"] == len(part[(part["final_grade"] == x["r"][0]) & (part["contract_type"] == x["c"])])


def test_pivot_transposes(client):
    a = _cube(client, rows=["income_type"], cols=["final_grade"], measure="approval_rate")
    b = _cube(client, rows=["final_grade"], cols=["income_type"], measure="approval_rate")
    cells_a = {(x["r"][0], x["c"]): (x["value"], x["n"]) for x in a["cells"]}
    cells_b = {(x["c"], x["r"][0]): (x["value"], x["n"]) for x in b["cells"]}
    assert cells_a == cells_b
    assert a["members"] == b["members"]


def test_roll_up_from_quarters_to_years(client, wide):
    cube = _cube(client, rows=["application_year", "application_quarter"], measure="total_credit")
    years = cube["members"]["application_year"]
    assert years == sorted(years) and cube["members"]["application_quarter"] == \
        sorted(cube["members"]["application_quarter"])
    for y in years:
        quarters = [x for x in cube["cells"] if x["r"][0] == y and x["r"][1] is not None]
        assert all(x["r"][1].startswith(y) for x in quarters)
        total = _cell(cube, [y, None], None)
        assert total["value"] == pytest.approx(sum(x["value"] for x in quarters))
        assert total["n"] == sum(x["n"] for x in quarters) == int((wide["application_year"] == y).sum())
    # rolling up again (dropping the level) gives the same year totals
    years_only = _cube(client, rows=["application_year"], measure="total_credit")
    for y in years:
        assert _cell(years_only, [y], None)["value"] == pytest.approx(_cell(cube, [y, None], None)["value"])
    months = _cube(client, rows=["application_quarter", "application_month"], measure="n_applications")
    for x in months["cells"]:  # drill-down: every month sits under its own quarter
        if x["r"][1] is not None:
            month = int(x["r"][1][5:7])
            assert x["r"][0] == f"{x['r'][1][:4]}-Q{(month - 1) // 3 + 1}"


def test_member_order_matches_api_by(client):
    for dim in ("age_bucket", "income_type", "final_grade", "rule_grade", "application_month"):
        cube = _cube(client, rows=[dim], measure="n_applications")
        by = [r["member"] for r in client.get(f"/api/by/{dim}").json()]
        assert cube["members"][dim] == [m for m in by if m is not None], dim
    assert _cube(client, rows=["rule_grade"], measure="n_applications")["members"]["rule_grade"][0] == "No rule hit"


@pytest.mark.parametrize("body", [
    {"rows": ["nope"]},
    {"rows": ["age_bucket; DROP TABLE dw.dim_time"]},
    {"rows": ["age_bucket", "age_bucket"]},
    {"rows": ["age_bucket"], "cols": ["age_bucket"]},
    {"rows": ["age_bucket", "gender", "income_type"]},
    {"cols": ["age_bucket", "gender"]},
    {"rows": ["age_bucket"], "measure": "evil"},
    {"rows": ["age_bucket"], "conditions": [{"field": "nope", "op": "is", "value": "x"}]},
    {"rows": ["age_bucket"], "conditions": [{"field": "amt_credit", "op": "gt", "value": "1"}]},
])
def test_invalid_cubes_are_400(client, body):
    r = client.post("/api/analytics/cube", json=body)
    assert r.status_code == 400 and r.json()["detail"]


@pytest.mark.parametrize("body", [{"rows": "age_bucket"}, {"rows": [1]}, {"row": ["age_bucket"]},
                                  {"measure": 3}])
def test_malformed_cubes_are_422(client, body):
    assert client.post("/api/analytics/cube", json=body).status_code == 422


def test_injection_like_condition_values_are_values(client):
    cube = _cube(client, rows=["age_bucket"], measure="n_applications",
                 conditions=[{"field": "income_type", "op": "is", "value": "x' OR 1=1 --"}])
    assert cube["n"] == 0 and cube["cells"] in ([], [{"r": [None], "c": None, "value": 0, "n": 0}])
    assert _cube(client, measure="n_applications")["n"] == N_CUSTOMERS


# ---- decision dimension ---------------------------------------------------------------------------
LABELS = {"approve": "Approve", "review": "Manual review", "reject": "Reject"}


def test_decision_dimension_members_and_order(client, wide):
    cube = _cube(client, rows=["decision"], measure="n_applications")
    counts = wide["decision"].map(LABELS).value_counts()
    assert cube["members"]["decision"] == [m for m in ["Approve", "Manual review", "Reject"] if m in counts]
    for m in cube["members"]["decision"]:
        assert _cell(cube, [m], None)["n"] == counts[m]
    assert [r["member"] for r in client.get("/api/by/decision").json()] == cube["members"]["decision"]
    meta = client.get("/api/meta").json()
    assert {"key": "decision", "label": "Decision"} in meta["dimensions"]
    assert meta["members"]["decision"] == cube["members"]["decision"]
    sliced = client.get("/api/kpis", params={"decision": "Manual review"}).json()
    assert sliced["n_applications"] == counts.get("Manual review", 0)


def test_year_by_decision_cube_sums_to_n(client, wide):
    cube = _cube(client, rows=["application_year"], cols=["decision"], measure="n_applications")
    detail = [x for x in cube["cells"] if x["r"][0] is not None and x["c"] is not None]
    assert sum(x["value"] for x in detail) == sum(x["n"] for x in detail) == cube["n"] == N_CUSTOMERS
    for m in cube["members"]["decision"]:
        assert _cell(cube, [None], m)["n"] == int((wide["decision"].map(LABELS) == m).sum())
    # approval_rate keeps working on the labelled decision
    rate = _cube(client, measure="approval_rate")["cells"][0]["value"]
    assert rate == pytest.approx((wide["decision"] == "approve").mean())


def test_explorer_field_catalog_still_intact(client):
    fields = {f["key"]: f for f in client.get("/api/explore/fields").json()["fields"]}
    assert fields["decision"]["values"] == ["Approve", "Manual review", "Reject"]
    assert fields["decision"]["label"] == "Decision" and fields["decision"]["group"] == "Risk & decision"
    m = client.post("/api/explore/metrics", json={"conditions": [
        {"field": "decision", "op": "is", "value": "Manual review"}]})
    assert m.status_code == 200 and m.json()["cohort"]["manual_review_rate"] == 1.0


def test_every_dimension_is_an_explorer_field_with_the_same_members(client):
    """Analytics drill-through turns a cube cell into Explorer conditions: every dimension must resolve."""
    from hcdw.api.queries import DIMENSIONS

    fields = {f["key"]: f for f in client.get("/api/explore/fields").json()["fields"]}
    meta = client.get("/api/meta").json()
    assert set(DIMENSIONS) <= set(fields)
    for dim, label in DIMENSIONS.items():
        f = fields[dim]
        assert f["type"] == "category" and f["label"] == label, dim
        assert f["values"] == meta["members"][dim], dim
        cube = _cube(client, rows=[dim], measure="n_applications")
        # same members; the cube orders unordered dimensions by size (like /api/by), the picker by name
        assert sorted(m for m in cube["members"][dim] if m != "(missing)") == sorted(f["values"]), dim
    assert fields["appr_weekday"]["group"] == "Product"
    assert fields["appr_weekday"]["values"][0] == "MONDAY"


def test_weekday_drill_through_matches_the_cube_cell(client, wide):
    cube = _cube(client, rows=["appr_weekday"], cols=["final_grade"], measure="n_applications")
    cell = max((x for x in cube["cells"] if x["r"][0] is not None and x["c"] is not None), key=lambda x: x["n"])
    m = client.post("/api/explore/metrics", json={"conditions": [
        {"field": "appr_weekday", "op": "is", "value": cell["r"][0]},
        {"field": "final_grade", "op": "is", "value": cell["c"]}]}).json()
    assert m["cohort"]["n_applications"] == cell["n"] > 0
