"""Rule catalog and rule evaluation against the warehouse (same SQL as the ETL views)."""
import dataclasses
import shutil

import duckdb
import pandas as pd
import pytest

from dss.grading import RuleHit
from dss.rules import (
    RuleDef, Segment, evaluate, evaluate_record, load_catalog, lookup_segments, rules_not_evaluated,
    segment_label,
)
from hcdw.config import get_settings
from hcdw.etl.load import build_marts

SEG = "BR-SEG-01"


def _with_dss(**changes):
    s = get_settings()
    return dataclasses.replace(s, raw={**s.raw, "dss": {**s.raw["dss"], **changes}})


@pytest.fixture(scope="module")
def seg_con(built, tmp_path_factory):
    """Warehouse copy whose segments keep their own rates (min n = 5), so the fixture has D/E segments."""
    path = tmp_path_factory.mktemp("rules") / "seg.duckdb"
    shutil.copy(built[0], path)
    con = duckdb.connect(str(path))
    build_marts(con, _with_dss(segment_min_n=5))
    assert con.execute("SELECT COUNT(*) FROM mart.segment_risk WHERE is_trigger").fetchone()[0] > 0
    yield con
    con.close()


def _wide(con) -> pd.DataFrame:
    return con.execute("SELECT * FROM mart.v_application_wide ORDER BY application_id").df()


def _record(con, **overrides) -> dict:
    """One real application as a plain dict (the shape the API builds), with overrides applied."""
    row = con.execute("""SELECT * FROM mart.v_application_wide
                         WHERE late_payment_ratio IS NOT NULL ORDER BY application_id LIMIT 1""").df()
    return {**row.iloc[0].to_dict(), **overrides}


# --- catalog -------------------------------------------------------------------------------------

def test_load_catalog_reads_dim_rule_and_rule_stats(dw):
    catalog = load_catalog(dw)
    assert all(isinstance(r, RuleDef) for r in catalog)
    stats = {r[0]: r[1:] for r in dw.execute(
        "SELECT rule_code, n_violators, default_rate, grade FROM mart.rule_stats").fetchall()}
    rules = {r["code"]: r for r in get_settings().dss["rules"]}
    assert {r.code for r in catalog} == set(rules)
    assert catalog[0].code == SEG  # segment rule first, then business rules by code
    assert [r.code for r in catalog[1:]] == sorted(r.code for r in catalog[1:])
    for r in catalog:
        cfg = rules[r.code]
        assert (r.group, r.label, r.sql, list(r.requires)) == (cfg["group"], cfg["label"], cfg["sql"],
                                                                cfg["requires"])
        assert (r.n_violators, r.rate, r.grade) == pytest.approx(stats[r.code], nan_ok=True)


# --- business rules ------------------------------------------------------------------------------

def test_business_hits_match_the_etl_rule_sql(dw):
    frame = _wide(dw)
    result = evaluate(dw, frame)
    assert set(result) == set(frame["application_id"])  # every id present, [] when nothing is violated
    stats = {r[0]: (r[1], r[2]) for r in dw.execute(
        "SELECT rule_code, default_rate, grade FROM mart.rule_stats").fetchall()}
    for rule in get_settings().dss["rules"]:
        if rule["group"] != "business":
            continue
        expected = {r[0] for r in dw.execute(f"""SELECT application_id FROM mart.v_application_wide
                                                 WHERE COALESCE(({rule['sql']}), FALSE)""").fetchall()}
        got = {app for app, hits in result.items() for h in hits if h.code == rule["code"]}
        assert got == expected, rule["code"]
        for hits in result.values():
            for h in hits:
                if h.code == rule["code"]:
                    assert (h.group, h.label) == ("business", rule["label"])
                    assert (h.rate, h.grade) == pytest.approx(stats[rule["code"]])
                    assert (h.segment_key, h.rate_key) == (None, None)


def test_null_late_payment_ratio_is_not_a_violation(dw):
    base = {"application_id": 1, "prev_approval_rate": 0.9, "max_dpd_days": 0, "has_current_overdue": False,
            "credit_history_status": None, "age_years": None, "income_type": None,
            "region_rating_client": None}
    frame = pd.DataFrame([
        {**base, "application_id": 1, "late_payment_ratio": None},
        {**base, "application_id": 2, "late_payment_ratio": float("nan")},
        {**base, "application_id": 3, "late_payment_ratio": 0.6},
    ])
    result = evaluate(dw, frame)
    assert result[1] == [] and result[2] == []
    assert {h.code for h in result[3]} == {"BR-RF-02", "BR-RF-05"}


def test_missing_rule_columns_count_as_null(dw):
    # a frame that lacks the rule columns entirely must not crash or violate anything
    assert evaluate(dw, pd.DataFrame({"application_id": [7, 8]})) == {7: [], 8: []}


def test_rule_without_train_violators_is_not_reported(dw):
    catalog = [dataclasses.replace(r, rate=None, grade=None) if r.code == "BR-RF-03" else r
               for r in load_catalog(dw)]
    frame = pd.DataFrame([{"application_id": 1, "max_dpd_days": 120}])
    assert evaluate(dw, frame) != {1: []}
    assert evaluate(dw, frame, catalog=catalog) == {1: []}


def test_hits_are_sorted_worst_grade_first(dw):
    frame = _wide(dw)
    for hits in evaluate(dw, frame).values():
        keys = [("ABCDE".index(h.grade), h.code) for h in hits]
        assert keys == sorted(keys, key=lambda k: (-k[0], k[1]))


# --- segment rule --------------------------------------------------------------------------------

