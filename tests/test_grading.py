"""Pure grading core: A-E scale, worst-of rule, policy and the downgrade summary (no warehouse)."""
import dataclasses
import itertools

import pytest

from dss.grading import (
    GRADES, Assessment, GradePolicy, Policy, RuleHit, assess, credit_to_income, grade_of, load_policy,
    policy_from_dict, recommended_credit, worse,
)
from hcdw.config import get_settings

THRESHOLDS = get_settings().dss["grade_thresholds"]
# one PD inside each grade of the configured scale
PD_OF = {"A": 0.005, "B": 0.03, "C": 0.10, "D": 0.15, "E": 0.30}


def hit(code: str, grade: str, group: str = "business", rate: float = 0.1) -> RuleHit:
    return RuleHit(code=code, group=group, label=f"label {code}", rate=rate, grade=grade)


@pytest.fixture(scope="module")
def policy() -> Policy:
    return load_policy(get_settings())


# --- scale ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("rate, grade", [
    (0.0, "A"), (0.0099, "A"), (0.01, "B"), (0.0699, "B"), (0.07, "C"), (0.1299, "C"),
    (0.13, "D"), (0.1999, "D"), (0.20, "E"), (1.0, "E"),
])
def test_grade_of_is_left_closed_on_the_configured_scale(rate, grade):
    assert grade_of(rate, THRESHOLDS) == grade


def test_grade_of_uses_the_thresholds_it_is_given():
    t = [0.1, 0.2, 0.3, 0.4]
    assert [grade_of(r, t) for r in (0.0999, 0.1, 0.2, 0.3, 0.4)] == ["A", "B", "C", "D", "E"]


@pytest.mark.parametrize("rate", [-0.01, 1.01, float("nan")])
def test_grade_of_rejects_impossible_rates(rate):
    with pytest.raises(ValueError):
        grade_of(rate, THRESHOLDS)


def test_grade_of_rejects_a_bad_scale():
    with pytest.raises(ValueError):
        grade_of(0.05, [0.01, 0.07, 0.13])


def test_worse_handles_missing_grades():
    assert worse(None, None) is None
    assert worse("C", None) == worse(None, "C") == "C"
    for a, b in itertools.product(GRADES, repeat=2):
        assert worse(a, b) == GRADES[max(GRADES.index(a), GRADES.index(b))]


# --- assess --------------------------------------------------------------------------------------

def test_no_hits_keeps_the_score_grade(policy):
    for grade, pd_ in PD_OF.items():
        a = assess(pd_, [], policy)
        assert (a.score_grade, a.rule_grade, a.final_grade) == (grade, None, grade)
        assert (a.downgraded, a.grade_source, a.n_rule_hits, a.hits) == (False, "score", 0, [])
        assert a.summary == "Final grade equals score grade (no rule violated)."


def test_final_is_never_better_than_score_for_any_grade_pair(policy):
    for score, rule in itertools.product(GRADES, (None, *GRADES)):
        hits = [] if rule is None else [hit("BR-RF-01", rule)]
        a = assess(PD_OF[score], hits, policy)
        assert a.score_grade == score and a.rule_grade == rule
        assert a.final_grade == worse(score, rule)
        assert GRADES.index(a.final_grade) >= GRADES.index(a.score_grade)
        assert a.downgraded == (a.final_grade != a.score_grade)
        expected_source = ("score" if rule is None or rule < score else "both" if rule == score else "rule")
        assert a.grade_source == expected_source, (score, rule)
        p = policy.by_grade[a.final_grade]
        assert (a.decision, a.max_credit_to_income) == (p.decision, p.max_credit_to_income)


def test_rule_grade_is_the_worst_of_several_hits(policy):
    hits = [hit("BR-RF-02", "C"), hit("BR-SEG-01", "E", "segment"), hit("BR-RF-01", "D")]
    a = assess(PD_OF["B"], hits, policy)
    assert (a.rule_grade, a.final_grade, a.n_rule_hits) == ("E", "E", 3)
    assert (a.decision, a.max_credit_to_income) == ("reject", 0.0)
    # sorted for display: worst grade first, then by code
    assert [h.code for h in a.hits] == ["BR-SEG-01", "BR-RF-01", "BR-RF-02"]


def test_decision_comes_from_the_final_grade(policy):
    a = assess(PD_OF["A"], [hit("BR-RF-03", "C")], policy)
    assert (a.final_grade, a.decision, a.max_credit_to_income) == ("C", "review", 4.0)
    assert a.decision_label == get_settings().dss["decision_labels"]["review"]


@pytest.mark.parametrize("pd_", [-0.1, 1.5, float("nan")])
def test_assess_rejects_an_invalid_pd(policy, pd_):
    with pytest.raises(ValueError):
        assess(pd_, [], policy)


