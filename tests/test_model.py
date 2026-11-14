"""Risk model + batch scoring on the synthetic fixture warehouse (trained into tmp_path)."""
import dataclasses
import json
import shutil
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

pytest.importorskip("lightgbm")
pytest.importorskip("sklearn")

from dss import predict  # noqa: E402
from dss.features import CATEGORICAL_FEATURES, FEATURE_LABELS, FEATURES, load_frame  # noqa: E402
from dss.train import train  # noqa: E402
from hcdw.config import get_settings  # noqa: E402
from hcdw.etl.load import build_marts  # noqa: E402


@pytest.fixture(scope="module")
def model_dir(built, tmp_path_factory):
    d = tmp_path_factory.mktemp("models")
    con = duckdb.connect(str(built[0]), read_only=True)
    try:
        train(con, model_dir=d)
    finally:
        con.close()
    return d


@pytest.fixture(scope="module")
def train_frame(built):
    con = duckdb.connect(str(built[0]), read_only=True)
    try:
        return load_frame(con, labelled=True)
    finally:
        con.close()


@pytest.fixture(scope="module")
def scored(built, model_dir, tmp_path_factory):
    """score_all on a COPY of the fixture warehouse, so other tests keep an unscored one.

    Segments keep their own rates (min n = 5) so the tiny fixture has triggered segment-rule hits too.
    """
    from dss.score import score_all

    path = tmp_path_factory.mktemp("scored") / "scored.duckdb"
    shutil.copy(built[0], path)
    s = get_settings()
    settings = dataclasses.replace(s, raw={**s.raw, "dss": {**s.raw["dss"], "segment_min_n": 5}})
    con = duckdb.connect(str(path))
    try:
        build_marts(con, settings)
        score_all(con, model_dir=model_dir, settings=settings)
        # re-scoring replaces the previous rows (FK child rows must be deleted first)
        summary = score_all(con, model_dir=model_dir, settings=settings)
    finally:
        con.close()
    return path, summary


def test_gender_is_not_a_model_feature(model_dir):
    assert "gender" not in FEATURES
    bundle = predict.load_model(model_dir)
    assert "gender" not in bundle["features"]
    assert "gender" not in bundle["model"].feature_name_
    assert all("gender" not in m.feature_name_ for m in bundle["fold_models"])
    assert set(FEATURE_LABELS) == set(FEATURES)


def test_artifacts_follow_the_contract(model_dir, train_frame):
    bundle = predict.load_model(model_dir)
    assert {"model", "calibrator", "categories", "features", "fold_models"} <= set(bundle)
    assert len(bundle["fold_models"]) == 5
    assert set(bundle["categories"]) == set(CATEGORICAL_FEATURES)

    oof = pd.read_parquet(model_dir / "oof_pd.parquet")
    assert list(oof.columns) == ["application_id", "fold", "pd_raw", "pd"]
    assert sorted(oof["application_id"]) == sorted(train_frame["application_id"])
    assert set(oof["fold"]) == set(range(5))
    assert oof["pd"].between(0, 1).all()

    metrics = json.loads((model_dir / "metrics.json").read_text())
    assert {"auc_oof", "brier", "calibration", "global_shap", "n_train", "mean_pd_oof"} <= set(metrics)
    assert metrics["n_train"] == len(train_frame)
    assert sum(b["n"] for b in metrics["calibration"]) == len(train_frame)
    assert {g["feature"] for g in metrics["global_shap"]} == set(FEATURES)
    # AUC is reported on the calibrated PD, the value mart.kpi_model_auc computes from the stored pd.
    y = train_frame.set_index("application_id")["target"].loc[oof["application_id"]].to_numpy()
    assert metrics["auc_oof"] == pytest.approx(_auc(oof["pd"].to_numpy(), y), abs=1e-9)


def _auc(p: np.ndarray, y: np.ndarray) -> float:
    from sklearn.metrics import roc_auc_score

    return float(roc_auc_score(y, p))


def test_score_single_manual_row_with_unseen_category(model_dir):
    row = {c: None for c in FEATURES}
    row.update({"amt_income": 120000.0, "amt_credit": 300000.0, "income_type": "Martian",
                "contract_type": "Cash loans", "ext_source_mean": 0.5})
    p = predict.score(pd.DataFrame([row]), model_dir=model_dir)
    assert len(p) == 1 and 0.0 <= p.iloc[0] <= 1.0
    # an unseen level is scored exactly like a missing one
    blank = predict.score(pd.DataFrame([{**row, "income_type": None}]), model_dir=model_dir)
    assert p.iloc[0] == blank.iloc[0]


@pytest.mark.parametrize("folds", [[1.5, 0, 0], [0, 7, 0]])
def test_invalid_fold_values_are_rejected(model_dir, train_frame, folds):
    with pytest.raises(ValueError):
        predict.score(train_frame.head(3), folds=folds, model_dir=model_dir)


def test_explain_returns_labelled_top_factors(model_dir, train_frame):
    out = predict.explain(train_frame.head(8), top_n=5, model_dir=model_dir)
    assert len(out) == 8
    assert any(e["up"] or e["down"] for e in out)
    for e in out:
        assert len(e["up"]) <= 5 and len(e["down"]) <= 5
        assert all(f["shap"] > 0 for f in e["up"]) and all(f["shap"] < 0 for f in e["down"])
        assert [f["shap"] for f in e["up"]] == sorted((f["shap"] for f in e["up"]), reverse=True)
        assert [f["shap"] for f in e["down"]] == sorted(f["shap"] for f in e["down"])
        for f in e["up"] + e["down"]:
            assert f["label"] == FEATURE_LABELS[f["feature"]]
            assert set(f) == {"feature", "label", "value", "shap"}


