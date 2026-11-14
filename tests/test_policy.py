"""Policy simulation: re-grade the stored assessments under new thresholds and policies."""
import dataclasses
import shutil

import duckdb
import numpy as np
import pandas as pd
import pytest

from dss.grading import GRADES, GradePolicy, RuleHit, assess, grade_of, load_policy, recommended_credit, worse
from dss.policy import (RULE_OPS, check_rule_overrides, income_cap, load_inputs, pd_distribution, pd_histogram,
                        rule_configs, simulate)
from dss.rules import evaluate, lookup_segments
from hcdw.config import get_settings
from hcdw.etl.load import build_marts

TRIGGERS = tuple(get_settings().dss["segment_trigger_grades"])


def _with_dss(**changes):
    s = get_settings()
    return dataclasses.replace(s, raw={**s.raw, "dss": {**s.raw["dss"], **changes}})


def _synthetic_pd(app_id: int) -> float:
    # deterministic, covers every grade and hits the exact thresholds 0.01/0.07/0.13/0.20
    return ((app_id * 37) % 100) / 400.0


def _fill(con) -> None:
    """Stand-in for `hcdw score`: synthetic PD + the real rules and grading core."""
    policy = load_policy(get_settings())
    frame = con.execute("SELECT * FROM mart.v_application_wide").df()
    hits = evaluate(con, frame)
    segs = lookup_segments(con, frame)
    rows, hit_rows = [], []
    for app, is_train, credit, cti in frame[["application_id", "is_train", "amt_credit",
                                             "credit_to_income"]].itertuples(index=False):
        app = int(app)
        a = assess(_synthetic_pd(app), hits[app], policy, amt_credit=credit, credit_to_income=cti)
        seg = segs.get(app)
        rows.append((app, a.pd, "oof" if is_train else "model", a.score_grade, a.rule_grade, a.final_grade,
                     a.grade_source, a.downgraded, a.n_rule_hits, seg.segment_key if seg else None,
                     seg.rate if seg else None, a.decision, a.max_credit_to_income, a.recommended_credit))
        hit_rows += [(app, h.code, h.rate, h.grade, h.segment_key, h.rate_key) for h in a.hits]
    # DuckDB FK limitation: clear the child table in its own transaction first
    con.execute("DELETE FROM dw.fact_rule_hit")
    con.execute("DELETE FROM dw.fact_risk_assessment")
    con.executemany("""INSERT INTO dw.fact_risk_assessment (application_id, pd, pd_source, score_grade,
                           rule_grade, final_grade, grade_source, downgraded, n_rule_hits, segment_key,
                           segment_rate, decision, max_credit_to_income, recommended_credit, scored_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())""", rows)
    if hit_rows:
        con.executemany("INSERT INTO dw.fact_rule_hit VALUES (?, ?, ?, ?, ?, ?)", hit_rows)


@pytest.fixture(scope="module")
def scored(built, tmp_path_factory):
    path = tmp_path_factory.mktemp("policy") / "scored.duckdb"
    shutil.copy(built[0], path)
    con = duckdb.connect(str(path))
    build_marts(con, _with_dss(segment_min_n=5))  # varied segment grades, some D/E triggers
    _fill(con)
    yield con
    con.close()


@pytest.fixture(scope="module")
def policy():
    return load_policy(get_settings())


def _stored(con) -> pd.DataFrame:
    """Every scored application (historical and new); target is NaN for new ones, so pandas means of
    target are the historical default rates."""
    return con.execute("""SELECT a.*, f.target, f.is_train FROM dw.fact_risk_assessment a
                          JOIN dw.fact_loan_application f USING (application_id)""").df()


def _check_shape(out: dict, grade_type: str, n: int, policy) -> None:
    assert (out["grade_type"], out["n"]) == (grade_type, n)
    assert [r["grade"] for r in out["by_grade"]] == list(GRADES)
    assert sum(r["n"] for r in out["by_grade"]) == n
    assert sum(r["share"] for r in out["by_grade"]) == pytest.approx(1)
    assert out["approval_rate"] + out["referral_rate"] + out["reject_rate"] == pytest.approx(1)
    for r in out["by_grade"]:
        assert r["decision"] == policy.by_grade[r["grade"]].decision
        assert (r["default_rate"] is None) == (r["n"] == 0)


