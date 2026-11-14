"""End-to-end ETL on the synthetic fixture + data-quality and constraint checks."""
import json
import shutil

import duckdb
import pytest

from hcdw.etl.pipeline import DataQualityError, run
from hcdw.sample import build_sample
from tests.conftest import N_CUSTOMERS


def test_quality_checks_pass(built):
    _, report = built
    failed = [c for c in report["data_quality"]["checks"] if not c["passed"]]
    assert not failed, failed


def test_every_applicant_reaches_the_fact(dw):
    assert dw.execute("SELECT COUNT(*) FROM dw.fact_loan_application").fetchone()[0] == N_CUSTOMERS
    assert dw.execute("SELECT COUNT(*) FROM dw.dim_customer").fetchone()[0] == N_CUSTOMERS


def test_test_rows_have_no_target(dw):
    n = dw.execute("SELECT COUNT(*) FROM dw.fact_loan_application WHERE NOT is_train AND target IS NOT NULL").fetchone()[0]
    assert n == 0
    assert dw.execute("SELECT COUNT(*) FROM dw.fact_loan_application WHERE NOT is_train").fetchone()[0] > 0


def test_marts_are_queryable(dw):
    wide = dw.execute("SELECT COUNT(*) FROM mart.v_application_wide").fetchone()[0]
    assert wide == N_CUSTOMERS
    rate = dw.execute("SELECT default_rate FROM mart.kpi_portfolio_summary").fetchone()[0]
    assert 0 <= rate <= 1


def test_every_application_has_one_credit_history_status(dw):
    rows = dw.execute("""
        SELECT credit_history_status, bureau_active_bucket = 'No bureau record' AS no_bureau,
               has_current_overdue, COUNT(*)
        FROM mart.v_application_wide GROUP BY ALL""").fetchall()
    assert sum(r[3] for r in rows) == N_CUSTOMERS
    for status, no_bureau, overdue, _ in rows:
        expected = "No bureau" if no_bureau else ("Current overdue" if overdue else "Clean history")
        assert status == expected
    # the status is part of the mini-dimension's natural key, so each profile maps to one row
    dupes = dw.execute("""
        SELECT COUNT(*) - COUNT(DISTINCT (bureau_active_bucket, max_dpd_bucket, has_current_overdue,
                                          has_bad_debt, prev_approval_bucket, credit_history_status))
        FROM dw.dim_credit_history""").fetchone()[0]
    assert dupes == 0


def test_foreign_key_is_enforced(built, tmp_path):
    con = duckdb.connect(str(built[0]))  # writable copy is not needed: we roll back
    try:
        con.execute("BEGIN")
        with pytest.raises(duckdb.ConstraintException):
            con.execute("""INSERT INTO dw.fact_bureau_credit (bureau_credit_id, customer_key, bureau_category_key,
                           open_date_key) VALUES (-1, -999, 1, 20180517)""")
    finally:
        con.execute("ROLLBACK")
        con.close()


def test_rerun_is_idempotent(fixture_dir, tmp_path):
    dw_path = tmp_path / "rerun.duckdb"
    first = run(fixture_dir, dw_path=dw_path)["loaded_rows"]
    second = run(fixture_dir, dw_path=dw_path)["loaded_rows"]
    assert first == second


def test_sample_keeps_children_consistent(fixture_dir, tmp_path):
    out = tmp_path / "sample"
    counts = build_sample(fixture_dir, out, n_customers=60, test_share=0.2, seed=1, compress=True)
    assert counts["application_train"] + counts["application_test"] <= 60
    con = duckdb.connect()
    orphans = con.execute(f"""
        SELECT COUNT(*) FROM read_csv('{(out / 'bureau.csv.gz').as_posix()}') b
        WHERE b.SK_ID_CURR NOT IN (
            SELECT SK_ID_CURR FROM read_csv('{(out / 'application_train.csv.gz').as_posix()}')
            UNION ALL
            SELECT SK_ID_CURR FROM read_csv('{(out / 'application_test.csv.gz').as_posix()}'))""").fetchone()[0]
    assert orphans == 0
    # the dates source is cut to the same applicants, also when it lives outside the source folder
    assert counts["application_dates"] == counts["application_train"] + counts["application_test"]
    report = run(out, dw_path=tmp_path / "sample.duckdb")
    assert report["data_quality"]["passed"]
    assert report["extracted_rows"]["application_dates"] == counts["application_dates"]


