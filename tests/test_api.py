"""Web API on the fixture warehouse: every endpoint answers, filters slice, names are whitelisted."""
import pytest
from fastapi.testclient import TestClient

from hcdw.api.app import create_app
from hcdw.api.queries import Queries
from tests.conftest import N_CUSTOMERS


@pytest.fixture(scope="module")
def client(built, tmp_path_factory):
    # an empty model dir: these tests never depend on whatever model sits in models/
    with TestClient(create_app(dw_path=built[0], model_dir=tmp_path_factory.mktemp("no_model"))) as c:
        yield c


def test_meta_lists_dimensions_and_members(client):
    meta = client.get("/api/meta").json()
    assert {"age_bucket", "income_type"} <= {d["key"] for d in meta["dimensions"]}
    assert meta["members"]["age_bucket"][0] == "18-24"  # ordered buckets, not alphabetical
    assert any(t["table_name"] == "fact_loan_application" for t in meta["warehouse"]["tables"])


def test_kpis_and_filters_slice(client):
    total = client.get("/api/kpis").json()
    assert total["n_applications"] == N_CUSTOMERS
    assert total["expected_loss"] is None  # nothing scored in this warehouse
    member = client.get("/api/meta").json()["members"]["gender"][0]
    sliced = client.get("/api/kpis", params={"gender": member}).json()
    assert 0 < sliced["n_applications"] < N_CUSTOMERS


def test_group_by_sums_back_to_total(client):
    rows = client.get("/api/by/age_bucket").json()
    assert sum(r["n_applications"] for r in rows) == N_CUSTOMERS
    total_credit = client.get("/api/kpis").json()["total_credit"]
    assert sum(r["total_credit"] for r in rows) == pytest.approx(total_credit)
    cells = client.get("/api/cross", params={"x": "age_bucket", "y": "gender"}).json()
    assert sum(c["total_credit"] for c in cells) == pytest.approx(total_credit)


@pytest.mark.parametrize("path", ["/", "/segments", "/policy", "/olap?tab=drill", "/assessment", "/analytics"])
def test_spa_routes_fall_back_to_index(client, monkeypatch, tmp_path, path):
    from hcdw.api import app as api

    (tmp_path / "index.html").write_text("<!doctype html><div id=root></div>")
    monkeypatch.setattr(api, "FRONTEND_DIST", tmp_path)
    r = client.get(path)
    assert r.status_code == 200 and "id=root" in r.text
    assert client.get("/api/not-an-endpoint").status_code == 404


@pytest.mark.parametrize("url", [
    "/api/cross?x=bureau_active_bucket&y=max_dpd_bucket",
    "/api/score-curve",
    "/api/dti-by-outcome",
    "/api/hierarchy?levels=age_bucket,income_bracket",
    "/api/bureau/kpis",
    "/api/bureau/trend?grain=year",
    "/api/bureau/by-type",
    "/api/simulator/options",
])
def test_endpoints_answer(client, url):
    assert client.get(url).status_code == 200


def test_pivot_is_a_cube_with_totals(client):
    p = client.get("/api/pivot", params={"row": "age_bucket", "col": "gender", "measure": "n_applications"}).json()
    assert p["rows"][-1] == "ALL" and p["cols"][-1] == "ALL"
    grand = next(c for c in p["cells"] if c["row_member"] == "ALL" and c["col_member"] == "ALL")
    assert grand["value"] == N_CUSTOMERS
    assert "GROUP BY CUBE" in p["sql"]


def test_saved_olap_queries_run(client):
    names = [q["name"] for q in client.get("/api/olap").json()]
    assert names == Queries.olap_files() and len(names) >= 6
    for n in names:
        assert client.get(f"/api/olap/{n}").status_code == 200


@pytest.mark.parametrize("url", [
    "/api/by/drop_table",                      # unknown dimension in the path
    "/api/kpis?bogus=1",                       # unknown filter
    "/api/pivot?row=gender&col=gender",        # same dimension twice
    "/api/pivot?row=gender&col=age_bucket&measure=evil",
    "/api/olap/..%2F..%2Fpyproject.toml",      # path traversal attempt
])
def test_bad_names_are_rejected(client, url):
    assert client.get(url).status_code in (400, 404)


def test_new_dss_dimensions_and_measures_work_before_scoring(client):
    rows = client.get("/api/by/credit_history_status").json()
    assert sum(r["n_applications"] for r in rows) == N_CUSTOMERS
    assert [r["member"] for r in rows] == [m for m in ["No bureau", "Clean history", "Current overdue"]
                                           if m in {r["member"] for r in rows}]
    # grades stay NULL until `hcdw score`: one null member, and the DSS measures are null
    assert [r["member"] for r in client.get("/api/by/final_grade").json()] == [None]
    p = client.get("/api/pivot", params={"row": "final_grade", "col": "credit_history_status",
                                         "measure": "approval_rate"})
    assert p.status_code == 200
    grand = next(c for c in p.json()["cells"] if c["row_member"] == "ALL" and c["col_member"] == "ALL")
    assert grand["value"] is None and grand["n_applications"] == N_CUSTOMERS