@pytest.mark.parametrize("grade_type, column", [("final", "final_grade"), ("score", "score_grade")])
def test_default_policy_reproduces_the_stored_grades(scored, policy, grade_type, column):
    stored = _stored(scored)
    assert stored["downgraded"].any() and stored["rule_grade"].notna().any()
    out = simulate(scored, policy, grade_type)
    _check_shape(out, grade_type, len(stored), policy)
    for r in out["by_grade"]:
        rows = stored[stored[column] == r["grade"]]
        assert r["n"] == len(rows), r["grade"]
        if len(rows):
            assert r["default_rate"] == pytest.approx(rows["target"].mean())
    if grade_type == "final":
        decisions = stored["decision"].value_counts(normalize=True)
        assert out["approval_rate"] == pytest.approx(decisions.get("approve", 0.0))
        assert out["referral_rate"] == pytest.approx(decisions.get("review", 0.0))
        assert out["reject_rate"] == pytest.approx(decisions.get("reject", 0.0))
        approved = stored[stored["decision"] == "approve"]
        assert out["approved_default_rate"] == pytest.approx(approved["target"].mean())


def test_every_scored_row_is_simulated(scored, policy):
    n_train = scored.execute("SELECT COUNT(*) FROM dw.fact_loan_application WHERE is_train").fetchone()[0]
    n_all = scored.execute("SELECT COUNT(*) FROM dw.fact_risk_assessment").fetchone()[0]
    assert n_train < n_all
    out = simulate(scored, policy)
    assert (out["n"], out["n_historical"]) == (n_all, n_train)
    assert sum(r["n"] for r in out["by_grade"]) == n_all
    assert sum(r["n_historical"] for r in out["by_grade"]) == n_train


def _regrade(con, t, retrigger_segments: bool = True) -> pd.Series:
    """Reference re-grade in plain Python from the stored PD, business-hit rates and segment rates."""
    stored = _stored(con)
    hits = con.execute("""SELECT h.application_id, d.rule_group, h.rate FROM dw.fact_rule_hit h
                          JOIN dw.dim_rule d USING (rule_code)""").fetchall()
    by_app: dict[int, list[tuple[str, float]]] = {}
    for app, group, rate in hits:
        by_app.setdefault(app, []).append((group, rate))
    out = []
    for app, pd_, seg_rate in stored[["application_id", "pd", "segment_rate"]].itertuples(index=False):
        rule = None
        for group, rate in by_app.get(app, []):
            if group == "business" or not retrigger_segments:
                g = grade_of(rate, t)
                rule = worse(rule, g if group == "business" or g in TRIGGERS else None)
        if retrigger_segments and not pd.isna(seg_rate) and grade_of(seg_rate, t) in TRIGGERS:
            rule = worse(rule, grade_of(seg_rate, t))
        out.append(worse(grade_of(pd_, t), rule))
    return pd.Series(out)


def _counts(out: dict) -> dict[str, int]:
    return {r["grade"]: r["n"] for r in out["by_grade"]}


def test_new_thresholds_regrade_from_stored_values(scored, policy):
    t = [0.03, 0.06, 0.09, 0.15]
    expected = _regrade(scored, t)
    out = simulate(scored, dataclasses.replace(policy, thresholds=t))
    assert _counts(out) == {g: int((expected == g).sum()) for g in GRADES}


def test_segment_trigger_is_reevaluated_under_new_thresholds(scored, policy):
    # C segments (rate >= 0.07) become E here, so they now trigger although no hit was stored for them
    t = [0.01, 0.02, 0.03, 0.05]
    expected = _regrade(scored, t)
    stored_hits_only = _regrade(scored, t, retrigger_segments=False)
    assert (expected != stored_hits_only).any()
    out = simulate(scored, dataclasses.replace(policy, thresholds=t))
    assert _counts(out) == {g: int((expected == g).sum()) for g in GRADES}