# --- summary sentence (English UI, docs/dss_design.md 7.1) --------------------------------------

@pytest.mark.parametrize("score, hits, summary", [
    ("C", [hit("BR-SEG-01", "E", "segment"), hit("BR-RF-02", "C")],
     "Downgraded from C to E by segment rule BR-SEG-01."),
    ("B", [hit("BR-RF-04", "D"), hit("BR-SEG-01", "D", "segment"), hit("BR-RF-02", "C")],
     "Downgraded from B to D by segment rule BR-SEG-01 and business rule BR-RF-04."),
    ("B", [hit("BR-RF-04", "D"), hit("BR-RF-01", "D")],
     "Downgraded from B to D by business rules BR-RF-01, BR-RF-04."),
    ("A", [hit("BR-RF-03", "C")], "Downgraded from A to C by business rule BR-RF-03."),
    ("C", [hit("BR-RF-02", "C")], "Final grade equals score grade C; violated rules do not downgrade it."),
    ("D", [hit("BR-RF-02", "C"), hit("BR-RF-04", "A")],
     "Final grade equals score grade D; violated rules do not downgrade it."),
    ("D", [], "Final grade equals score grade (no rule violated)."),
])
def test_summary_templates(policy, score, hits, summary):
    assert assess(PD_OF[score], hits, policy).summary == summary


# --- credit limit --------------------------------------------------------------------------------

def test_recommended_credit_formula(policy):
    # amt_credit x min(1, max_cti / credit_to_income)
    assert recommended_credit(300_000, 2.0, 6.0) == 300_000
    assert recommended_credit(300_000, 5.0, 4.0) == pytest.approx(240_000)
    assert recommended_credit(300_000, 5.0, 0.0) == 0.0
    a = assess(PD_OF["C"], [], policy, amt_credit=500_000, credit_to_income=5.0)
    assert a.recommended_credit == pytest.approx(400_000)
    e = assess(PD_OF["E"], [], policy, amt_credit=500_000, credit_to_income=1.0)
    assert e.recommended_credit == 0.0
    assert assess(PD_OF["C"], [], policy).recommended_credit is None


def test_credit_to_income_uses_the_capped_income():
    assert credit_to_income(270_000, 135_000) == pytest.approx(2.0)
    assert credit_to_income(1_800_000, 2_000_000, income_cap=900_000) == pytest.approx(2.0)
    assert credit_to_income(270_000, 135_000, income_cap=900_000) == pytest.approx(2.0)
    with pytest.raises(ValueError):
        credit_to_income(270_000, 0)


# --- policy --------------------------------------------------------------------------------------

def test_load_policy_matches_settings(policy):
    dss = get_settings().dss
    assert policy.thresholds == [float(t) for t in dss["grade_thresholds"]]
    assert set(policy.by_grade) == set(GRADES)
    for g, p in dss["policy"].items():
        assert policy.by_grade[g] == GradePolicy(p["decision"], float(p["max_credit_to_income"]))
    assert policy.segment_trigger_grades == tuple(dss["segment_trigger_grades"])
    assert policy.decision_labels == dss["decision_labels"]


@pytest.mark.parametrize("changes", [
    {"thresholds": [0.01, 0.07, 0.13]},
    {"thresholds": [0.01, 0.13, 0.07, 0.20]},
    {"thresholds": [0.0, 0.07, 0.13, 0.20]},
    {"thresholds": [0.01, 0.07, 0.13, 1.0]},
    {"by_grade": {g: GradePolicy("approve", 1.0) for g in "ABCD"}},
    {"by_grade": {g: GradePolicy("maybe", 1.0) for g in GRADES}},
    {"by_grade": {g: GradePolicy("approve", -1.0) for g in GRADES}},
    {"segment_trigger_grades": ("F",)},
])
def test_invalid_policy_is_rejected(policy, changes):
    with pytest.raises(ValueError):
        dataclasses.replace(policy, **changes)


def test_policy_from_dict_builds_on_the_base_policy(policy):
    body = {"thresholds": [0.02, 0.08, 0.15, 0.25],
            "policy": {g: {"decision": "review", "max_credit_to_income": 2.5} for g in GRADES}}
    p = policy_from_dict(body, policy)
    assert p.thresholds == [0.02, 0.08, 0.15, 0.25]
    assert p.by_grade["A"] == GradePolicy("review", 2.5)
    assert p.segment_trigger_grades == policy.segment_trigger_grades
    assert policy_from_dict({}, policy) == policy
    with pytest.raises(ValueError):
        policy_from_dict({"policy": {"A": {"decision": "approve"}}}, policy)


def test_policy_from_dict_null_fields_keep_the_base(policy):
    assert policy_from_dict({"thresholds": None, "policy": None}, policy) == policy


