"""Simulated source system: application_dates.csv (application and outcome date per applicant).

The Kaggle data has no calendar dates. This module plays the role of an upstream system that would
record them, and writes a source-like file the ETL loads as an optional source table. It lives outside
hcdw.etl on purpose: the ETL only sees a CSV, exactly as it would in production.

Every date is a pure function of (seed, SK_ID_CURR) via SHA-256, so the file is identical on every run
and machine, a date never depends on another row, and dates are uniform inside their window and
independent of every other column: monthly or quarterly trends built on them are noise by construction.

- train (historical) applications: uniform in [anchor - historical_window_days[0], anchor - [1]]
- test (new) applications:         uniform in [anchor - new_window_days[0], anchor - [1]], no outcome
- TARGET = 1: outcome = application + m months, m uniform in default_after_months (1 month = 30 days)
- TARGET = 0: outcome = application + repaid_observed_months months ("repaid as of")
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from hcdw.config import DAYS_PER_MONTH, validate_synthetic_dates
from hcdw.db import sql_literal_path
from hcdw.etl.extract import find_source_file

COLUMNS = ["SK_ID_CURR", "APPLICATION_DATE", "OUTCOME_DATE"]


def _uniform(seed: int, salt: str, ids: np.ndarray, n_values: int) -> np.ndarray:
    """Deterministic integer in [0, n_values) per id (SHA-256, so stable across Python/OS versions)."""
    out = np.empty(len(ids), dtype=np.int64)
    for i, x in enumerate(ids):
        digest = hashlib.sha256(f"{seed}:{salt}:{int(x)}".encode()).digest()
        out[i] = int.from_bytes(digest[:8], "big") % n_values
    return out


def _days_back(seed: int, salt: str, ids: np.ndarray, window: list[int]) -> np.ndarray:
    far, near = window
    return near + _uniform(seed, salt, ids, far - near + 1)


def synth_dates(train: pd.DataFrame, test: pd.DataFrame, cfg: dict, anchor: date) -> pd.DataFrame:
    """train: SK_ID_CURR, TARGET; test: SK_ID_CURR. Returns COLUMNS sorted by SK_ID_CURR, ISO dates."""
    cfg = validate_synthetic_dates(cfg)
    seed = cfg["seed"]
    tr_ids = train["SK_ID_CURR"].to_numpy(dtype=np.int64)
    te_ids = test["SK_ID_CURR"].to_numpy(dtype=np.int64)
    target = pd.to_numeric(train["TARGET"], errors="coerce").to_numpy(dtype=float)

    tr_back = _days_back(seed, "historical", tr_ids, cfg["historical_window_days"])
    te_back = _days_back(seed, "new", te_ids, cfg["new_window_days"])
    lo, hi = cfg["default_after_months"]
    default_months = lo + _uniform(seed, "default", tr_ids, hi - lo + 1)
    months = np.where(target == 1, default_months, cfg["repaid_observed_months"])
    has_outcome = np.isin(target, (0, 1))

    def iso(days_before: int) -> str:
        return (anchor - timedelta(days=int(days_before))).isoformat()

    rows = [
        (i, iso(b), iso(b - m * DAYS_PER_MONTH) if ok else None)
        for i, b, m, ok in zip(tr_ids, tr_back, months, has_outcome)
    ]
    rows += [(i, iso(b), None) for i, b in zip(te_ids, te_back)]
    df = pd.DataFrame(rows, columns=COLUMNS).sort_values("SK_ID_CURR", kind="stable").reset_index(drop=True)
    df["SK_ID_CURR"] = df["SK_ID_CURR"].astype("int64")
    return df


def _read_ids(path: Path, with_target: bool) -> pd.DataFrame:
    con = duckdb.connect()
    try:
        src = f"read_csv({sql_literal_path(path)}, header = true, all_varchar = true)"
        header = {r[0].lower(): r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
        cols = ["SK_ID_CURR"] + (["TARGET"] if with_target else [])
        missing = [c for c in cols if c.lower() not in header]
        if missing:
            raise ValueError(f"{path.name} has no column(s) {missing}")
        sel = ", ".join(f'TRY_CAST("{header[c.lower()]}" AS BIGINT) AS {c}' for c in cols)
        return con.execute(f"SELECT {sel} FROM {src} WHERE {cols[0]} IS NOT NULL").df()
    finally:
        con.close()


def make_application_dates(source_dir: Path, out_path: Path, cfg: dict, anchor: date) -> dict[str, int]:
    """Read application ids from source_dir (Kaggle file names) and write out_path (application_dates.csv)."""
    train_path = find_source_file(Path(source_dir), "application_train")
    if train_path is None:
        raise FileNotFoundError(f"application_train.csv not found in {source_dir}")
    test_path = find_source_file(Path(source_dir), "application_test")
    train = _read_ids(train_path, with_target=True)
    test = _read_ids(test_path, with_target=False) if test_path else pd.DataFrame({"SK_ID_CURR": []})
    df = synth_dates(train, test, cfg, anchor)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Fixed line ending so the bytes are identical on every OS.
    df.to_csv(out_path, index=False, lineterminator="\n")
    return {"applications": len(df), "with_outcome": int(df["OUTCOME_DATE"].notna().sum())}