def test_policy_changes_decisions_not_grades(scored, policy):
    everyone_rejected = dataclasses.replace(policy, by_grade={g: GradePolicy("reject", 0.0) for g in GRADES})
    out = simulate(scored, everyone_rejected)
    base = simulate(scored, policy)
    assert [r["n"] for r in out["by_grade"]] == [r["n"] for r in base["by_grade"]]
    assert (out["approval_rate"], out["referral_rate"], out["reject_rate"]) == (0.0, 0.0, 1.0)
    assert out["approved_default_rate"] is None


def test_bad_grade_type_is_rejected(scored, policy):
    with pytest.raises(ValueError):
        simulate(scored, policy, "rule")


def test_simulate_before_scoring_returns_empty_result(dw, policy):
    out = simulate(dw, policy)
    assert out["n"] == 0
    assert (out["approval_rate"], out["referral_rate"], out["reject_rate"], out["approved_default_rate"]) == (
        None, None, None, None)
    assert [(r["grade"], r["n"], r["share"]) for r in out["by_grade"]] == [(g, 0, None) for g in GRADES]
    assert (out["approved_credit"], out["approved_credit_requested"], out["review_credit"],
            out["approved_expected_loss"]) == (0.0, 0.0, 0.0, 0.0)
    assert all((r["credit_volume"], r["requested_credit"], r["expected_loss"]) == (0.0, 0.0, 0.0)
               for r in out["by_grade"])


def test_income_cap_is_the_etl_income_quantile(dw):
    # independent of the implementation: the ETL caps incomes at this quantile of all applications
    q = float(get_settings().raw["cleaning"]["income_outlier_quantile"])
    incomes = dw.execute("SELECT amt_income FROM dw.fact_loan_application").df()["amt_income"]
    expected = float(incomes.quantile(q))
    assert incomes.max() > expected  # the fixture's extreme income really is capped
    assert income_cap(dw) == pytest.approx(expected)
    assert np.isfinite(income_cap(dw))


# --- credit volume: the per-grade caps (max_credit_to_income) ------------------------------------

def _amounts(con) -> pd.DataFrame:
    return con.execute("""SELECT a.application_id, a.final_grade, a.score_grade, a.decision, a.recommended_credit,
                                 w.amt_credit, w.credit_to_income
                          FROM dw.fact_risk_assessment a
                          JOIN mart.v_application_wide w USING (application_id)""").df()


def _reference_credit(con, policy, column: str = "final_grade") -> dict[str, tuple[float, float]]:
    """Per grade (credit_volume, requested_credit), row by row with grading.recommended_credit."""
    out = {g: [0.0, 0.0] for g in GRADES}
    for grade, credit, cti in _amounts(con)[[column, "amt_credit", "credit_to_income"]].itertuples(index=False):
        p = policy.by_grade[grade]
        if p.decision != "reject":
            out[grade][0] += recommended_credit(credit, cti, p.max_credit_to_income)
        out[grade][1] += credit
    return {g: (v[0], v[1]) for g, v in out.items()}


def _credit(out: dict) -> dict[str, tuple[float, float]]:
    return {r["grade"]: (r["credit_volume"], r["requested_credit"]) for r in out["by_grade"]}


@pytest.mark.parametrize("grade_type, column", [("final", "final_grade"), ("score", "score_grade")])
def test_default_policy_credit_matches_stored_recommended_credit(scored, policy, grade_type, column):
    rows = _amounts(scored)
    out = simulate(scored, policy, grade_type)
    expected = _reference_credit(scored, policy, column)
    for g in GRADES:
        assert _credit(out)[g] == pytest.approx(expected[g]), g
    if grade_type == "final":
        approved, review = rows[rows["decision"] == "approve"], rows[rows["decision"] == "review"]
        assert len(approved) and len(review)
        assert out["approved_credit"] == pytest.approx(approved["recommended_credit"].sum())
        assert out["approved_credit_requested"] == pytest.approx(approved["amt_credit"].sum())
        assert out["review_credit"] == pytest.approx(review["recommended_credit"].sum())
        for g in GRADES:
            assert _credit(out)[g] == pytest.approx((rows.loc[rows["final_grade"] == g, "recommended_credit"].sum(),
                                                    rows.loc[rows["final_grade"] == g, "amt_credit"].sum()))


