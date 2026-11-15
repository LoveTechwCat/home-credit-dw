"""DSS warehouse objects: grade/rule dimensions from settings, segment and rule views, KPI views, DQ."""
import dataclasses
import shutil

import duckdb
import pytest

from hcdw.config import get_settings
from hcdw.etl.load import build_marts
from hcdw.etl.quality import run_checks
from tests.conftest import N_CUSTOMERS

GRADES = "ABCDE"


def grade_of(rate: float, thresholds) -> str:
    """Reference left-closed scale: exactly a threshold belongs to the worse grade."""
    return GRADES[sum(rate >= t for t in thresholds)]


def _with_dss(**changes):
    s = get_settings()
    raw = {**s.raw, "dss": {**s.raw["dss"], **changes}}
    return dataclasses.replace(s, raw=raw)


def _copy(built, tmp_path, name="copy.duckdb"):
    path = tmp_path / name
    shutil.copy(built[0], path)
    return duckdb.connect(str(path))


# --- settings -> SQL ---------------------------------------------------------------------------

def test_sql_params_render_dss_fragments():
    p = get_settings().sql_params
    dss = get_settings().raw["dss"]
    assert p["segment_min_n"] == str(dss["segment_min_n"])
    assert p["dss_trigger_grades_sql"] == "'D', 'E'"
    assert p["dss_age_band_sql"].startswith("CASE WHEN age_years >= 18 AND age_years < 25 THEN '18-24'")
    assert "WHEN age_years >= 55 AND age_years < 120 THEN '55+' END" in p["dss_age_band_sql"]
    assert "WHEN income_type IS NULL THEN NULL" in p["dss_income_group_sql"]
    business = [r for r in dss["rules"] if r["group"] == "business"]
    hits = p["dss_rule_hits_sql"]
    assert hits.count("UNION ALL") == len(business) - 1
    for r in business:
        assert f"SELECT application_id, '{r['code']}' AS rule_code FROM src WHERE COALESCE(({r['sql']}), FALSE)" in hits


def test_sql_params_escape_literals_and_handle_no_business_rules():
    seg = {"code": "BR-SEG-01", "group": "segment", "label": "x", "sql": None, "requires": []}
    odd = {"code": "O'Brien", "group": "business", "label": "x", "sql": "max_dpd_days > 1", "requires": []}
    segment = {**get_settings().raw["dss"]["segment"], "income_groups": ["Working", "Kid's"]}
    p = _with_dss(rules=[seg, odd], segment=segment).sql_params
    assert "'O''Brien' AS rule_code" in p["dss_rule_hits_sql"]
    assert "'Kid''s'" in p["dss_income_group_sql"]
    con = duckdb.connect()
    con.execute("CREATE TABLE src AS SELECT 1::BIGINT AS application_id, 5 AS max_dpd_days")
    assert con.execute(p["dss_rule_hits_sql"]).fetchall() == [(1, "O'Brien")]
    empty = _with_dss(rules=[seg]).sql_params["dss_rule_hits_sql"]
    assert con.execute(empty).fetchall() == []


@pytest.mark.parametrize("changes", [
    {"grade_thresholds": [0.01, 0.07, 0.13]},
    {"grade_thresholds": [0.01, 0.13, 0.07, 0.20]},
    {"grade_thresholds": [0.0, 0.07, 0.13, 0.20]},
    {"policy": {g: {"decision": "approve", "max_credit_to_income": 1.0} for g in "ABCD"}},
    {"policy": {g: {"decision": "maybe", "max_credit_to_income": 1.0} for g in GRADES}},
    {"segment_trigger_grades": ["F"]},
])
def test_invalid_dss_settings_are_rejected(changes):
    with pytest.raises(ValueError):
        _with_dss(**changes).dss


@pytest.mark.parametrize("mutate", [
    lambda rules: rules + [rules[1]],                                   # duplicate code
    lambda rules: [r for r in rules if r["group"] != "segment"],         # no segment rule
    lambda rules: rules + [{**rules[0], "code": "BR-SEG-02"}],          # two segment rules
    lambda rules: rules[:1] + [{**rules[1], "sql": None}] + rules[2:],  # business rule without sql
])
def test_invalid_rule_catalog_is_rejected(mutate):
    rules = [dict(r) for r in get_settings().raw["dss"]["rules"]]
    with pytest.raises(ValueError):
        _with_dss(rules=mutate(rules)).dss