_FULL = {g: {"decision": "approve", "max_credit_to_income": 1.0} for g in GRADES}


@pytest.mark.parametrize("body", [
    [0.01, 0.07, 0.13, 0.20],                                  # body is not an object
    "thresholds",
    {"thresholds": "0.01,0.07,0.13,0.20"},
    {"thresholds": 0.07},
    {"thresholds": {"A": 0.01}},
    {"thresholds": [0.01, "x", 0.13, 0.20]},
    {"thresholds": ["0.01", "0.07", "0.13", "0.20"]},
    {"thresholds": [0.01, None, 0.13, 0.20]},
    {"thresholds": [0.01, [0.07], 0.13, 0.20]},
    {"thresholds": [True, 0.07, 0.13, 0.20]},
    {"policy": "approve"},
    {"policy": [1, 2, 3, 4, 5]},
    {"policy": {**_FULL, "A": "approve"}},
    {"policy": {**_FULL, "A": None}},
    {"policy": {**_FULL, "A": {"decision": ["approve"], "max_credit_to_income": 1.0}}},
    {"policy": {**_FULL, "A": {"decision": {"x": 1}, "max_credit_to_income": 1.0}}},
    {"policy": {**_FULL, "A": {"decision": "approve", "max_credit_to_income": "lots"}}},
    {"policy": {**_FULL, "A": {"decision": "approve", "max_credit_to_income": None}}},
    {"policy": {**_FULL, "A": {"decision": "approve", "max_credit_to_income": [1]}}},
    {"policy": {**_FULL, "A": {"decision": "approve", "max_credit_to_income": float("nan")}}},
    {"policy": {**_FULL, "F": {"decision": "approve", "max_credit_to_income": 1.0}}},
])
def test_policy_from_dict_rejects_malformed_bodies_with_value_error(policy, body):
    # ValueError (never TypeError/KeyError) so the API answers 400, not 500
    with pytest.raises(ValueError):
        policy_from_dict(body, policy)


@pytest.mark.parametrize("changes", [
    {"thresholds": [0.01, [0.07], 0.13, 0.20]},
    {"thresholds": None},
    {"by_grade": [("A", GradePolicy("approve", 1.0))]},
    {"by_grade": {g: GradePolicy("approve", [1.0]) for g in GRADES}},
    {"by_grade": {g: GradePolicy(["approve"], 1.0) for g in GRADES}},
    {"by_grade": {g: "approve" for g in GRADES}},
])
def test_policy_constructor_raises_value_error_for_wrong_types(policy, changes):
    with pytest.raises(ValueError):
        dataclasses.replace(policy, **changes)


def test_assess_keyword_does_not_hide_the_module_helper(policy):
    import dss.grading as grading

    a = assess(PD_OF["C"], [], policy, amt_credit=600_000, credit_to_income=3.0)
    assert a.recommended_credit == 600_000
    assert grading.credit_to_income(600_000, 200_000) == pytest.approx(3.0)


def test_assessment_is_a_plain_record(policy):
    a = assess(PD_OF["B"], [hit("BR-RF-02", "C")], policy, amt_credit=1.0, credit_to_income=1.0)
    assert isinstance(a, Assessment)
    d = dataclasses.asdict(a)
    assert {"pd", "score_grade", "rule_grade", "final_grade", "hits", "decision", "max_credit_to_income",
            "downgraded", "summary", "grade_source", "n_rule_hits", "decision_label",
            "recommended_credit"} <= set(d)
    assert d["hits"][0] == {"code": "BR-RF-02", "group": "business", "label": "label BR-RF-02", "rate": 0.1,
                            "grade": "C", "segment_key": None, "rate_key": None}


def test_rejected_grades_get_no_credit_whatever_their_cap(policy):
    # one rule with policy.simulate: a reject decision never comes with a credit limit
    generous = dataclasses.replace(policy, by_grade={**policy.by_grade, "D": GradePolicy("reject", 5.0),
                                                     "E": GradePolicy("reject", 5.0)})
    for grade in ("D", "E"):
        a = assess(PD_OF[grade], [], generous, amt_credit=300_000, credit_to_income=2.0)
        assert (a.decision, a.recommended_credit) == ("reject", 0.0)
        assert a.max_credit_to_income == 5.0  # the configured cap is still reported (matches dim_risk_grade)
    downgraded = assess(PD_OF["B"], [hit("BR-RF-01", "E")], generous, amt_credit=300_000, credit_to_income=2.0)
    assert (downgraded.final_grade, downgraded.recommended_credit) == ("E", 0.0)
    c = assess(PD_OF["C"], [], generous, amt_credit=300_000, credit_to_income=2.0)
    assert c.recommended_credit == 300_000  # non-reject grades keep using their cap
