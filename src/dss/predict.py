"""Load the trained model, score applications (calibrated PD) and explain them (SHAP).

Train applications can be re-scored exactly as stored in models/oof_pd.parquet: pass their `folds`
and each row goes to the fold model that did not see it (docs/dss_design.md §6.2). Rows without a
fold (test rows, simulator input) use the model trained on all train rows.
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from dss.features import FEATURE_LABELS, FEATURES, model_matrix
from dss.train import MODEL_DIR


def _model_path(model_dir: Path | None) -> Path:
    return Path(model_dir or MODEL_DIR) / "model.pkl"


def model_available(model_dir: Path | None = None) -> bool:
    return _model_path(model_dir).exists()


def load_model(model_dir: Path | None = None) -> dict:
    """The model.pkl bundle; reloaded automatically after `hcdw train` rewrites the file."""
    path = _model_path(model_dir)
    if not path.exists():
        raise FileNotFoundError(f"No model at {path}. Run `hcdw train` first.")
    return _load(str(path.resolve()), path.stat().st_mtime_ns)


@lru_cache(maxsize=2)
def _load(path: str, mtime_ns: int) -> dict:
    import joblib

    bundle = joblib.load(path)
    if not isinstance(bundle, dict) or "fold_models" not in bundle:
        raise ValueError(f"{path} is an old-format model. Run `hcdw train` again.")
    return bundle


def _routes(df: pd.DataFrame, folds, bundle: dict):
    """(model, row positions) pairs: fold k -> fold_models[k]; no fold (NaN/None/<0) -> full model.

    `folds` is a Series aligned on df.index, or a sequence in row order.
    """
    n = len(df)
    if folds is None:
        yield bundle["model"], np.arange(n)
        return
    f = folds.reindex(df.index) if isinstance(folds, pd.Series) else pd.Series(list(folds), index=df.index)
    f = pd.to_numeric(f, errors="coerce").to_numpy(dtype="float64")
    n_folds = len(bundle["fold_models"])
    given = ~np.isnan(f)
    bad = given & ((f >= n_folds) | (f != np.floor(f)))
    if bad.any():
        raise ValueError(f"fold must be an integer 0..{n_folds - 1} (or missing), got {sorted(set(f[bad]))}")
    use_full = np.isnan(f) | (f < 0)
    if use_full.any():
        yield bundle["model"], np.flatnonzero(use_full)
    for k in range(n_folds):
        pos = np.flatnonzero(f == k)
        if len(pos):
            yield bundle["fold_models"][k], pos


def score(df: pd.DataFrame, folds=None, *, model_dir: Path | None = None) -> pd.Series:
    """Calibrated probability of default per row (columns = dss.features.FEATURES; missing -> NaN)."""
    bundle = load_model(model_dir)
    X = model_matrix(df, bundle["categories"])
    raw = np.empty(len(X))
    for model, pos in _routes(df, folds, bundle):
        raw[pos] = model.predict_proba(X.iloc[pos])[:, 1]
    return pd.Series(bundle["calibrator"].predict(raw), index=df.index, name="pd")


def _json_value(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if isinstance(v, np.generic):
        v = v.item()
        return None if isinstance(v, float) and math.isnan(v) else v
    return v


def explain(df: pd.DataFrame, top_n: int = 5, folds=None, *, model_dir: Path | None = None) -> list[dict]:
    """Per row: {"up": factors raising risk, "down": factors lowering it}, each at most top_n long.

    SHAP values come from LightGBM's TreeSHAP (pred_contrib) on the raw model, in log-odds; `up` is
    sorted by SHAP descending, `down` ascending (strongest first). `value` is what the model saw
    (an unseen category shows as null).
    """
    bundle = load_model(model_dir)
    X = model_matrix(df, bundle["categories"])
    contrib = np.zeros((len(X), len(FEATURES)))
    for model, pos in _routes(df, folds, bundle):
        contrib[pos] = model.booster_.predict(X.iloc[pos], pred_contrib=True)[:, : len(FEATURES)]

    out = []
    for i in range(len(X)):
        row = X.iloc[i]
        factors = [
            {"feature": f, "label": FEATURE_LABELS[f], "value": _json_value(row[f]), "shap": float(s)}
            for f, s in zip(FEATURES, contrib[i])
        ]
        up = sorted((x for x in factors if x["shap"] > 0), key=lambda x: x["shap"], reverse=True)[:top_n]
        down = sorted((x for x in factors if x["shap"] < 0), key=lambda x: x["shap"])[:top_n]
        out.append({"up": up, "down": down})
    return out