def test_segment_hits_follow_mart_segment_risk(seg_con):
    p = get_settings().sql_params
    frame = _wide(seg_con)
    expected = {r[0]: r[1:] for r in seg_con.execute(f"""
        SELECT w.application_id, r.segment_key, r.rate_key, r.default_rate, r.grade
        FROM (SELECT *, {p['dss_age_band_sql']} AS age_band, {p['dss_income_group_sql']} AS income_group
              FROM mart.v_application_wide) w
        JOIN mart.segment_risk r USING (credit_history_status, age_band, income_group, region_rating_client)
        WHERE r.is_trigger""").fetchall()}
    assert expected
    result = evaluate(seg_con, frame)
    got = {app: h for app, hits in result.items() for h in hits if h.code == SEG}
    assert set(got) == set(expected)
    for app, h in got.items():
        key, rate_key, rate, grade = expected[app]
        assert (h.group, h.segment_key, h.rate_key, h.grade) == ("segment", key, rate_key, grade)
        assert h.rate == pytest.approx(rate)
        assert h.grade in get_settings().dss["segment_trigger_grades"]
        assert h.label == segment_label(rate_key)


def test_lookup_segments_covers_triggered_and_untriggered_rows(seg_con):
    frame = _wide(seg_con)
    segs = lookup_segments(seg_con, frame)
    assert all(isinstance(s, Segment) for s in segs.values())
    risk = {r[0]: r[1:] for r in seg_con.execute(
        "SELECT segment_key, rate_key, default_rate, grade, is_trigger FROM mart.segment_risk").fetchall()}
    assert {s.is_trigger for s in segs.values()} == {True, False}
    for s in segs.values():
        assert (s.rate_key, s.grade, s.is_trigger) == (risk[s.segment_key][0], risk[s.segment_key][2],
                                                        risk[s.segment_key][3])
        assert s.rate == pytest.approx(risk[s.segment_key][1])
    one = frame.iloc[[0]].assign(age_years=None)
    assert lookup_segments(seg_con, one) == {}


def test_segment_label_uses_display_labels():
    assert segment_label("No bureau|25-34|Working|3") == "Segment: No bureau data | 25-34 | Working | Region 3"
    assert (segment_label("Current overdue|25-34|*|*")
            == "Segment: Currently overdue in bureau | 25-34 | Any | Any")


def test_per_settings_cache_never_mixes_settings():
    # labels are cached per Settings object: another Settings must get its own labels
    seg = get_settings().dss["segment"]
    labels = {**seg["labels"], "credit_history_status": {**seg["labels"]["credit_history_status"],
                                                          "No bureau": "Thin file"}}
    other = _with_dss(segment={**seg, "labels": labels})
    key = "No bureau|25-34|Working|3"
    for _ in range(2):  # second round is served from the caches
        assert segment_label(key) == "Segment: No bureau data | 25-34 | Working | Region 3"
        assert segment_label(key, other) == "Segment: Thin file | 25-34 | Working | Region 3"
        assert segment_label(key, get_settings()) == segment_label(key)


def test_settings_are_validated_once_per_settings_object(dw, monkeypatch):
    import hcdw.config

    frame = _wide(dw)
    catalog = load_catalog(dw)
    evaluate(dw, frame, catalog)  # warm the cache for the shared settings object
    calls = []
    real = hcdw.config.validate_dss
    monkeypatch.setattr(hcdw.config, "validate_dss", lambda d: calls.append(1) or real(d))
    evaluate(dw, frame, catalog)
    evaluate_record(dw, frame.iloc[0].to_dict(), catalog)
    rules_not_evaluated({"late_payment_ratio": None})
    assert calls == []


# --- manual simulator ----------------------------------------------------------------------------

def test_rules_not_evaluated_lists_rules_with_missing_inputs():
    record = {"amt_income": 1.0, "amt_credit": 1.0, "credit_history_status": "No bureau", "age_years": 33.0,
              "income_type": "Working", "region_rating_client": 3, "prev_approval_rate": 0.9,
              "late_payment_ratio": None, "max_dpd_days": float("nan"), "has_current_overdue": False}
    out = rules_not_evaluated(record)
    labels = {r["code"]: r["label"] for r in get_settings().dss["rules"]}
    assert out == [
        {"code": "BR-RF-02", "group": "business", "label": labels["BR-RF-02"], "missing": ["late_payment_ratio"]},
        {"code": "BR-RF-03", "group": "business", "label": labels["BR-RF-03"], "missing": ["max_dpd_days"]},
        {"code": "BR-RF-05", "group": "business", "label": labels["BR-RF-05"], "missing": ["late_payment_ratio"]},
    ]
    assert [r["code"] for r in rules_not_evaluated({**record, "age_years": None})][0] == SEG
    assert [r["code"] for r in rules_not_evaluated({k: v for k, v in record.items()
                                                    if k != "region_rating_client"})][0] == SEG
    full = {**record, "late_payment_ratio": 0.1, "max_dpd_days": 0}
    assert rules_not_evaluated(full) == []


def test_evaluate_record_matches_batch_and_skips_unevaluable_rules(seg_con):
    frame = _wide(seg_con)
    batch = evaluate(seg_con, frame)
    for app in frame["application_id"][:40]:
        record = frame[frame["application_id"] == app].iloc[0].to_dict()
        hits, not_evaluated = evaluate_record(seg_con, record)
        assert hits == batch[app]
        assert not_evaluated == rules_not_evaluated(record)
    record = _record(seg_con, late_payment_ratio=None, application_id=None)
    hits, not_evaluated = evaluate_record(seg_con, record)
    assert {"BR-RF-02", "BR-RF-05"} <= {r["code"] for r in not_evaluated}
    assert not {"BR-RF-02", "BR-RF-05"} & {h.code for h in hits}
    assert all(isinstance(h, RuleHit) for h in hits)
