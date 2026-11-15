"""ETL of the optional application_dates source: application/outcome date keys, calendar columns,
bureau dates relative to each application, DQ checks, and the behaviour when the file is absent."""
import shutil
from datetime import date

import duckdb
import pandas as pd
import pytest

from hcdw.config import get_settings
from hcdw.etl.pipeline import DataQualityError, run
from tests.conftest import N_CUSTOMERS

ANCHOR = date.fromisoformat(get_settings().anchor_date)


def _source(fixture_dir) -> pd.DataFrame:
    df = pd.read_csv(fixture_dir / "application_dates.csv", dtype={"OUTCOME_DATE": "string"})
    return df.set_index("SK_ID_CURR")


def _copy_fixture(fixture_dir, dest, with_dates: bool = True):
    shutil.copytree(fixture_dir, dest)
    if not with_dates:
        (dest / "application_dates.csv").unlink()
    return dest


def _checks(report) -> dict[str, bool]:
    return {c["name"]: c["passed"] for c in report["data_quality"]["checks"]}


@pytest.fixture(scope="module")
def without_dates(fixture_dir, tmp_path_factory):
    d = _copy_fixture(fixture_dir, tmp_path_factory.mktemp("nodates") / "src", with_dates=False)
    dw_path = tmp_path_factory.mktemp("nodates_dw") / "nodates.duckdb"
    return dw_path, run(d, dw_path=dw_path)


def test_fact_dates_come_from_the_source(dw, fixture_dir):
    src = _source(fixture_dir)
    rows = dw.execute("""
        SELECT f.application_id, ta.full_date, tout.full_date, f.is_train
        FROM dw.fact_loan_application f
        JOIN dw.dim_time ta ON ta.date_key = f.application_date_key
        LEFT JOIN dw.dim_time tout ON tout.date_key = f.outcome_date_key""").fetchall()
    assert len(rows) == N_CUSTOMERS
    for app_id, app_date, outcome, is_train in rows:
        assert app_date.isoformat() == src.loc[app_id, "APPLICATION_DATE"]
        expected = src.loc[app_id, "OUTCOME_DATE"]
        assert (outcome.isoformat() if outcome else None) == (None if pd.isna(expected) else expected)
        assert (outcome is None) == (not is_train)
    assert len({r[1] for r in rows}) > 50  # applications are spread over the calendar, not on one day