def _segment(**age_band):
    seg = get_settings().raw["dss"]["segment"]
    return {**seg, "age_band": {**seg["age_band"], **age_band}}


@pytest.mark.parametrize("changes", [
    {"segment": _segment(labels=["18-24", "25-34", "35-44", "45-54"])},           # 4 labels, 5 bands
    {"segment": _segment(edges=[18, 35, 25, 45, 55, 120])},                        # not increasing
    {"segment": _segment(edges=[18, 25, 25, 45, 55, 120])},                        # not strictly
    {"grade_labels": {g: g for g in "ABCD"}},                                      # E has no label
    {"decision_labels": {"approve": "Approve", "reject": "Reject"}},               # review is used
])
def test_incomplete_dss_settings_are_rejected(changes):
    with pytest.raises(ValueError):
        _with_dss(**changes).dss


@pytest.mark.parametrize("mutate", [
    lambda r: {k: v for k, v in r.items() if k != "requires"},   # business rule without requires
    lambda r: {**r, "requires": "max_dpd_days"},                    # not a list
    lambda r: {**r, "requires": [1]},                               # not a list of str
    lambda r: {**r, "sql": "max_dpd_days > 90; DROP SCHEMA dw CASCADE"},
    lambda r: {**r, "sql": "max_dpd_days > 90 -- comment would swallow the UNION ALL"},
])
def test_unsafe_or_incomplete_business_rule_is_rejected(mutate):
    rules = [dict(r) for r in get_settings().raw["dss"]["rules"]]
    rules[1] = mutate(rules[1])
    with pytest.raises(ValueError):
        _with_dss(rules=rules).dss


# --- dimensions --------------------------------------------------------------------------------

def test_dim_risk_grade_matches_settings(dw):
    dss = get_settings().raw["dss"]
    t = dss["grade_thresholds"]
    rows = dw.execute("""SELECT grade, grade_rank, min_rate, max_rate, label, decision, decision_label,
                                max_credit_to_income FROM dw.dim_risk_grade ORDER BY grade_rank""").fetchall()
    assert [r[0] for r in rows] == list(GRADES)
    assert [r[1] for r in rows] == [1, 2, 3, 4, 5]
    assert [r[2] for r in rows] == [0.0, *t]
    assert [r[3] for r in rows] == [*t, None]
    for grade, _, _, _, label, decision, decision_label, mcti in rows:
        pol = dss["policy"][grade]
        assert (label, decision, mcti) == (dss["grade_labels"][grade], pol["decision"], pol["max_credit_to_income"])
        assert decision_label == dss["decision_labels"][decision]


def test_dim_rule_matches_settings(dw):
    rules = get_settings().raw["dss"]["rules"]
    rows = dw.execute("SELECT rule_code, rule_group, label, sql_condition, requires FROM dw.dim_rule").fetchall()
    assert sorted(rows) == sorted((r["code"], r["group"], r["label"], r["sql"], r["requires"]) for r in rules)


@pytest.mark.parametrize("rate", [0.0, 0.0099, 0.01, 0.0699, 0.07, 0.1299, 0.13, 0.1999, 0.20, 0.5, 1.0])
def test_grade_lookup_is_left_closed(dw, rate):
    # same join the views use to turn a rate into a grade
    grades = dw.execute("""SELECT grade FROM dw.dim_risk_grade g
                           WHERE ? >= g.min_rate AND (g.max_rate IS NULL OR ? < g.max_rate)""",
                        [rate, rate]).fetchall()
    assert grades == [(grade_of(rate, get_settings().raw["dss"]["grade_thresholds"]),)]


def test_exactly_seven_percent_is_c(dw):
    assert dw.execute("""SELECT grade FROM dw.dim_risk_grade
                         WHERE 0.07 >= min_rate AND (max_rate IS NULL OR 0.07 < max_rate)""").fetchone()[0] == "C"


# --- views -------------------------------------------------------------------------------------

