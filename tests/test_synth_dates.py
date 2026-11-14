"""Simulated application_dates source (hcdw.synth): deterministic, inside the configured windows, no trend."""
import dataclasses
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from hcdw.config import get_settings
from hcdw.synth import DAYS_PER_MONTH, make_application_dates, synth_dates

ANCHOR = date.fromisoformat(get_settings().anchor_date)
CFG = get_settings().synthetic_dates


def _frame(n: int = 20000, seed: int = 0) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    ids = np.arange(100001, 100001 + n)
    is_test = rng.random(n) < 0.15
    train = pd.DataFrame({"SK_ID_CURR": ids[~is_test], "TARGET": (rng.random((~is_test).sum()) < 0.08).astype(int)})
    test = pd.DataFrame({"SK_ID_CURR": ids[is_test]})
    return train, test


def _days_back(series: pd.Series) -> pd.Series:
    return series.map(lambda d: (ANCHOR - date.fromisoformat(d)).days)


def test_dates_fall_in_the_configured_windows():
    train, test = _frame()
    out = synth_dates(train, test, CFG, ANCHOR).set_index("SK_ID_CURR")
    assert list(out.columns) == ["APPLICATION_DATE", "OUTCOME_DATE"]
    assert len(out) == len(train) + len(test) and out.index.is_unique
    far, near = CFG["historical_window_days"]
    hist = _days_back(out.loc[train["SK_ID_CURR"], "APPLICATION_DATE"])
    assert hist.between(near, far).all() and {hist.min(), hist.max()} == {near, far}  # both ends reachable
    new_far, new_near = CFG["new_window_days"]
    new = _days_back(out.loc[test["SK_ID_CURR"], "APPLICATION_DATE"])
    assert new.between(new_near, new_far).all()
    assert out.loc[test["SK_ID_CURR"], "OUTCOME_DATE"].isna().all()


def test_outcome_follows_the_target():
    train, test = _frame()
    out = synth_dates(train, test, CFG, ANCHOR).set_index("SK_ID_CURR").loc[train["SK_ID_CURR"]]
    gap = (pd.to_datetime(out["OUTCOME_DATE"]) - pd.to_datetime(out["APPLICATION_DATE"])).dt.days.to_numpy()
    target = train["TARGET"].to_numpy()
    lo, hi = CFG["default_after_months"]
    assert set(gap[target == 1] // DAYS_PER_MONTH) == set(range(lo, hi + 1))
    assert (gap[target == 1] % DAYS_PER_MONTH == 0).all()
    assert (gap[target == 0] == CFG["repaid_observed_months"] * DAYS_PER_MONTH).all()
    assert (pd.to_datetime(out["OUTCOME_DATE"]) <= pd.Timestamp(ANCHOR)).all()  # outcomes are already known


def test_generation_is_deterministic_and_seeded():
    train, test = _frame(3000)
    a = synth_dates(train, test, CFG, ANCHOR)
    b = synth_dates(train.sample(frac=1, random_state=1), test, CFG, ANCHOR)  # row order does not matter
    pd.testing.assert_frame_equal(a, b)
    c = synth_dates(train, test, {**CFG, "seed": CFG["seed"] + 1}, ANCHOR)
    assert (a["APPLICATION_DATE"] != c["APPLICATION_DATE"]).mean() > 0.9
    # a date depends only on its own id, so adding applicants never moves existing dates
    more = synth_dates(pd.concat([train, pd.DataFrame({"SK_ID_CURR": [9_999_999], "TARGET": [1]})]), test,
                       CFG, ANCHOR)
    pd.testing.assert_frame_equal(more[more["SK_ID_CURR"] != 9_999_999].reset_index(drop=True), a)


def test_no_trend_is_injected():
    train, test = _frame(40000)
    out = synth_dates(train, test, CFG, ANCHOR).set_index("SK_ID_CURR").loc[train["SK_ID_CURR"]]
    months = pd.to_datetime(out["APPLICATION_DATE"]).dt.to_period("Q")
    df = pd.DataFrame({"q": months.to_numpy(), "target": train["TARGET"].to_numpy()})
    inner = df[(df["q"] > df["q"].min()) & (df["q"] < df["q"].max())]  # edge quarters are partial
    by_q = inner.groupby("q")["target"].agg(["size", "mean"])
    assert by_q["size"].max() / by_q["size"].min() < 1.15     # uniform volume per full quarter
    overall = df["target"].mean()
    assert (by_q["mean"] - overall).abs().max() < 0.02        # default rate does not drift with time
    back = _days_back(out["APPLICATION_DATE"]).to_numpy()
    assert abs(np.corrcoef(back, df["target"])[0, 1]) < 0.03  # dates independent of the outcome


def test_make_application_dates_writes_a_source_file(fixture_dir, tmp_path):
    out = tmp_path / "a" / "application_dates.csv"
    counts = make_application_dates(fixture_dir, out, CFG, ANCHOR)
    text = out.read_text()
    assert text.splitlines()[0] == "SK_ID_CURR,APPLICATION_DATE,OUTCOME_DATE"
    train = pd.read_csv(fixture_dir / "application_train.csv")
    test = pd.read_csv(fixture_dir / "application_test.csv")
    assert counts == {"applications": len(train) + len(test), "with_outcome": len(train)}
    again = tmp_path / "b.csv"
    make_application_dates(fixture_dir, again, CFG, ANCHOR)
    assert again.read_bytes() == out.read_bytes()
    # the fixture builder ships the very same file
    assert (fixture_dir / "application_dates.csv").read_bytes() == out.read_bytes()
    # test rows: empty OUTCOME_DATE field
    first_test = int(test["SK_ID_CURR"].iloc[0])
    line = next(row for row in text.splitlines() if row.startswith(f"{first_test},"))
    assert line.endswith(",")


@pytest.mark.parametrize("changes", [
    {"historical_window_days": [180, 900]},             # far end must come first
    {"historical_window_days": [900, -1]},
    {"new_window_days": [90]},
    {"default_after_months": [0, 6]},
    {"default_after_months": [6, 1]},
    {"repaid_observed_months": 0},
    {"historical_window_days": [900, 150]},             # a 6-month outcome would land after the snapshot
    {"seed": "abc"},
])
def test_invalid_synthetic_dates_settings_are_rejected(changes):
    s = get_settings()
    bad = dataclasses.replace(s, raw={**s.raw, "synthetic_dates": {**s.raw["synthetic_dates"], **changes}})
    with pytest.raises(ValueError):
        bad.synthetic_dates


def test_anchor_window_example():
    # documented example: the latest historical application plus the longest outcome is the snapshot
    far, near = CFG["historical_window_days"]
    longest = max(CFG["default_after_months"][1], CFG["repaid_observed_months"]) * DAYS_PER_MONTH
    assert ANCHOR - timedelta(days=near) + timedelta(days=longest) <= ANCHOR
