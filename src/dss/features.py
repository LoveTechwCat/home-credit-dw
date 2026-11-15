"""Feature matrix for the risk model.

Rule: read from the warehouse (mart.v_application_wide), never from the raw CSVs, so the model and
the dashboard see the same cleaned data. Need a raw column that is not in the warehouse yet? Add it
to src/sql/ddl/01_staging.sql + the transforms.

`gender` is deliberately not a feature (fairness, docs/dss_design.md §6.1); it stays an analysis
dimension only.
"""
from __future__ import annotations

import pandas as pd

NUMERIC_FEATURES = [
    "amt_income", "amt_credit", "amt_annuity", "amt_goods_price",
    "debt_to_income", "credit_to_income", "credit_to_goods", "ext_source_mean",
    "age_years", "years_employed", "cnt_children", "cnt_fam_members",
    "cnt_bureau_active", "cnt_prev_applications", "prev_approval_rate",
    "max_dpd_days", "late_payment_ratio", "region_rating_client",
]
CATEGORICAL_FEATURES = [
    "income_type", "education_type", "family_status", "housing_type",
    "occupation_type", "contract_type", "max_dpd_bucket", "bureau_active_bucket",
]
FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES

# Display names shown in the web app (English UI, docs/dss_design.md §6.1).
FEATURE_LABELS: dict[str, str] = {
    "amt_income": "Annual income",
    "amt_credit": "Credit amount",
    "amt_annuity": "Loan annuity",
    "amt_goods_price": "Goods price",
    "debt_to_income": "Annuity to income ratio",
    "credit_to_income": "Credit to income ratio",
    "credit_to_goods": "Credit to goods price ratio",
    "ext_source_mean": "Average external credit score",
    "age_years": "Age",
    "years_employed": "Years employed",
    "cnt_children": "Number of children",
    "cnt_fam_members": "Family members",
    "cnt_bureau_active": "Active bureau credits",
    "cnt_prev_applications": "Previous Home Credit applications",
    "prev_approval_rate": "Previous approval rate",
    "max_dpd_days": "Worst days past due (history)",
    "late_payment_ratio": "Share of instalments paid late",
    "region_rating_client": "Region rating",
    "income_type": "Employment type",
    "education_type": "Education",
    "family_status": "Family status",
    "housing_type": "Housing",
    "occupation_type": "Occupation",
    "contract_type": "Contract type",
    "max_dpd_bucket": "Max days past due band",
    "bureau_active_bucket": "Active bureau credits band",
}


def load_frame(con, labelled: bool = True) -> pd.DataFrame:
    """labelled=True -> train rows with TARGET; False -> test rows to score. Ordered by application_id."""
    cols = ", ".join(["application_id", "target", *FEATURES])
    df = con.execute(
        f"SELECT {cols} FROM mart.v_application_wide WHERE is_train = ? ORDER BY application_id", [labelled]
    ).df()
    return prepare(df)


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in CATEGORICAL_FEATURES:
        if c in df:
            df[c] = df[c].astype("category")
    return df


def model_matrix(df: pd.DataFrame, categories: dict[str, list[str]]) -> pd.DataFrame:
    """The model's input: FEATURES in order, numerics as float, categoricals on the TRAINING levels.

    Pinning every categorical to the saved levels makes the integer codes LightGBM sees identical at
    train and serve time, whatever subset of levels a frame happens to contain; a level never seen in
    training becomes NaN (LightGBM's missing branch). Missing columns are treated as all-NaN, so a
    hand-typed simulator row with blanks still scores.
    """
    X = pd.DataFrame(index=df.index)
    for c in NUMERIC_FEATURES:
        X[c] = pd.to_numeric(df[c], errors="coerce").astype("float64") if c in df else float("nan")
    for c in CATEGORICAL_FEATURES:
        levels = categories[c]
        values = df[c].astype("object") if c in df else pd.Series(None, index=df.index, dtype="object")
        X[c] = pd.Categorical(values.where(values.isin(levels), None), categories=levels)
    return X[FEATURES]


def category_levels(df: pd.DataFrame) -> dict[str, list[str]]:
    """Sorted non-null levels of each categorical feature (saved with the model)."""
    return {c: sorted(str(v) for v in df[c].dropna().unique()) for c in CATEGORICAL_FEATURES}