def _check_segment_risk(con, min_n: int, thresholds) -> list[int]:
    rows = con.execute("""SELECT segment_key, n, level, rate_key, rate_n, default_rate, grade, is_trigger
                          FROM mart.segment_risk""").fetchall()
    n_train = con.execute("""SELECT COUNT(*) FROM mart.v_application_wide
                             WHERE is_train AND age_years >= 18 AND age_years < 120
                               AND region_rating_client IS NOT NULL""").fetchone()[0]
    assert rows
    assert sum(r[1] for r in rows) == n_train
    assert len({r[0] for r in rows}) == len(rows)
    for key, n, level, rate_key, rate_n, rate, grade, trigger in rows:
        # a small segment never keeps its own rate: it must name the parent level it borrows from
        assert (level == 4) == (n >= min_n), key
        assert level == 0 or rate_n >= min_n, key
        assert rate_key.count("*") == 4 - level, key
        assert rate is not None and grade == grade_of(rate, thresholds), key
        assert trigger == (grade in ("D", "E")), key
    return sorted({r[2] for r in rows})


def test_segment_risk_borrows_parent_rate_for_small_segments(dw):
    dss = get_settings().raw["dss"]
    _check_segment_risk(dw, dss["segment_min_n"], dss["grade_thresholds"])


def test_segment_risk_levels_follow_segment_min_n(built, tmp_path):
    settings = _with_dss(segment_min_n=8)
    con = _copy(built, tmp_path)
    try:
        build_marts(con, settings)
        levels = _check_segment_risk(con, 8, settings.raw["dss"]["grade_thresholds"])
        assert len(levels) >= 2  # with a tiny threshold some segments keep their own rate
        n_domain = con.execute("""SELECT COUNT(DISTINCT credit_history_status) * COUNT(DISTINCT age_band)
                                         * COUNT(DISTINCT income_group) * COUNT(DISTINCT region_rating_client)
                                  FROM mart.segment_risk""").fetchone()[0]
        assert con.execute("SELECT COUNT(*) FROM mart.segment_risk").fetchone()[0] == n_domain
    finally:
        con.close()


def test_rule_stats_uses_train_rows_only(dw):
    p = get_settings().sql_params
    rules = {r["code"]: r for r in get_settings().raw["dss"]["rules"]}
    stats = {r[0]: r[1:] for r in dw.execute(
        "SELECT rule_code, rule_group, label, n_violators, default_rate, grade FROM mart.rule_stats").fetchall()}
    assert set(stats) == set(rules)
    thresholds = get_settings().raw["dss"]["grade_thresholds"]
    for code, (group, label, n, rate, grade) in stats.items():
        assert (group, label) == (rules[code]["group"], rules[code]["label"])
        if group == "business":
            expected = dw.execute(f"""SELECT COUNT(*), AVG(target) FROM mart.v_application_wide
                                      WHERE is_train AND COALESCE(({rules[code]['sql']}), FALSE)""").fetchone()
        else:
            expected = dw.execute(f"""
                SELECT COUNT(*), AVG(w.target)
                FROM (SELECT *, {p['dss_age_band_sql']} AS age_band, {p['dss_income_group_sql']} AS income_group
                      FROM mart.v_application_wide) w
                JOIN mart.segment_risk r ON r.segment_key = concat_ws('|', w.credit_history_status, w.age_band,
                                                                      w.income_group, w.region_rating_client)
                WHERE w.is_train AND r.is_trigger""").fetchone()
        if n:
            assert (n, rate) == pytest.approx(expected)
            assert grade == grade_of(rate, thresholds)
        else:
            assert (expected[0], rate, grade) == (0, None, None)


def test_rule_stats_ignore_test_rows(built, tmp_path):
    # Make every test row violate BR-RF-03: the train-only counts must not move.
    con = _copy(built, tmp_path)
    try:
        before = con.execute("SELECT rule_code, n_violators FROM mart.rule_stats ORDER BY 1").fetchall()
        con.execute("UPDATE dw.fact_loan_application SET max_dpd_days = 999 WHERE NOT is_train")
        assert con.execute("SELECT rule_code, n_violators FROM mart.rule_stats ORDER BY 1").fetchall() == before
    finally:
        con.close()


def test_wide_view_works_while_assessment_is_empty(dw):
    assert dw.execute("SELECT COUNT(*) FROM dw.fact_risk_assessment").fetchone()[0] == 0
    n, scored = dw.execute("""SELECT COUNT(*), COUNT(pd) + COUNT(score_grade) + COUNT(rule_grade)
                                     + COUNT(final_grade) + COUNT(downgraded) + COUNT(decision)
                                     + COUNT(recommended_credit)
                              FROM mart.v_application_wide""").fetchone()
    assert (n, scored) == (N_CUSTOMERS, 0)