def test_fold_aware_score_reproduces_stored_oof_pd(model_dir, train_frame):
    oof = pd.read_parquet(model_dir / "oof_pd.parquet").set_index("application_id")
    folds = train_frame["application_id"].map(oof["fold"])
    p = predict.score(train_frame, folds=folds, model_dir=model_dir)
    expected = train_frame["application_id"].map(oof["pd"])
    assert np.allclose(p.to_numpy(), expected.to_numpy(), rtol=0, atol=1e-9)
    # one row at a time (the what-if path) gives the same number
    one = train_frame.iloc[[3]]
    assert abs(predict.score(one, folds=folds.iloc[[3]], model_dir=model_dir).iloc[0] - expected.iloc[3]) < 1e-9


def test_score_all_grades_every_application(scored, built):
    path, summary = scored
    con = duckdb.connect(str(path), read_only=True)
    try:
        n_apps, n_scored, n_final = con.execute("""
            SELECT (SELECT COUNT(*) FROM dw.fact_loan_application), COUNT(*), COUNT(final_grade)
            FROM dw.fact_risk_assessment""").fetchone()
        sources = dict(con.execute("""
            SELECT f.is_train, LIST(DISTINCT a.pd_source) FROM dw.fact_risk_assessment a
            JOIN dw.fact_loan_application f USING (application_id) GROUP BY 1""").fetchall())
        worse = con.execute("SELECT COUNT(*) FROM dw.fact_risk_assessment WHERE final_grade < score_grade").fetchone()[0]
        groups = [r[0] for r in con.execute("""
            SELECT DISTINCT d.rule_group FROM dw.fact_rule_hit h JOIN dw.dim_rule d USING (rule_code)""").fetchall()]
        n_hits = con.execute("SELECT COUNT(*) FROM dw.fact_rule_hit").fetchone()[0]
    finally:
        con.close()
    assert n_scored == n_final == n_apps == summary["n_scored"]
    assert sources[True] == ["oof"] and sources[False] == ["model"]
    assert worse == 0
    assert summary["dq_passed"] is True
    # both kinds of rule hits were written (FKs and CHECKs held, DQ re-derived the rule grades)
    assert {"segment", "business"} <= set(groups)
    assert summary["n_rule_hits"] == n_hits > 0


def test_score_all_uses_out_of_fold_pd_for_train_rows(scored, model_dir):
    path, _ = scored
    con = duckdb.connect(str(path), read_only=True)
    try:
        stored = con.execute("""
            SELECT a.application_id, a.pd FROM dw.fact_risk_assessment a
            JOIN dw.fact_loan_application f USING (application_id) WHERE f.is_train""").df()
    finally:
        con.close()
    oof = pd.read_parquet(model_dir / "oof_pd.parquet").set_index("application_id")["pd"]
    assert len(stored) == len(oof)
    assert np.allclose(stored["pd"].to_numpy(), stored["application_id"].map(oof).to_numpy(), rtol=0, atol=1e-12)


def test_failed_write_leaves_the_warehouse_unscored(scored, tmp_path):
    from dss.score import _write

    path = tmp_path / "broken.duckdb"
    shutil.copy(scored[0], path)
    con = duckdb.connect(str(path))
    try:
        ra = con.execute("SELECT * FROM dw.fact_risk_assessment").df()
        hits = con.execute("SELECT * FROM dw.fact_rule_hit").df()
        assert len(ra) and len(hits)
        ra.loc[0, "pd"] = 2.0  # violates CHECK (pd BETWEEN 0 AND 1) inside the swap transaction
        with pytest.raises(RuntimeError, match="hcdw score"):
            _write(con, ra, hits)
        counts = con.execute("""SELECT (SELECT COUNT(*) FROM dw.fact_risk_assessment),
                                       (SELECT COUNT(*) FROM dw.fact_rule_hit)""").fetchone()
    finally:
        con.close()
    assert counts == (0, 0)  # cleanly "not scored", never old assessments without their hits


# --- CLI -------------------------------------------------------------------------------------------

@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    """Point the CLI's settings at a warehouse path under tmp_path (DW_PATH) for one test."""
    from hcdw.config import get_settings as gs

    def use(dw_path):
        monkeypatch.setenv("DW_PATH", str(dw_path))
        monkeypatch.setenv("DW_TARGET", "local")
        gs.cache_clear()

    yield use
    monkeypatch.undo()
    gs.cache_clear()


def _invoke(*args):
    from typer.testing import CliRunner

    from hcdw.cli import app

    return CliRunner().invoke(app, list(args))


def test_cli_score_without_warehouse_exits_cleanly(cli_env, tmp_path):
    dw = tmp_path / "missing.duckdb"
    cli_env(dw)
    result = _invoke("score")
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)  # no traceback
    assert "hcdw etl" in result.output
    assert not dw.exists() and not Path(f"{dw}.wal").exists()  # no empty warehouse left behind


def test_cli_score_with_stale_model_exits_cleanly(cli_env, built, model_dir, tmp_path, monkeypatch):
    import dss.train

    stale = tmp_path / "models"
    shutil.copytree(model_dir, stale)
    oof = pd.read_parquet(stale / "oof_pd.parquet")
    oof.iloc[1:].to_parquet(stale / "oof_pd.parquet", index=False)  # one train row has no OOF PD
    monkeypatch.setattr(dss.train, "MODEL_DIR", stale)
    monkeypatch.setattr(predict, "MODEL_DIR", stale)
    dw = tmp_path / "hc.duckdb"
    shutil.copy(built[0], dw)
    cli_env(dw)
    result = _invoke("score")
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
    assert "hcdw train" in result.output