def test_lower_caps_lower_credit_but_not_grades_or_rates(scored, policy):
    tight = dataclasses.replace(policy, by_grade={**policy.by_grade, "B": GradePolicy("approve", 2.0),
                                                  "C": GradePolicy("review", 1.0)})
    base, out = simulate(scored, policy), simulate(scored, tight)
    expected = _reference_credit(scored, tight)
    for g in GRADES:
        assert _credit(out)[g] == pytest.approx(expected[g]), g
    assert out["by_grade"][1]["credit_volume"] < base["by_grade"][1]["credit_volume"]
    assert out["approved_credit"] < base["approved_credit"]
    assert out["review_credit"] < base["review_credit"]
    keep = ("n", "approval_rate", "referral_rate", "reject_rate", "approved_default_rate",
            "approved_credit_requested")
    assert {k: out[k] for k in keep} == {k: base[k] for k in keep}
    # caps move only the credit and the loss on it
    strip = lambda rows: [{k: v for k, v in r.items() if k not in ("credit_volume", "expected_loss")}  # noqa: E731
                          for r in rows]
    assert strip(out["by_grade"]) == strip(base["by_grade"])


def test_rejected_grades_get_no_credit_whatever_their_cap(scored, policy):
    generous_reject = dataclasses.replace(policy, by_grade={g: GradePolicy("reject", 8.0) for g in GRADES})
    out = simulate(scored, generous_reject)
    assert (out["approved_credit"], out["approved_credit_requested"], out["review_credit"]) == (0.0, 0.0, 0.0)
    assert all(r["credit_volume"] == 0.0 for r in out["by_grade"])
    assert sum(r["requested_credit"] for r in out["by_grade"]) == pytest.approx(_amounts(scored)["amt_credit"].sum())


def test_simulate_credit_equals_assess_when_a_reject_grade_has_a_cap(scored, policy):
    # D and E rejected with a generous cap: both simulate and assess must give them no credit
    generous = dataclasses.replace(policy, by_grade={**policy.by_grade, "D": GradePolicy("reject", 5.0),
                                                     "E": GradePolicy("reject", 5.0)})
    hits: dict[int, list[RuleHit]] = {}
    for app, code, group, rate, grade in scored.execute("""
            SELECT h.application_id, h.rule_code, d.rule_group, h.rate, h.grade
            FROM dw.fact_rule_hit h JOIN dw.dim_rule d USING (rule_code)""").fetchall():
        hits.setdefault(app, []).append(RuleHit(code, group, code, rate, grade))
    rows = scored.execute("""SELECT a.application_id, a.pd, w.amt_credit, w.credit_to_income
                             FROM dw.fact_risk_assessment a JOIN mart.v_application_wide w USING (application_id)
                             """).fetchall()
    total = {"approve": 0.0, "review": 0.0, "reject": 0.0}
    for app, pd_, credit, cti in rows:
        a = assess(pd_, hits.get(app, []), generous, amt_credit=credit, credit_to_income=cti)
        total[a.decision] += a.recommended_credit
    out = simulate(scored, generous)
    assert out["by_grade"][3]["n"] > 0 and out["by_grade"][3]["requested_credit"] > 0
    assert total["reject"] == 0.0
    assert out["approved_credit"] == pytest.approx(total["approve"])
    assert out["review_credit"] == pytest.approx(total["review"])


# --- expected loss on the recommended credit ------------------------------------------------------