def test_kpi_views_with_empty_dss_tables(dw):
    s = dw.execute("SELECT * FROM mart.kpi_portfolio_summary").df().iloc[0]
    assert s["n_applications"] == N_CUSTOMERS and s["n_scored"] == 0
    assert 0 <= s["default_rate"] <= 1 and s["total_credit"] > 0 and s["median_debt_to_income"] > 0
    for col in ("downgraded_share", "expected_loss", "avg_pd", "approval_rate", "approved_default_rate",
                "referral_rate", "reject_rate", "auc"):
        assert s[col] is None or s[col] != s[col], col  # NULL / NaN until `hcdw score` runs
    dist = dw.execute("SELECT grade_type, grade, n, share FROM mart.kpi_grade_distribution").fetchall()
    assert len(dist) == 15 and all(r[2] == 0 and r[3] is None for r in dist)
    assert dw.execute("SELECT COUNT(*) FROM mart.kpi_default_by_segment").fetchone()[0] > 0
    assert dw.execute("SELECT COUNT(*) FROM mart.kpi_default_by_credit_history").fetchone()[0] >= 2


# --- filled DSS tables (stand-in for `hcdw score`, which Stage 3 writes) -----------------------

def _fill_assessments(con) -> None:
    """Synthetic scoring: a deterministic PD per application plus the configured rules."""
    p = get_settings().sql_params
    con.execute(f"""
        CREATE TEMP TABLE hits AS
        WITH src AS (SELECT * FROM mart.v_application_wide), h AS ({p['dss_rule_hits_sql']})
        SELECT h.application_id, h.rule_code, s.default_rate AS rate, s.grade
        FROM h JOIN mart.rule_stats s USING (rule_code) WHERE s.grade IS NOT NULL""")
    con.execute("""
        INSERT INTO dw.fact_risk_assessment (application_id, pd, pd_source, score_grade, rule_grade,
            final_grade, grade_source, downgraded, n_rule_hits, segment_key, segment_rate, decision,
            max_credit_to_income, recommended_credit, scored_at)
        WITH base AS (
            SELECT application_id, is_train, amt_credit, credit_to_income,
                   ((application_id * 37) % 100) / 400.0 AS pd
            FROM mart.v_application_wide),
        sg AS (SELECT b.*, g.grade AS score_grade FROM base b JOIN dw.dim_risk_grade g
                 ON b.pd >= g.min_rate AND (g.max_rate IS NULL OR b.pd < g.max_rate)),
        rg AS (SELECT application_id, MAX(grade) AS rule_grade, COUNT(*) AS n_hits FROM hits GROUP BY 1),
        fin AS (SELECT sg.*, rg.rule_grade, COALESCE(rg.n_hits, 0) AS n_hits,
                       GREATEST(sg.score_grade, COALESCE(rg.rule_grade, 'A')) AS final_grade
                FROM sg LEFT JOIN rg USING (application_id))
        SELECT application_id, pd, CASE WHEN is_train THEN 'oof' ELSE 'model' END,
               score_grade, rule_grade, final_grade,
               CASE WHEN rule_grade IS NULL OR rule_grade < score_grade THEN 'score'
                    WHEN rule_grade = score_grade THEN 'both' ELSE 'rule' END,
               final_grade <> score_grade, n_hits, NULL, NULL, g.decision, g.max_credit_to_income,
               amt_credit * LEAST(1, g.max_credit_to_income / credit_to_income), now()
        FROM fin JOIN dw.dim_risk_grade g ON g.grade = fin.final_grade""")
    con.execute("INSERT INTO dw.fact_rule_hit SELECT application_id, rule_code, rate, grade, NULL, NULL FROM hits")


@pytest.fixture(scope="module")
def scored(built, tmp_path_factory):
    con = _copy(built, tmp_path_factory.mktemp("scored"))
    _fill_assessments(con)
    yield con
    con.close()


def test_quality_checks_validate_filled_dss_tables(scored):
    report = run_checks(scored)
    failed = [c for c in report["checks"] if not c["passed"]]
    assert not failed, failed
    names = {c["name"] for c in report["checks"]}
    assert {"pk_unique:fact_risk_assessment", "pk_unique:fact_rule_hit",
            "fk:fact_rule_hit.rule_code->dim_rule", "completeness:risk_assessment",
            "dss:rule_grade_is_worst_hit", "dss:final_not_better_than_score",
            "dss:grade_dims_match_settings"} <= names
    assert report["row_counts"]["fact_risk_assessment"] == N_CUSTOMERS


