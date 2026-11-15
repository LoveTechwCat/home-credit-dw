"""Batch scoring: PD + rules + A-E grades + policy for every application (docs/dss_design.md §5.3).

    hcdw score            # after `hcdw train`

- Train applications take their stored out-of-fold PD (pd_source 'oof'): grading them with the
  in-sample PD of a model that saw their label would make the grade scale look better than it is.
- Test applications are scored by the model trained on all train rows (pd_source 'model').
- Rules and grading come from dss.rules / dss.grading, the same code the API uses for one application.

Results replace dw.fact_risk_assessment and dw.fact_rule_hit; the DQ checks then run on the result.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from hcdw.config import GRADES, Settings, get_settings

from dss.features import FEATURES
from dss.rules import SEGMENT_INPUTS

log = logging.getLogger(__name__)

ASSESSMENT_COLUMNS = [
    "application_id", "pd", "pd_source", "score_grade", "rule_grade", "final_grade", "grade_source",
    "downgraded", "n_rule_hits", "segment_key", "segment_rate", "decision", "max_credit_to_income",
    "recommended_credit", "scored_at",
]
HIT_COLUMNS = ["application_id", "rule_code", "rate", "grade", "segment_key", "rate_key"]


def _load_frame(con, settings: Settings) -> pd.DataFrame:
    """Model features + every column a rule reads, for all applications."""
    requires = [c for r in settings.dss["rules"] for c in r.get("requires", [])]
    cols = list(dict.fromkeys(["application_id", "is_train", "target", *FEATURES, *SEGMENT_INPUTS, *requires]))
    return con.execute(f"SELECT {', '.join(cols)} FROM mart.v_application_wide ORDER BY application_id").df()


def _pd_column(frame: pd.DataFrame, model_dir: Path | None) -> tuple[pd.Series, pd.Series]:
    """(pd, pd_source) per row: stored OOF PD for train rows, the full model for the rest."""
    from dss import predict
    from dss.train import MODEL_DIR

    predict.load_model(model_dir)  # fail fast with "run hcdw train" when there is no model
    oof_path = Path(model_dir or MODEL_DIR) / "oof_pd.parquet"
    if not oof_path.exists():
        raise FileNotFoundError(f"No {oof_path}. Run `hcdw train` first.")
    oof = pd.read_parquet(oof_path, columns=["application_id", "pd"]).set_index("application_id")["pd"]

    is_train = frame["is_train"].astype(bool)
    train_ids = frame.loc[is_train, "application_id"]
    if len(train_ids) != len(oof) or not train_ids.isin(oof.index).all():
        raise RuntimeError(
            f"The model was trained on other data ({len(oof)} OOF rows vs {len(train_ids)} train applications "
            "in the warehouse). Run `hcdw train` again, then `hcdw score`.")

    pd_ = pd.Series(float("nan"), index=frame.index, name="pd")
    pd_[is_train] = train_ids.map(oof).to_numpy()
    if (~is_train).any():
        pd_[~is_train] = predict.score(frame.loc[~is_train], model_dir=model_dir).to_numpy()
    source = pd.Series("model", index=frame.index).where(~is_train, "oof")
    return pd_, source


def _grade_table(df: pd.DataFrame, col: str) -> list[dict]:
    """n / share / observed default rate per grade (train rows); the rule grade adds a `None` row."""
    n_all = len(df)
    grades = [*GRADES, None] if col == "rule_grade" else list(GRADES)
    out = []
    for g in grades:
        part = df[df[col].isna()] if g is None else df[df[col] == g]
        out.append({"grade": g, "n": int(len(part)), "share": len(part) / n_all if n_all else 0.0,
                    "default_rate": float(part["target"].mean()) if len(part) else None})
    return out


def _summary(ra: pd.DataFrame, frame: pd.DataFrame) -> dict:
    df = ra.merge(frame[["application_id", "is_train", "target"]], on="application_id")
    train = df[df["is_train"]]
    test = df[~df["is_train"]]

    def rate(d: pd.DataFrame, decision: str) -> float | None:
        return float((d["decision"] == decision).mean()) if len(d) else None

    approved = train[train["decision"] == "approve"]
    return {
        "n_scored": int(len(df)), "n_train": int(len(train)), "n_test": int(len(test)),
        "mean_pd_train": float(train["pd"].mean()) if len(train) else None,
        "mean_pd_test": float(test["pd"].mean()) if len(test) else None,
        "train_default_rate": float(train["target"].mean()) if len(train) else None,
        # portfolio KPIs on train rows (the ones with an observed outcome)
        "downgraded_share": float(train["downgraded"].mean()) if len(train) else None,
        "with_rule_grade_share": float(train["rule_grade"].notna().mean()) if len(train) else None,
        "approval_rate": rate(train, "approve"), "referral_rate": rate(train, "review"),
        "reject_rate": rate(train, "reject"),
        "approved_default_rate": float(approved["target"].mean()) if len(approved) else None,
        "test_approval_rate": rate(test, "approve"),
        "grades_train": {k: _grade_table(train, f"{k}_grade") for k in ("score", "rule", "final")},
    }


def _write(con, ra: pd.DataFrame, hits: pd.DataFrame) -> None:
    """Replace both fact tables.

    DuckDB checks a foreign key against the committed index, so the child rows are deleted and
    committed first; assessments and hits are then swapped in one transaction. Everything is
    computed before this point, so the window with an empty fact_rule_hit is only the second step.
    If that step fails anyway, the old assessments are deleted too: a warehouse with no scores is
    reported as "not scored" (API 503), while old assessments without their rule hits would be wrong.
    """
    con.execute("BEGIN TRANSACTION")
    con.execute("DELETE FROM dw.fact_rule_hit")
    con.execute("COMMIT")
    con.register("_dss_ra", ra)
    con.register("_dss_hits", hits)
    try:
        con.execute("BEGIN TRANSACTION")
        con.execute("DELETE FROM dw.fact_risk_assessment")
        con.execute(f"INSERT INTO dw.fact_risk_assessment ({', '.join(ASSESSMENT_COLUMNS)}) "
                    f"SELECT {', '.join(ASSESSMENT_COLUMNS)} FROM _dss_ra")
        con.execute(f"INSERT INTO dw.fact_rule_hit ({', '.join(HIT_COLUMNS)}) "
                    f"SELECT {', '.join(HIT_COLUMNS)} FROM _dss_hits")
        con.execute("COMMIT")
    except BaseException as exc:
        try:
            con.execute("ROLLBACK")
        except Exception:  # a failed COMMIT has already rolled back
            pass
        con.execute("DELETE FROM dw.fact_risk_assessment")
        if not isinstance(exc, Exception):
            raise
        raise RuntimeError(f"Writing the scores failed ({exc}); the warehouse is now unscored. "
                           "Run `hcdw score` again.") from exc
    finally:
        con.unregister("_dss_ra")
        con.unregister("_dss_hits")
    con.execute("CHECKPOINT")


def score_all(con, model_dir: Path | None = None, settings: Settings | None = None) -> dict:
    """Score, grade and store every application in the warehouse behind `con` (opened writable).

    Returns a summary (counts, grade distribution and policy rates on train rows, timings) plus the
    data-quality result: `dq_passed` and `dq_checks`.
    """
    from dss.grading import assess, load_policy
    from dss.rules import evaluate, lookup_segments
    from hcdw.etl.quality import run_checks

    settings = settings or get_settings()
    policy = load_policy(settings)
    timings: dict[str, float] = {}
    t = time.perf_counter()

    frame = _load_frame(con, settings)
    pd_, source = _pd_column(frame, model_dir)
    timings["pd"] = round(time.perf_counter() - t, 2)

    t = time.perf_counter()
    rule_hits = evaluate(con, frame, settings=settings)
    segments = lookup_segments(con, frame, settings=settings)
    timings["rules"] = round(time.perf_counter() - t, 2)

    t = time.perf_counter()
    scored_at = datetime.now().replace(microsecond=0)
    ra_rows, hit_rows = [], []
    for app_id, p, src, amt_credit, cti in zip(frame["application_id"], pd_, source,
                                               frame["amt_credit"], frame["credit_to_income"]):
        app_id = int(app_id)
        a = assess(float(p), rule_hits.get(app_id, []), policy,
                   amt_credit=float(amt_credit), credit_to_income=float(cti))
        seg = segments.get(app_id)
        ra_rows.append((app_id, a.pd, src, a.score_grade, a.rule_grade, a.final_grade, a.grade_source,
                        a.downgraded, a.n_rule_hits, seg.segment_key if seg else None,
                        seg.rate if seg else None, a.decision, a.max_credit_to_income,
                        a.recommended_credit, scored_at))
        hit_rows += [(app_id, h.code, h.rate, h.grade, h.segment_key, h.rate_key) for h in a.hits]
    ra = pd.DataFrame(ra_rows, columns=ASSESSMENT_COLUMNS)
    hits = pd.DataFrame(hit_rows, columns=HIT_COLUMNS).astype({"application_id": "int64", "rate": "float64"})
    timings["grade"] = round(time.perf_counter() - t, 2)

    t = time.perf_counter()
    _write(con, ra, hits)
    timings["write"] = round(time.perf_counter() - t, 2)

    t = time.perf_counter()
    dq = run_checks(con, settings)
    timings["dq"] = round(time.perf_counter() - t, 2)

    summary = _summary(ra, frame)
    summary.update({"n_rule_hits": int(len(hits)), "timings_sec": timings,
                    "dq_passed": dq["passed"], "dq_checks": dq["checks"]})
    log.info("scored %d applications (%d rule hits); DQ %s", len(ra), len(hits), "PASS" if dq["passed"] else "FAIL")
    return summary