def test_default_policy_expected_loss_matches_stored_assessments(scored, policy):
    rows = scored.execute("""SELECT a.final_grade, a.decision, a.recommended_credit * a.pd AS el
                             FROM dw.fact_risk_assessment a""").df()
    out = simulate(scored, policy)
    approved = rows[rows["decision"] == "approve"]
    assert approved["el"].sum() > 0
    assert out["approved_expected_loss"] == pytest.approx(approved["el"].sum())
    for r in out["by_grade"]:
        assert r["expected_loss"] == pytest.approx(rows.loc[rows["final_grade"] == r["grade"], "el"].sum()), r["grade"]


def test_stricter_decisions_or_lower_caps_lower_approved_expected_loss(scored, policy):
    base = simulate(scored, policy)
    stricter = simulate(scored, dataclasses.replace(policy, by_grade={**policy.by_grade,
                                                                      "B": GradePolicy("review", 6.0)}))
    assert stricter["approved_expected_loss"] < base["approved_expected_loss"]
    assert stricter["by_grade"][1]["expected_loss"] == pytest.approx(base["by_grade"][1]["expected_loss"])
    lower = simulate(scored, dataclasses.replace(policy, by_grade={**policy.by_grade,
                                                                   "B": GradePolicy("approve", 2.0)}))
    assert lower["approved_expected_loss"] < base["approved_expected_loss"]
    assert lower["by_grade"][1]["expected_loss"] < base["by_grade"][1]["expected_loss"]


def test_rejected_grades_carry_no_expected_loss(scored, policy):
    generous = dataclasses.replace(policy, by_grade={**policy.by_grade, "B": GradePolicy("reject", 8.0),
                                                     "D": GradePolicy("reject", 5.0)})
    out = simulate(scored, generous)
    assert out["by_grade"][1]["n"] > 0 and out["by_grade"][3]["n"] > 0
    assert out["by_grade"][1]["expected_loss"] == 0.0 and out["by_grade"][3]["expected_loss"] == 0.0
    approved = [r for r in out["by_grade"] if r["decision"] == "approve"]
    assert out["approved_expected_loss"] == pytest.approx(sum(r["expected_loss"] for r in approved))
    assert simulate(scored, dataclasses.replace(
        policy, by_grade={g: GradePolicy("reject", 8.0) for g in GRADES}))["approved_expected_loss"] == 0.0



# ---- PD distribution -------------------------------------------------------------------------------
def test_pd_histogram_bins_are_left_closed_like_the_grade_scale():
    pd_ = np.array([0.0, 0.0699999, 0.07, 0.0700001, 28 / 400, 0.2999, 0.30, 0.95, 1.0])
    target = np.array([0, 1, 1, 0, 0, 1, 0, 1, 1], dtype=float)
    credit = np.full(len(pd_), 100.0)
    bins = pd_histogram(pd_, target, credit)
    assert len(bins) == 61
    assert [b["lo"] for b in bins[:3]] == [0.0, 0.005, 0.01] and bins[-1]["lo"] == 0.3 and bins[-1]["hi"] == 1.0
    assert all(a["hi"] == b["lo"] for a, b in zip(bins, bins[1:]))
    at = {b["lo"]: b for b in bins}
    assert at[0.07]["n"] == 3          # 0.07, 28/400 (== 0.07) and 0.0700001
    assert at[0.065]["n"] == 1         # 0.0699999
    assert at[0.295]["n"] == 1 and at[0.3]["n"] == 3  # 0.30, 0.95 and 1.0 in the overflow bin
    assert at[0.0]["n"] == 1 and sum(b["n"] for b in bins) == len(pd_)
    assert at[0.07]["defaults"] == 1 and at[0.07]["default_rate"] == pytest.approx(1 / 3)
    assert at[0.07]["credit"] == 300.0 and at[0.07]["credit_at_risk"] == pytest.approx(100 * (0.07 * 2 + 0.0700001))
    empty = at[0.1]
    assert (empty["n"], empty["defaults"], empty["default_rate"], empty["credit"]) == (0, 0, None, 0.0)