def test_simulator_without_model_is_503_but_options_answer(client):
    r = client.post("/api/simulator/score", json={"amt_income": 100000, "amt_credit": 200000})
    assert r.status_code == 503 and "hcdw train" in r.json()["detail"]
    opts = client.get("/api/simulator/options").json()
    assert opts["model_available"] is False
    assert opts["required_fields"] == ["amt_income", "amt_credit"]
    assert opts["credit_history_status"] == ["No bureau", "Clean history", "Current overdue"]
    rules = {r["code"]: r for r in opts["rules"]}
    assert rules["BR-RF-02"]["requires"] == ["late_payment_ratio"]
    assert rules["BR-SEG-01"]["group"] == "segment"


@pytest.mark.parametrize("url", ["/api/assessment/100001", "/api/grades", "/api/explain/global",
                                 "/api/policy/pd-distribution", "/api/rules"])
def test_dss_endpoints_are_503_without_model_or_scores(client, url):
    assert client.get(url).status_code == 503
    assert client.post("/api/policy/simulate", json={}).status_code == 503


def test_kpi_summary_before_scoring(client, built):
    import duckdb

    con = duckdb.connect(str(built[0]), read_only=True)
    try:
        cur = con.execute("SELECT * FROM mart.kpi_portfolio_summary")
        columns = [d[0] for d in cur.description]
    finally:
        con.close()
    r = client.get("/api/kpis/summary")
    assert r.status_code == 200
    s = r.json()
    assert list(s) == columns
    assert s["n_applications"] == N_CUSTOMERS and s["n_scored"] == 0 and s["n_labelled"] > 0
    for k in ("expected_loss", "avg_pd", "approval_rate", "approved_default_rate", "referral_rate", "reject_rate",
              "downgraded_share", "auc"):
        assert s[k] is None, k
    assert s["default_rate"] is not None and s["total_credit"] > 0


@pytest.mark.parametrize("dim,width", [("application_year", 4), ("application_quarter", 7),
                                       ("application_month", 7)])
def test_application_calendar_dimensions_are_chronological(client, dw, dim, width):
    rows = client.get(f"/api/by/{dim}").json()
    members = [r["member"] for r in rows]
    assert len(members) > 1 and members == sorted(members) and all(len(m) == width for m in members)
    assert sum(r["n_applications"] for r in rows) == N_CUSTOMERS
    meta = client.get("/api/meta").json()
    assert dim in {d["key"] for d in meta["dimensions"]} and meta["members"][dim] == members
    expected = dict(dw.execute(f"SELECT CAST({dim} AS VARCHAR), COUNT(*) FROM mart.v_application_wide "
                               "GROUP BY 1").fetchall())
    assert {r["member"]: r["n_applications"] for r in rows} == expected


def test_application_calendar_filters_and_pivot(client, dw):
    year = client.get("/api/by/application_year").json()[1]
    k = client.get("/api/kpis", params={"application_year": year["member"]}).json()
    assert k["n_applications"] == year["n_applications"] > 0
    p = client.get("/api/pivot", params={"row": "application_year", "col": "application_quarter",
                                         "measure": "n_applications"}).json()
    assert p["rows"][:-1] == sorted(p["rows"][:-1]) and p["cols"][:-1] == sorted(p["cols"][:-1])
    for c in p["cells"]:
        if "ALL" not in (c["row_member"], c["col_member"]) and c["value"]:
            assert c["col_member"].startswith(c["row_member"])  # a quarter belongs to its year


def test_analytics_cube_before_scoring(client):
    """The cube works on an unscored warehouse; a NULL member shows as '(missing)', null means total."""
    r = client.post("/api/analytics/cube", json={"rows": ["final_grade"], "cols": ["gender"],
                                                 "measure": "n_applications"})
    assert r.status_code == 200
    cube = r.json()
    assert cube["members"]["final_grade"] == ["(missing)"] and cube["n"] == N_CUSTOMERS
    grand = next(c for c in cube["cells"] if c["r"] == [None] and c["c"] is None)
    assert grand["value"] == N_CUSTOMERS
    assert client.post("/api/analytics/cube", json={"measure": "expected_loss"}).json()["cells"][0]["value"] is None


def test_decision_dimension_before_scoring(client):
    cube = client.post("/api/analytics/cube", json={"rows": ["decision"], "measure": "n_applications"}).json()
    assert cube["members"]["decision"] == ["(missing)"] and cube["n"] == N_CUSTOMERS
    assert client.get("/api/meta").json()["members"]["decision"] == []