def test_sample_finds_dates_in_an_extra_folder(fixture_dir, tmp_path):
    src, side = tmp_path / "src", tmp_path / "side"
    shutil.copytree(fixture_dir, src)
    side.mkdir()
    shutil.move(src / "application_dates.csv", side / "application_dates.csv")
    counts = build_sample(src, tmp_path / "out", n_customers=40, test_share=0.2, seed=1, compress=False,
                          extra_dirs=[side])
    assert counts["application_dates"] == counts["application_train"] + counts["application_test"]


def test_bureau_fact_uses_junk_dimension_and_close_dates(dw):
    n_cat, n_combos = dw.execute("""
        SELECT (SELECT COUNT(*) FROM dw.dim_bureau_category),
               (SELECT COUNT(*) FROM (SELECT DISTINCT credit_active, credit_type, credit_currency
                                      FROM mart.v_bureau_credit_wide))""").fetchone()
    assert n_cat >= n_combos > 0
    # only closed credits get a close date, never before the open date
    bad = dw.execute("""
        SELECT COUNT(*) FROM mart.v_bureau_credit_wide
        WHERE close_date IS NOT NULL AND (credit_active <> 'Closed' OR close_date < open_date
                                          OR credit_duration_days <> close_date - open_date)""").fetchone()[0]
    assert bad == 0
    # the fixture has one absurd DAYS_ENDDATE_FACT (-42000): it must be dropped, not crash the load
    closed, dated = dw.execute("""
        SELECT COUNT(*), COUNT(close_date_key) FROM mart.v_bureau_credit_wide
        WHERE credit_active = 'Closed'""").fetchone()
    assert dated == closed - 1


def test_failed_run_keeps_previous_warehouse(fixture_dir, tmp_path):
    dw_path = tmp_path / "keep.duckdb"
    run(fixture_dir, dw_path=dw_path)
    before = dw_path.stat().st_mtime_ns
    with pytest.raises(FileNotFoundError):
        run(tmp_path / "does-not-exist", dw_path=dw_path)
    assert dw_path.stat().st_mtime_ns == before
    assert not (tmp_path / "keep.duckdb.building").exists()
    con = duckdb.connect(str(dw_path), read_only=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM dw.fact_loan_application").fetchone()[0] == N_CUSTOMERS
    finally:
        con.close()


def test_failed_quality_gate_keeps_previous_warehouse(fixture_dir, tmp_path):
    dw_path = tmp_path / "gate.duckdb"
    run(fixture_dir, dw_path=dw_path)
    before = dw_path.read_bytes()
    # A source row that fails a DQ check: an outcome recorded before its application.
    src = tmp_path / "src"
    shutil.copytree(fixture_dir, src)
    dates = (src / "application_dates.csv").read_text().splitlines()
    row = next(i for i, line in enumerate(dates[1:], start=1) if not line.endswith(","))  # a train row
    app_id, app_date, _ = dates[row].split(",")
    dates[row] = f"{app_id},{app_date},2000-01-01"
    (src / "application_dates.csv").write_text("\n".join(dates) + "\n")

    with pytest.raises(DataQualityError) as failed:
        run(src, dw_path=dw_path)
    assert "source:outcome_not_before_application" in str(failed.value)
    assert dw_path.read_bytes() == before                    # live warehouse untouched
    assert not (tmp_path / "gate.duckdb.building").exists()
    assert not (tmp_path / "gate.duckdb.building.wal").exists()
    report = json.loads((tmp_path / "dq_report.json").read_text())  # the evidence is kept
    assert report["published"] is False and report["data_quality"]["passed"] is False
    failed_names = {c["name"] for c in report["data_quality"]["checks"] if not c["passed"]}
    assert failed_names == {"source:outcome_not_before_application"}
    assert failed.value.report == report