def test_pd_distribution_covers_the_simulated_population(scored, policy):
    dist = pd_distribution(scored)  # historical loans only (the bins carry default rates)
    stored = _stored(scored).merge(
        scored.execute("SELECT application_id, amt_credit FROM dw.fact_loan_application").df(), on="application_id")
    stored = stored[stored["is_train"]]
    assert dist["n"] == simulate(scored, policy)["n_historical"] == len(stored) == sum(b["n"] for b in dist["bins"])
    assert dist["bin_width"] == 0.005
    assert sum(b["defaults"] for b in dist["bins"]) == int(stored["target"].sum())
    assert sum(b["credit"] for b in dist["bins"]) == pytest.approx(stored["amt_credit"].sum())
    assert sum(b["credit_at_risk"] for b in dist["bins"]) == pytest.approx((stored["amt_credit"] * stored["pd"]).sum())
    # the synthetic PDs hit 0.07 exactly: those loans open the bin starting at 0.07
    exact = int((stored["pd"] == 0.07).sum())
    assert exact > 0
    in_bin = stored[(stored["pd"] >= 0.07) & (stored["pd"] < 0.075)]
    assert next(b for b in dist["bins"] if b["lo"] == 0.07)["n"] == len(in_bin) >= exact


# ---- transitions -------------------------------------------------------------------------------------
def test_default_policy_has_no_transitions(scored, policy):
    assert simulate(scored, policy)["transitions"] == []


def test_raising_the_bc_cut_off_moves_c_to_b(scored, policy):
    base = simulate(scored, policy)
    out = simulate(scored, dataclasses.replace(policy, thresholds=[0.01, 0.09, 0.13, 0.20]))
    tr = out["transitions"]
    assert tr and all((r["from_grade"], r["to_grade"]) == ("C", "B") for r in tr)
    assert all((r["from_decision"], r["to_decision"]) == ("review", "approve") for r in tr)
    delta_b = _counts(out)["B"] - _counts(base)["B"]
    assert sum(r["n"] for r in tr) == delta_b > 0
    _check_transition_rows(tr)


def test_a_decision_change_is_a_transition_without_a_grade_change(scored, policy):
    c_approved = dataclasses.replace(policy, by_grade={**policy.by_grade, "C": GradePolicy("approve", 4.0)})
    base = simulate(scored, policy)
    tr = simulate(scored, c_approved)["transitions"]
    assert len(tr) == 1
    row = tr[0]
    assert (row["from_grade"], row["to_grade"], row["from_decision"], row["to_decision"]) == \
        ("C", "C", "review", "approve")
    assert row["n"] == _counts(base)["C"]
    stored = _stored(scored)
    c = stored[stored["final_grade"] == "C"]
    assert row["defaults"] == int(c["target"].sum()) and row["default_rate"] == pytest.approx(c["target"].mean())


def test_score_grade_transitions_undo_the_rule_downgrades(scored, policy):
    tr = simulate(scored, policy, "score")["transitions"]
    stored = _stored(scored)
    assert sum(r["n"] for r in tr if r["from_grade"] != r["to_grade"]) == int(stored["downgraded"].sum())
    assert all(r["to_grade"] < r["from_grade"] for r in tr)  # without rules a grade can only improve
    _check_transition_rows(tr)


def _check_transition_rows(rows):
    keys = ["from_grade", "to_grade", "from_decision", "to_decision", "n", "n_historical", "defaults",
            "default_rate", "requested_credit", "credit_at_risk"]
    assert [r["n"] for r in rows] == sorted((r["n"] for r in rows), reverse=True)
    for r in rows:
        assert list(r) == keys and r["n"] > 0
        assert (r["from_grade"], r["from_decision"]) != (r["to_grade"], r["to_decision"])
        assert r["n_historical"] <= r["n"]
        if r["n_historical"]:
            assert r["default_rate"] == pytest.approx(r["defaults"] / r["n_historical"])
        else:
            assert r["default_rate"] is None and r["defaults"] == 0



# ---- per-rule simulation (Playground) --------------------------------------------------------------
RULES = rule_configs()
PARAM_RULES = [r for r in RULES if r.get("param")]


