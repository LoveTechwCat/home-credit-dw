"""Train the default-risk model (docs/dss_design.md §6).

    pip install -e ".[ml]"
    hcdw train            # or: python -m dss.train

LightGBM, 5-fold stratified cross-validation:
- every train application gets an out-of-fold (OOF) PD from the fold model that never saw it, so
  grades of train rows are not flattered by in-sample scores;
- an isotonic calibrator is fitted on the OOF raw PD, so a PD of 7% means ~7% observed default rate;
- a final model on all train rows scores test rows and new applications.

Outputs in models/: model.pkl, oof_pd.parquet, metrics.json.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from hcdw.config import ROOT

from dss.features import CATEGORICAL_FEATURES, FEATURE_LABELS, FEATURES, category_levels, load_frame, model_matrix

log = logging.getLogger(__name__)

MODEL_DIR = ROOT / "models"
N_FOLDS = 5
SEED = 42
SHAP_SAMPLE = 20_000  # rows used for the global mean |SHAP| (TreeSHAP on all 307k rows is slow)


def default_params(n_rows: int) -> dict:
    """Fixed LightGBM settings. No class weighting: the calibrated PD must match observed rates.

    min_child_samples scales with the data so the tiny test fixture still grows trees.
    """
    return dict(
        n_estimators=800, learning_rate=0.02, num_leaves=31,
        min_child_samples=int(min(100, max(5, n_rows // 1000))),
        subsample=0.8, subsample_freq=1, colsample_bytree=0.7, reg_lambda=1.0,
        random_state=SEED, verbose=-1,
    )


def _fit(X: pd.DataFrame, y: np.ndarray, params: dict):
    import lightgbm as lgb

    model = lgb.LGBMClassifier(**params)
    model.fit(X, y, categorical_feature=CATEGORICAL_FEATURES)
    return model


def _calibration_table(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> list[dict]:
    """Equal-count bins by PD rank (ties split by order, so every bin has ~n/10 rows)."""
    order = np.argsort(p, kind="stable")
    return [
        {"bin": i, "n": int(len(idx)), "mean_pd": float(p[idx].mean()), "default_rate": float(y[idx].mean())}
        for i, idx in enumerate(np.array_split(order, n_bins), start=1) if len(idx)
    ]


def _global_shap(model, X: pd.DataFrame) -> list[dict]:
    if len(X) > SHAP_SAMPLE:
        X = X.sample(SHAP_SAMPLE, random_state=SEED)
    contrib = model.booster_.predict(X, pred_contrib=True)[:, : len(FEATURES)]  # last column = bias
    mean_abs = np.abs(contrib).mean(axis=0)
    rows = [{"feature": f, "label": FEATURE_LABELS[f], "mean_abs_shap": float(v)} for f, v in zip(FEATURES, mean_abs)]
    return sorted(rows, key=lambda r: r["mean_abs_shap"], reverse=True)


def _write_atomic(path: Path, write) -> None:
    """Write to a side file, then swap, so a running API never loads a half-written artifact."""
    tmp = path.with_name(path.name + ".tmp")
    write(tmp)
    os.replace(tmp, path)


def train(con, model_dir: Path | None = None, params: dict | None = None) -> dict:
    """Train on the labelled rows of the warehouse behind `con`; write artifacts; return metrics."""
    import joblib
    from sklearn.isotonic import IsotonicRegression
    from sklearn.metrics import brier_score_loss, roc_auc_score
    from sklearn.model_selection import StratifiedKFold

    model_dir = Path(model_dir or MODEL_DIR)
    df = load_frame(con, labelled=True)
    if df.empty:
        raise ValueError("No labelled applications in the warehouse; run `hcdw etl` first.")
    categories = category_levels(df)
    X = model_matrix(df, categories)
    y = df["target"].astype(int).to_numpy()
    params = params or default_params(len(df))

    folds = np.full(len(df), -1, dtype=np.int8)
    pd_raw = np.full(len(df), np.nan)
    fold_models = []
    skf = StratifiedKFold(n_splits=N_FOLDS, shuffle=True, random_state=SEED)
    for k, (fit_idx, hold_idx) in enumerate(skf.split(X, y)):
        m = _fit(X.iloc[fit_idx], y[fit_idx], params)
        pd_raw[hold_idx] = m.predict_proba(X.iloc[hold_idx])[:, 1]
        folds[hold_idx] = k
        fold_models.append(m)
        log.info("fold %d: AUC %.4f on %d held-out rows", k, roc_auc_score(y[hold_idx], pd_raw[hold_idx]), len(hold_idx))

    calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(pd_raw, y)
    pd_cal = calibrator.predict(pd_raw)
    log.info("final model on all %d rows ...", len(df))
    model = _fit(X, y, params)

    metrics = {
        "auc_oof": float(roc_auc_score(y, pd_cal)),        # same as mart.kpi_model_auc on the stored pd
        "auc_oof_raw": float(roc_auc_score(y, pd_raw)),    # before calibration (isotonic pools scores into ties)
        "brier": float(brier_score_loss(y, pd_cal)),
        "mean_pd_oof": float(pd_cal.mean()),
        "base_rate": float(y.mean()),
        "n_train": int(len(df)),
        "n_folds": N_FOLDS,
        "calibration": _calibration_table(pd_cal, y),
        "global_shap": _global_shap(model, X),
        "features": FEATURES,
        "params": params,
        "trained_at": datetime.now().isoformat(timespec="seconds"),
    }
    oof = pd.DataFrame({
        "application_id": df["application_id"].astype("int64").to_numpy(),
        "fold": folds,
        "pd_raw": pd_raw,
        "pd": pd_cal,
    })
    bundle = {
        "model": model, "calibrator": calibrator, "categories": categories,
        "features": list(FEATURES), "fold_models": fold_models,
        "params": params, "trained_at": metrics["trained_at"],
    }

    model_dir.mkdir(parents=True, exist_ok=True)
    _write_atomic(model_dir / "model.pkl", lambda p: joblib.dump(bundle, p))
    _write_atomic(model_dir / "oof_pd.parquet", lambda p: oof.to_parquet(p, index=False))
    _write_atomic(model_dir / "metrics.json", lambda p: p.write_text(json.dumps(metrics, indent=2), encoding="utf-8"))
    log.info("AUC(OOF) %.4f, Brier %.4f, mean PD %.4f vs base rate %.4f; saved to %s",
             metrics["auc_oof"], metrics["brier"], metrics["mean_pd_oof"], metrics["base_rate"], model_dir)
    return metrics


def main(dw_path: Path | None = None, model_dir: Path | None = None) -> dict:
    """Train from the local warehouse file (read-only, so it never blocks the dashboard)."""
    from hcdw.db import get_conn

    con = get_conn("local", read_only=True, path=dw_path)
    try:
        return train(con, model_dir=model_dir)
    finally:
        con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    m = main()
    print(f"AUC(OOF) = {m['auc_oof']:.4f}; Brier = {m['brier']:.4f}; mean PD = {m['mean_pd_oof']:.4f}")