def test_wide_view_calendar_columns(dw):
    df = dw.execute("""
        SELECT application_date, application_month, application_quarter, application_year,
               outcome_date, months_to_outcome, is_train, target
        FROM mart.v_application_wide""").df()
    d = pd.to_datetime(df["application_date"])
    assert (df["application_month"] == d.dt.strftime("%Y-%m")).all()
    assert (df["application_quarter"] == d.dt.year.astype(str) + "-Q" + d.dt.quarter.astype(str)).all()
    assert (df["application_year"] == d.dt.year).all()
    scored = df[df["outcome_date"].notna()]
    gap = (pd.to_datetime(scored["outcome_date"]) - pd.to_datetime(scored["application_date"])).dt.days
    assert (scored["months_to_outcome"] == gap // 30).all()
    assert df.loc[~df["is_train"], ["outcome_date", "months_to_outcome"]].isna().all().all()
    lo, hi = get_settings().synthetic_dates["default_after_months"]
    assert scored.loc[scored["target"] == 1, "months_to_outcome"].between(lo, hi).all()


def test_bureau_dates_are_relative_to_each_application(dw):
    rows = dw.execute("""
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE t_open.full_date <> ta.full_date + b.days_credit),
               COUNT(*) FILTER (WHERE t_open.full_date > ta.full_date),
               COUNT(*) FILTER (WHERE t_close.full_date IS NOT NULL
                                  AND t_close.full_date <> ta.full_date + b.days_enddate_fact::INTEGER)
        FROM dw.fact_bureau_credit f
        JOIN staging.bureau b ON b.sk_id_bureau = f.bureau_credit_id
        JOIN dw.fact_loan_application a ON a.application_id = b.sk_id_curr
        JOIN dw.dim_time ta ON ta.date_key = a.application_date_key
        JOIN dw.dim_time t_open ON t_open.date_key = f.open_date_key
        LEFT JOIN dw.dim_time t_close ON t_close.date_key = f.close_date_key""").fetchone()
    assert rows[0] > 0 and rows[1:] == (0, 0, 0)


def test_dim_time_covers_every_date(dw):
    lo, hi = dw.execute("SELECT MIN(full_date), MAX(full_date) FROM dw.dim_time").fetchone()
    first, last = dw.execute("""SELECT MIN(application_date), MAX(GREATEST(application_date, outcome_date))
                                FROM mart.v_application_wide""").fetchone()
    assert lo <= first and last <= hi


def test_date_checks_pass_with_the_source(built):
    checks = _checks(built[1])
    for name in ("source:application_dates_one_row_per_application", "source:application_dates_parse",
                 "source:outcome_not_before_application", "source:dates_not_after_snapshot",
                 "source:test_rows_have_no_outcome", "dates:bureau_open_not_after_application",
                 "fk:fact_loan_application.outcome_date_key->dim_time"):
        assert checks[name], name


def test_without_the_source_every_application_sits_on_the_snapshot(without_dates):
    dw_path, report = without_dates
    assert report["data_quality"]["passed"]
    assert report["extracted_rows"].get("application_dates") is None
    con = duckdb.connect(str(dw_path), read_only=True)
    try:
        days, outcomes = con.execute("""SELECT COUNT(DISTINCT application_date), COUNT(outcome_date)
                                        FROM mart.v_application_wide""").fetchone()
        assert (days, outcomes) == (1, 0)
        assert con.execute("SELECT MIN(application_date) FROM mart.v_application_wide").fetchone()[0] == ANCHOR
    finally:
        con.close()


def test_dates_change_no_other_number(built, without_dates):
    with_rep, without_rep = built[1], without_dates[1]
    counts = {k: v for k, v in with_rep["loaded_rows"].items() if k != "dim_time"}
    assert counts == {k: v for k, v in without_rep["loaded_rows"].items() if k != "dim_time"}
    sql = """
        SELECT (SELECT SUM(amt_credit) FROM dw.fact_loan_application),
               (SELECT AVG(target) FROM dw.fact_loan_application WHERE is_train),
               (SELECT SUM(debt_to_income) FROM dw.fact_loan_application),
               (SELECT SUM(amt_credit_sum) FROM dw.fact_bureau_credit),
               (SELECT COUNT(close_date_key) FROM dw.fact_bureau_credit),
               (SELECT SUM(credit_duration_days) FROM dw.fact_bureau_credit),
               (SELECT COUNT(*) FROM dw.dim_credit_history),
               (SELECT SUM(n) FROM mart.segment_risk),
               (SELECT list(n_violators ORDER BY rule_code) FROM mart.rule_stats)"""
    results = []
    for path in (built[0], without_dates[0]):
        con = duckdb.connect(str(path), read_only=True)
        results.append(con.execute(sql).fetchone())
        con.close()
    for a, b in zip(*results):  # float sums may differ in the last bit: row order is not fixed
        assert a == (pytest.approx(b, rel=1e-12) if isinstance(b, float) else b)


def test_rebuild_gives_identical_dates(fixture_dir, built, tmp_path):
    run(fixture_dir, dw_path=tmp_path / "again.duckdb")
    q = "SELECT application_id, application_date_key, outcome_date_key FROM dw.fact_loan_application ORDER BY 1"
    cons = [duckdb.connect(str(p), read_only=True) for p in (built[0], tmp_path / "again.duckdb")]
    try:
        assert cons[0].execute(q).fetchall() == cons[1].execute(q).fetchall()
    finally:
        for c in cons:
            c.close()


def test_source_in_an_extra_folder_is_found(fixture_dir, tmp_path):
    src = _copy_fixture(fixture_dir, tmp_path / "src", with_dates=False)
    side = tmp_path / "side"
    side.mkdir()
    shutil.copy(fixture_dir / "application_dates.csv", side)
    report = run(src, dw_path=tmp_path / "side.duckdb", extra_source_dirs=[side])
    assert report["extracted_rows"]["application_dates"] == N_CUSTOMERS
    assert report["data_quality"]["passed"]


def test_bad_source_rows_fail_the_checks(fixture_dir, tmp_path):
    src = _copy_fixture(fixture_dir, tmp_path / "src")
    df = pd.read_csv(src / "application_dates.csv", dtype=str)
    test_ids = set(pd.read_csv(src / "application_test.csv")["SK_ID_CURR"].astype(str))
    train_rows = df.index[~df["SK_ID_CURR"].isin(test_ids)]
    test_row = df.index[df["SK_ID_CURR"].isin(test_ids)][0]
    df.loc[train_rows[0], "APPLICATION_DATE"] = "not-a-date"
    df.loc[train_rows[1], "OUTCOME_DATE"] = "2000-01-01"                    # before its application
    df.loc[train_rows[2], "OUTCOME_DATE"] = "2030-01-01"                    # after the snapshot
    df.loc[test_row, "OUTCOME_DATE"] = df.loc[test_row, "APPLICATION_DATE"]  # new application with an outcome
    df = pd.concat([df.drop(index=train_rows[3]), df.iloc[[5]]])            # one missing, one duplicate
    df.to_csv(src / "application_dates.csv", index=False)
    with pytest.raises(DataQualityError) as failed:
        run(src, dw_path=tmp_path / "bad.duckdb")
    checks = _checks(failed.value.report)
    for name in ("source:application_dates_one_row_per_application", "source:application_dates_parse",
                 "source:outcome_not_before_application", "source:dates_not_after_snapshot",
                 "source:test_rows_have_no_outcome"):
        assert checks[name] is False, name