def test_rule_sql_equals_its_param():
    assert {r["code"] for r in PARAM_RULES} == {"BR-RF-01", "BR-RF-02", "BR-RF-03", "BR-RF-05"}
    for r in PARAM_RULES:
        p = r["param"]
        assert r["sql"] == f"{p['field']} {p['op']} {p['value']}", r["code"]
        assert p["min"] <= p["value"] <= p["max"] and p["step"] > 0 and p["unit"] in ("ratio", "days")


def test_param_evaluation_matches_the_stored_hits(scored):
    """`field op value` at the configured value finds exactly the rows scoring stored (same rate too)."""
    inputs = load_inputs(scored)
    for r in PARAM_RULES:
        p = r["param"]
        col = inputs.fields[p["field"]]
        with np.errstate(invalid="ignore"):
            hit = RULE_OPS[p["op"]](col, p["value"]) & ~np.isnan(col)
        stored = inputs.stored[r["code"]]
        assert hit.any(), r["code"]
        assert (hit == ~np.isnan(stored)).all(), r["code"]
        assert inputs.target[hit & inputs.is_train].mean() == pytest.approx(stored[hit][0], abs=1e-12)


def _without_rules(out: dict) -> dict:
    return {k: v for k, v in out.items() if k not in ("rules", "grade_type")}


def test_default_rules_reproduce_todays_results(scored, policy):
    base = simulate(scored, policy)
    configured = {r["code"]: {"enabled": True, **({"value": r["param"]["value"]} if r.get("param") else {})}
                  for r in RULES}
    assert simulate(scored, policy, rules={}) == base
    assert simulate(scored, policy, rules=configured) == base
    assert simulate(scored, policy, inputs=load_inputs(scored)) == base
    # a value moved back to the configured one is a no-op
    assert simulate(scored, policy, rules={"BR-RF-02": {"value": 0.2}}) == base
    assert [(r["code"], r["enabled"], r["value"]) for r in base["rules"]] == [
        (r["code"], True, r["param"]["value"] if r.get("param") else None) for r in RULES]
    stats = dict(scored.execute("SELECT rule_code, n_violators FROM mart.rule_stats").fetchall())
    for r in base["rules"]:
        assert r["n_violators"] == stats[r["code"]], r["code"]


def test_all_rules_off_equals_the_score_grade(scored, policy):
    off = simulate(scored, policy, rules={r["code"]: {"enabled": False} for r in RULES})
    score = simulate(scored, policy, "score")
    assert _without_rules(off) == _without_rules(score)
    assert not any(r["enabled"] for r in score["rules"])


@pytest.mark.parametrize("code", [r["code"] for r in RULES])
def test_disabling_a_rule_never_worsens_a_grade(scored, policy, code):
    out = simulate(scored, policy, rules={code: {"enabled": False}})
    assert all(GRADES.index(t["to_grade"]) <= GRADES.index(t["from_grade"]) for t in out["transitions"])
    assert next(r for r in out["rules"] if r["code"] == code)["enabled"] is False
    expected = _regrade_rules(scored, policy.thresholds, {code: {"enabled": False}})
    assert _counts(out) == {g: int((expected == g).sum()) for g in GRADES}


def test_raising_the_dpd_threshold_reduces_violators(scored, policy):
    base = {r["code"]: r for r in simulate(scored, policy)["rules"]}
    out = simulate(scored, policy, rules={"BR-RF-03": {"value": 365}})
    moved = next(r for r in out["rules"] if r["code"] == "BR-RF-03")
    assert moved["value"] == 365 and moved["n_violators"] < base["BR-RF-03"]["n_violators"]
    assert all(GRADES.index(t["to_grade"]) <= GRADES.index(t["from_grade"]) for t in out["transitions"])