def test_quality_flags_partially_scored_warehouse(built, tmp_path):
    con = _copy(built, tmp_path, "partial.duckdb")
    try:
        _fill_assessments(con)
        victim = con.execute("SELECT MIN(application_id) FROM dw.fact_risk_assessment").fetchone()[0]
        con.execute("DELETE FROM dw.fact_rule_hit WHERE application_id = ?", [victim])
        con.execute("DELETE FROM dw.fact_risk_assessment WHERE application_id = ?", [victim])
        checks = {c["name"]: c["passed"] for c in run_checks(con)["checks"]}
        assert checks["completeness:risk_assessment"] is False
        # a stored hit that the assessment row does not account for is caught too
        app = con.execute("""SELECT application_id FROM dw.fact_risk_assessment
                             WHERE rule_grade IS NULL LIMIT 1""").fetchone()[0]
        con.execute("INSERT INTO dw.fact_rule_hit VALUES (?, 'BR-RF-03', 0.5, 'E', NULL, NULL)", [app])
        checks = {c["name"]: c["passed"] for c in run_checks(con)["checks"]}
        assert checks["dss:rule_grade_is_worst_hit"] is False
    finally:
        con.close()


def test_wide_view_and_kpis_after_scoring(scored):
    unscored = scored.execute("SELECT COUNT(*) FROM mart.v_application_wide WHERE final_grade IS NULL").fetchone()[0]
    assert unscored == 0
    s = scored.execute("SELECT * FROM mart.kpi_portfolio_summary").df().iloc[0]
    n_train = scored.execute("SELECT COUNT(*) FROM dw.fact_loan_application WHERE is_train").fetchone()[0]
    # counts, decisions, credit and credit at risk: every scored application; outcomes: train only
    assert (s["n_applications"], s["n_scored"], s["n_labelled"]) == (N_CUSTOMERS, N_CUSTOMERS, n_train)
    assert s["approval_rate"] + s["referral_rate"] + s["reject_rate"] == pytest.approx(1)
    assert 0 <= s["downgraded_share"] <= 1 and 0 <= s["auc"] <= 1 and s["expected_loss"] > 0
    ref = scored.execute("SELECT SUM(amt_credit * pd), AVG(pd) FROM mart.v_application_wide").fetchone()
    assert (s["expected_loss"], s["avg_pd"]) == pytest.approx(ref)
    assert s["default_rate"] == pytest.approx(scored.execute(
        "SELECT AVG(target) FROM mart.v_application_wide WHERE is_train").fetchone()[0])
    final = scored.execute("""SELECT SUM(share), SUM(n) FROM mart.kpi_grade_distribution
                              WHERE grade_type = 'final'""").fetchone()
    assert final == (pytest.approx(1), N_CUSTOMERS)  # SM-1 grade shares add up to 100%
    score_dist = scored.execute("""SELECT grade, n FROM mart.kpi_grade_distribution
                                   WHERE grade_type = 'score' ORDER BY grade""").fetchall()
    assert score_dist == scored.execute("""SELECT g.grade, COUNT(w.application_id) FROM dw.dim_risk_grade g
                                           LEFT JOIN mart.v_application_wide w ON w.score_grade = g.grade
                                           GROUP BY 1 ORDER BY 1""").fetchall()
    rates = scored.execute("""SELECT grade, default_rate FROM mart.kpi_grade_distribution
                              WHERE grade_type = 'final' ORDER BY grade""").fetchall()
    assert rates == [(g, pytest.approx(r) if r is not None else None) for g, r in scored.execute(
        """SELECT g.grade, AVG(w.target) FROM dw.dim_risk_grade g
           LEFT JOIN mart.v_application_wide w ON w.final_grade = g.grade AND w.is_train
           GROUP BY 1 ORDER BY 1""").fetchall()]


def test_auc_matches_a_reference_computation(scored):
    pairs = scored.execute("""SELECT a.pd, f.target FROM dw.fact_risk_assessment a
                              JOIN dw.fact_loan_application f USING (application_id)
                              WHERE f.is_train AND a.pd_source = 'oof'""").fetchall()
    pos = [p for p, t in pairs if t == 1]
    neg = [p for p, t in pairs if t == 0]
    wins = sum((p > q) + 0.5 * (p == q) for p in pos for q in neg)
    auc = scored.execute("SELECT auc FROM mart.kpi_portfolio_summary").fetchone()[0]
    assert auc == pytest.approx(wins / (len(pos) * len(neg)))