@pytest.mark.parametrize("rules", [
    {"BR-RF-02": {"value": 0.3}},
    {"BR-RF-01": {"value": 0.8}, "BR-RF-05": {"enabled": False}},
    {"BR-RF-03": {"value": 30}, "BR-SEG-01": {"enabled": False}},
    {"BR-RF-02": {"value": 0.0}},
])
def test_edited_rules_regrade_like_the_reference(scored, policy, rules):
    out = simulate(scored, policy, rules=rules)
    expected = _regrade_rules(scored, policy.thresholds, rules)
    assert _counts(out) == {g: int((expected == g).sum()) for g in GRADES}
    t = [0.01, 0.05, 0.1, 0.18]  # the edited rule's rate is graded with the simulated cut-offs
    moved = simulate(scored, dataclasses.replace(policy, thresholds=t), rules=rules)
    expected = _regrade_rules(scored, t, rules)
    assert _counts(moved) == {g: int((expected == g).sum()) for g in GRADES}
    for r in moved["rules"]:
        if r["default_rate"] is not None:
            assert r["grade"] == grade_of(r["default_rate"], t)


def _regrade_rules(con, t, rules) -> pd.Series:
    """Reference final grades in plain pandas: stored hits for unchanged rules, `field op value` for moved
    ones (rate = default rate of the train violators), segment rule re-triggered unless switched off."""
    chosen = check_rule_overrides(rules)
    wide = con.execute("""SELECT w.*, a.pd AS stored_pd, a.segment_rate FROM mart.v_application_wide w
                          JOIN dw.fact_risk_assessment a USING (application_id)
                          ORDER BY w.application_id""").df()
    hits = con.execute("SELECT application_id, rule_code, rate FROM dw.fact_rule_hit").df()
    rule_grade = pd.Series([None] * len(wide), index=wide["application_id"].to_numpy(), dtype=object)
    for r in RULES:
        state = chosen[r["code"]]
        if not state["enabled"]:
            continue
        if r["group"] == "segment":
            for app, rate in wide[["application_id", "segment_rate"]].itertuples(index=False):
                if not pd.isna(rate) and grade_of(rate, t) in TRIGGERS:
                    rule_grade[app] = worse(rule_grade[app], grade_of(rate, t))
            continue
        p = r.get("param")
        if p is None or state["value"] == p["value"]:
            mine = hits[hits["rule_code"] == r["code"]]
            violators = dict(zip(mine["application_id"], mine["rate"]))
        else:
            col = wide[p["field"]].astype(float)
            mask = {"<": col < state["value"], ">": col > state["value"]}[p["op"]].fillna(False)
            ids = wide.loc[mask, "application_id"]  # violators on every row; rated by the historical ones
            hist = mask & wide["is_train"].astype(bool)
            rate = float(wide.loc[hist, "target"].mean()) if hist.any() else None
            violators = {app: rate for app in ids} if rate is not None else {}
        for app, rate in violators.items():
            rule_grade[app] = worse(rule_grade[app], grade_of(rate, t))
    return pd.Series([worse(grade_of(p_, t), g) for p_, g in zip(wide["stored_pd"], rule_grade.to_numpy())])


@pytest.mark.parametrize("rules", [
    {"BR-XX-99": {"enabled": False}},
    {"BR-RF-04": {"value": 1}},              # no threshold on a boolean rule
    {"BR-SEG-01": {"value": 0.1}},
    {"BR-RF-02": {"value": 1.5}},            # outside [min, max]
    {"BR-RF-03": {"value": -15}},
    {"BR-RF-02": {"value": float("nan")}},
    {"BR-RF-02": {"value": float("inf")}},
    {"BR-RF-02": {"value": 10**400}},
    {"BR-RF-02": {"value": "0.3"}},
    {"BR-RF-02": {"value": True}},
    {"BR-RF-02": {"enabled": "no"}},
    {"BR-RF-02": {"enabled": False, "threshold": 0.3}},
    {"BR-RF-02": True},
    ["BR-RF-02"],
])
def test_invalid_rule_overrides_raise(scored, policy, rules):
    with pytest.raises(ValueError):
        check_rule_overrides(rules)
    with pytest.raises(ValueError):
        simulate(scored, policy, rules=rules)
