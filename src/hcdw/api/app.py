"""FastAPI application: JSON endpoints under /api, plus the built React app (frontend/dist) at /.

Run:  hcdw serve            (production: serves the built frontend too)
      hcdw serve --reload   (backend development; run `npm run dev` in frontend/ for the UI)

DSS endpoints (assessment, what-if, simulator, grades, policy simulation, global SHAP) follow the
contract in docs/dss_design.md section 7 and reuse dss.predict / dss.rules / dss.grading / dss.policy.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from dss import grading, predict, rules
from dss import policy as dss_policy
from dss.features import FEATURES
from dss.train import MODEL_DIR
from hcdw.api.cube import HIERARCHIES, Cube, CubeQuery
from hcdw.api.explore import ApplicationsQuery, CohortQuery, Explorer
from hcdw.api.queries import (CREDIT_HISTORY_STATUSES, DIMENSIONS, FILTER_DIMS, GRADE_SCOPES, MEASURES, Filters,
                              Queries, QueryError, Warehouse, make_filters)
from hcdw.config import GRADES, ROOT, get_settings

log = logging.getLogger(__name__)

FRONTEND_DIST = ROOT / "frontend" / "dist"
RESERVED = {"row", "col", "measure", "grain", "levels", "x", "y"}


def parse_filters(request: Request) -> Filters:
    """?age_bucket=25-34&age_bucket=35-44&gender=F -> filters tuple. Unknown keys -> 400."""
    raw: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        if key in RESERVED:
            continue
        if key not in DIMENSIONS:
            raise HTTPException(400, f"Unknown filter '{key}'")
        raw.setdefault(key, []).append(value)
    return make_filters(raw)


# ---- DSS: request models ----------------------------------------------------------------------
# Bounds keep every derived number finite (no inf/NaN, hence no null in a numeric response field):
# amounts in [1, 1e9] make every ratio at most 1e9.
MAX_AMOUNT = 1e9
MAX_COUNT = 100
MAX_DPD_DAYS = 100_000


class _Input(BaseModel):
    """Strict, finite input: unknown keys, inf/NaN, bools-as-numbers and numbers-as-strings are refused.

    A blank string counts as missing (forms send "" for an empty field).
    """
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_missing(cls, v):
        return None if isinstance(v, str) and not v.strip() else v


class SimulatorInput(_Input):
    """A hand-typed new application (docs/dss_design.md 7.3). Only the two amounts are required.

    Ranges follow the segment rule's domain (age band edges, region ratings, history statuses), so an
    out-of-domain value is refused instead of silently missing every segment.
    """
    amt_income: float = Field(ge=1, le=MAX_AMOUNT)
    amt_credit: float = Field(gt=0, le=MAX_AMOUNT)
    amt_annuity: float | None = Field(None, ge=0, le=MAX_AMOUNT)
    age_years: float | None = Field(None, ge=18, lt=120)
    years_employed: float | None = Field(None, ge=0, le=80)
    ext_source_mean: float | None = Field(None, ge=0, le=1)
    max_dpd_days: int | None = Field(None, ge=0, le=MAX_DPD_DAYS)
    cnt_bureau_active: int | None = Field(None, ge=0, le=MAX_COUNT)
    has_current_overdue: bool | None = None
    income_type: str | None = None
    contract_type: str | None = None
    credit_history_status: Literal["No bureau", "Clean history", "Current overdue"] | None = None
    region_rating_client: Literal[1, 2, 3] | None = None
    prev_approval_rate: float | None = Field(None, ge=0, le=1)
    late_payment_ratio: float | None = Field(None, ge=0, le=1)
    amt_goods_price: float | None = Field(None, ge=1, le=MAX_AMOUNT)
    cnt_children: int | None = Field(None, ge=0, le=MAX_COUNT)
    cnt_fam_members: int | None = Field(None, ge=0, le=MAX_COUNT)
    cnt_prev_applications: int | None = Field(None, ge=0, le=MAX_COUNT)
    education_type: str | None = None
    family_status: str | None = None
    housing_type: str | None = None
    occupation_type: str | None = None


class WhatIfOverrides(_Input):
    """Fields a what-if may change: the application's terms and profile, never its credit history,
    external score, age, gender or the derived ratios (docs/dss_design.md 7.2)."""
    amt_income: float | None = Field(None, ge=1, le=MAX_AMOUNT)
    amt_credit: float | None = Field(None, gt=0, le=MAX_AMOUNT)
    amt_annuity: float | None = Field(None, ge=0, le=MAX_AMOUNT)
    amt_goods_price: float | None = Field(None, ge=1, le=MAX_AMOUNT)
    contract_type: str | None = None
    income_type: str | None = None
    occupation_type: str | None = None
    education_type: str | None = None
    family_status: str | None = None
    housing_type: str | None = None
    cnt_children: int | None = Field(None, ge=0, le=MAX_COUNT)
    cnt_fam_members: int | None = Field(None, ge=0, le=MAX_COUNT)
    years_employed: float | None = Field(None, ge=0, le=80)
    region_rating_client: Literal[1, 2, 3] | None = None


class WhatIfRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: int
    overrides: dict[str, Any] = Field(default_factory=dict)


class GradePolicyInput(BaseModel):
    """One grade of a /api/policy/simulate request (checked here: dss.grading ignores unknown keys)."""
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    decision: str
    max_credit_to_income: float = Field(ge=0, le=MAX_COUNT)


SIMULATOR_FIELDS = list(SimulatorInput.model_fields)
SIMULATOR_REQUIRED = ["amt_income", "amt_credit"]
WHAT_IF_FIELDS = list(WhatIfOverrides.model_fields)
# what-if overrides that may be set to null (they can be missing in the data too)
WHAT_IF_NULLABLE = {"amt_annuity", "amt_goods_price", "occupation_type", "years_employed", "cnt_fam_members"}
CATEGORICAL_INPUTS = ("income_type", "contract_type", "education_type", "family_status", "housing_type",
                      "occupation_type")
AMOUNTS = {"amt_income", "amt_credit", "amt_annuity", "amt_goods_price"}
POLICY_KEYS = {"thresholds", "policy", "grade_type", "rules"}
# 503 details are shown as-is by the web app: one sentence each.
NO_MODEL = ("No trained risk model yet: stop `hcdw serve`, run `hcdw etl --score` (or `hcdw train` and "
            "`hcdw score`), then start it again.")
NOT_SCORED = ("The warehouse has no stored scores yet: stop `hcdw serve`, run `hcdw etl --score` "
              "(or `hcdw score`), then start it again.")
STALE_SCORES = ("The model is newer than the stored scores: stop `hcdw serve`, run `hcdw score`, "
                "then start it again.")
OTHER_DATA = ("The model was trained on other data than this warehouse: stop `hcdw serve`, run "
              "`hcdw etl --score` (or `hcdw train` and `hcdw score`), then start it again.")


def _json_safe(value):
    """Non-finite floats as text (inf / nan), recursively: JSON cannot carry them as numbers."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def check_policy_entries(policy: Any) -> None:
    """Each grade of a policy request is exactly {decision, max_credit_to_income}, finite and bounded."""
    if policy is None:
        return
    if not isinstance(policy, dict):
        raise HTTPException(400, "Invalid policy: policy must be an object keyed by grade.")
    for grade, entry in policy.items():
        try:
            GradePolicyInput.model_validate(entry)
        except ValidationError as exc:
            raise HTTPException(400, f"Invalid policy for grade {grade}: {_validation_message(exc)}") from exc


# Applicant facts shown to the officer with every assessment (and the what-if form's current values).
PROFILE_FIELDS = [
    "age_years", "income_type", "occupation_type", "education_type", "family_status", "housing_type",
    "contract_type", "region_rating_client", "credit_history_status", "ext_source_mean", "years_employed",
    "cnt_children", "cnt_fam_members", "amt_income", "amt_credit", "amt_annuity", "amt_goods_price",
    "cnt_bureau_active", "max_dpd_days", "late_payment_ratio", "prev_approval_rate", "has_current_overdue",
    "application_date", "outcome_date", "months_to_outcome",
]
_PROFILE_INTS = {"region_rating_client", "cnt_children", "cnt_fam_members", "cnt_bureau_active", "max_dpd_days",
                 "months_to_outcome"}
_PROFILE_DATES = {"application_date", "outcome_date"}


def profile_of(row) -> dict:
    """PROFILE_FIELDS of a warehouse row (Series) or simulator record (dict): JSON-native, null when missing.

    Dates are ISO strings; a hand-typed simulator record has none, so they are null there.
    """
    out = {}
    for k in PROFILE_FIELDS:
        v = row.get(k)
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            out[k] = None
            continue
        if k in _PROFILE_DATES:  # ISO date; checked before .item(), which turns datetime64 into an int
            out[k] = pd.Timestamp(v).date().isoformat()
            continue
        v = v.item() if hasattr(v, "item") else v
        if k == "has_current_overdue":
            out[k] = bool(v)
        elif isinstance(v, str):
            out[k] = v
        elif k in _PROFILE_INTS:
            out[k] = int(v)
        else:
            out[k] = float(v) if math.isfinite(float(v)) else None
    return out


def finite_json(value):
    """numpy scalars as Python values and non-finite floats as null, recursively; keeps every key."""
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: finite_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_json(v) for v in value]
    return value


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'body'}: {e['msg']}" for e in exc.errors())


def ratios(amt_income: float, amt_credit: float, amt_annuity: float | None, amt_goods_price: float | None,
           income_cap: float | None) -> dict[str, float | None]:
    """The ETL's R7 ratios (hcdw.etl.clean): amounts over the income capped at the outlier cap."""
    income = float(amt_income) if income_cap is None else min(float(amt_income), float(income_cap))
    return {
        "debt_to_income": None if amt_annuity is None else float(amt_annuity) / income,
        "credit_to_income": grading.credit_to_income(amt_credit, amt_income, income_cap),
        "credit_to_goods": float(amt_credit) / float(amt_goods_price) if amt_goods_price else None,
    }


def max_dpd_bucket(days: int | None) -> str | None:
    """Same bands as staging.agg_credit_profile."""
    if days is None:
        return None
    return "No DPD" if days == 0 else "1-30" if days <= 30 else "31-60" if days <= 60 else "61-90" if days <= 90 \
        else "90+"


def derive_history(values: dict) -> dict:
    """Fill the credit-history fields that follow from the others (docs/dss_design.md 7.3); 400 on conflicts."""
    status, overdue, cnt = values["credit_history_status"], values["has_current_overdue"], values["cnt_bureau_active"]
    if status == "No bureau" and cnt:
        raise HTTPException(400, f"credit_history_status 'No bureau' conflicts with cnt_bureau_active = {cnt}: "
                                 "an applicant without a bureau record has no active bureau credits.")
    if overdue is not None and status is not None and overdue != (status == "Current overdue"):
        raise HTTPException(400, f"has_current_overdue = {str(overdue).lower()} conflicts with "
                                 f"credit_history_status '{status}'.")
    if overdue is None and status is not None:
        overdue = status == "Current overdue"
    if overdue and status is None:
        status = "Current overdue"
    if status == "No bureau":
        cnt, bucket = 0, "No bureau record"
    elif cnt is not None and (status is not None or cnt > 0):  # cnt = 0 alone may also mean "no bureau"
        bucket = "0" if cnt == 0 else "1-2" if cnt <= 2 else "3-5" if cnt <= 5 else "6+"
    else:
        bucket = None
    return {**values, "credit_history_status": status, "has_current_overdue": overdue,
            "cnt_bureau_active": cnt, "bureau_active_bucket": bucket,
            "max_dpd_bucket": max_dpd_bucket(values["max_dpd_days"])}


class DssService:
    """One-application assessment with the very code `hcdw score` runs in batch (dss.predict, dss.rules,
    dss.grading), so a what-if without changes reproduces the stored result."""

    def __init__(self, wh: Warehouse, q: Queries, model_dir: Path | None = None) -> None:
        self.wh, self.q = wh, q
        self.model_dir = Path(model_dir) if model_dir is not None else MODEL_DIR
        self._lock = threading.Lock()
        self._catalog: list[rules.RuleDef] | None = None
        self._income_cap: tuple[float | None] | None = None
        self._folds: tuple[int, pd.Series] | None = None
        self._sim_inputs: dss_policy.SimInputs | None = None

    # ---- cached inputs (the warehouse is a static snapshot while the API runs) -----------------
    def cursor(self):
        """A fresh cursor of the shared read-only connection; use it as a context manager (closes it)."""
        return self.wh.connection().cursor()

    def catalog(self) -> list[rules.RuleDef]:
        with self._lock:
            if self._catalog is None:
                with self.cursor() as cur:
                    self._catalog = rules.load_catalog(cur)
            return self._catalog

    def income_cap(self) -> float | None:
        with self._lock:
            if self._income_cap is None:
                with self.cursor() as cur:
                    self._income_cap = (dss_policy.income_cap(cur),)
            return self._income_cap[0]

    def sim_inputs(self) -> dss_policy.SimInputs:
        """Policy-simulation inputs of every scored application (outcomes on historical loans), read once
        (the warehouse is static while serving)."""
        with self._lock:
            if self._sim_inputs is None:
                with self.cursor() as cur:
                    self._sim_inputs = dss_policy.load_inputs(cur)
            return self._sim_inputs

    def folds(self) -> pd.Series:
        """application_id -> CV fold of the train rows (models/oof_pd.parquet), reloaded after retraining."""
        path = self.model_dir / "oof_pd.parquet"
        if not path.exists():
            raise HTTPException(503, NO_MODEL)
        mtime = path.stat().st_mtime_ns
        with self._lock:
            if self._folds is None or self._folds[0] != mtime:
                oof = pd.read_parquet(path, columns=["application_id", "fold"])
                self._folds = (mtime, oof.set_index("application_id")["fold"])
            return self._folds[1]

    def frame_columns(self) -> tuple[str, ...]:
        requires = [c for r in self.catalog() for c in r.requires]
        return tuple(dict.fromkeys(["application_id", "is_train", "target", *FEATURES, *rules.SEGMENT_INPUTS,
                                    *requires, *PROFILE_FIELDS]))

    # ---- guards ---------------------------------------------------------------------------------
    def require_model(self) -> None:
        if not predict.model_available(self.model_dir):
            raise HTTPException(503, NO_MODEL)

    def require_scored(self) -> None:
        if self.q.n_scored() == 0:
            raise HTTPException(503, NOT_SCORED)

    def require_fresh_scores(self) -> None:
        """Stored grades and a re-trained model must not be mixed (SHAP / what-if would disagree with them).

        Compares metrics.json `trained_at` with the last `hcdw score` run, not file mtimes: copying the
        project between machines (Google Drive) rewrites mtimes. Both are naive local times to the second.
        Grades and policy simulation only read stored scores, a consistent snapshot, so they skip this check.
        """
        scored_at = self.q.last_scored_at()
        trained_at = self.trained_at()
        if scored_at is not None and trained_at is not None and trained_at > scored_at:
            raise HTTPException(503, STALE_SCORES)

    def trained_at(self) -> datetime | None:
        """metrics.json `trained_at` as naive local time; None (= assume fresh) when missing or unreadable."""
        path = self.model_dir / "metrics.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))["trained_at"]
            t = datetime.fromisoformat(value)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("cannot read trained_at from %s (%s); not checking for stale scores", path, exc)
            return None
        return t.astimezone().replace(tzinfo=None) if t.tzinfo else t

    def check_categories(self, values: dict) -> None:
        for field in CATEGORICAL_INPUTS:
            v = values.get(field)
            if v is not None and v not in self.q.members(field):
                raise HTTPException(400, f"Unknown {field} '{v}'. Expected one of: {', '.join(self.q.members(field))}.")

    # ---- pipeline -------------------------------------------------------------------------------
    def load_application(self, application_id: int) -> tuple[pd.DataFrame, pd.Series | None]:
        """The application's warehouse row and its fold (None for test rows -> full model)."""
        frame = self.q.application(application_id, self.frame_columns())
        if frame.empty:
            raise HTTPException(404, f"Application {application_id} not found.")
        if not bool(frame["is_train"].iloc[0]):
            return frame, None
        # a Series aligned on the frame's index: predict routes each row to the fold model that never saw it
        folds = frame["application_id"].map(self.folds())
        if folds.isna().any():
            raise HTTPException(503, OTHER_DATA)
        return frame, folds

    def explain(self, frame: pd.DataFrame, folds: pd.Series | None) -> dict:
        return predict.explain(frame, top_n=5, folds=folds, model_dir=self.model_dir)[0]

    def stored(self, frame: pd.DataFrame, folds: pd.Series | None) -> dict:
        """The assessment `hcdw score` stored, plus its SHAP factors."""
        app_id = int(frame["application_id"].iloc[0])
        s = self.q.stored_assessment(app_id)
        if not s:
            raise HTTPException(503, NOT_SCORED)
        policy = grading.load_policy()
        hits = grading.sort_hits([
            grading.RuleHit(h["code"], h["group"],
                            rules.segment_label(h["rate_key"]) if h["group"] == rules.SEGMENT else h["label"],
                            float(h["rate"]), h["grade"], h["segment_key"], h["rate_key"])
            for h in self.q.stored_rule_hits(app_id)])
        a = grading.Assessment(
            pd=float(s["pd"]), score_grade=s["score_grade"], rule_grade=s["rule_grade"],
            final_grade=s["final_grade"], hits=hits, decision=s["decision"],
            max_credit_to_income=float(s["max_credit_to_income"]), downgraded=bool(s["downgraded"]),
            summary=grading.summarize(s["score_grade"], s["rule_grade"], s["final_grade"], hits),
            grade_source=s["grade_source"], n_rule_hits=int(s["n_rule_hits"]),
            decision_label=policy.decision_labels.get(s["decision"], s["decision"]),
            recommended_credit=float(s["recommended_credit"]))
        return self.to_json(a, frame, pd_source=s["pd_source"], shap=self.explain(frame, folds))

    def rescore(self, frame: pd.DataFrame, folds: pd.Series | None) -> dict:
        """Score, rule-check and grade a warehouse row again (batch rule semantics: NULL never violates)."""
        app_id = int(frame["application_id"].iloc[0])
        pd_ = float(predict.score(frame, folds=folds, model_dir=self.model_dir).iloc[0])
        catalog = self.catalog()
        with self.cursor() as cur:
            hits = rules.evaluate(cur, frame, catalog)[app_id]
        r = frame.iloc[0]
        a = grading.assess(pd_, hits, grading.load_policy(), amt_credit=float(r["amt_credit"]),
                           credit_to_income=float(r["credit_to_income"]))
        return self.to_json(a, frame, pd_source="model" if folds is None else "oof", shap=self.explain(frame, folds))

    def what_if(self, application_id: int, overrides: dict[str, Any]) -> dict:
        unknown = sorted(set(overrides) - set(WHAT_IF_FIELDS))
        if unknown:
            raise HTTPException(400, f"Unknown override field(s): {', '.join(unknown)}. "
                                     f"Allowed: {', '.join(WHAT_IF_FIELDS)}.")
        try:
            parsed = WhatIfOverrides.model_validate(overrides)
        except ValidationError as exc:
            raise HTTPException(400, f"Invalid override value: {_validation_message(exc)}") from exc
        values = {k: getattr(parsed, k) for k in overrides}
        nulls = sorted(k for k, v in values.items() if v is None and k not in WHAT_IF_NULLABLE)
        if nulls:
            raise HTTPException(400, f"Override(s) cannot be null: {', '.join(nulls)}.")
        self.check_categories(values)
        self.require_model()
        self.require_scored()
        self.require_fresh_scores()

        frame, folds = self.load_application(application_id)
        base = self.stored(frame, folds)
        scenario = frame.copy()
        for k, v in values.items():
            scenario[k] = [v]
        if AMOUNTS & set(values):
            r = scenario.iloc[0]
            new = ratios(r["amt_income"], r["amt_credit"], None if pd.isna(r["amt_annuity"]) else r["amt_annuity"],
                         None if pd.isna(r["amt_goods_price"]) else r["amt_goods_price"], self.income_cap())
            # only the ratios whose amounts changed: the others keep the stored ETL values bit for bit
            inputs = {"debt_to_income": {"amt_income", "amt_annuity"},
                      "credit_to_income": {"amt_income", "amt_credit"},
                      "credit_to_goods": {"amt_credit", "amt_goods_price"}}
            for ratio, needs in inputs.items():
                if needs & set(values):
                    scenario[ratio] = [new[ratio]]
        return {"application_id": application_id, "overrides": values, "base": base,
                "scenario": self.rescore(scenario, folds)}

    def simulate_new(self, inp: SimulatorInput) -> dict:
        """Assess a hand-typed application; rules whose inputs are missing are listed, never hit."""
        self.require_model()
        values = inp.model_dump()
        self.check_categories(values)
        record = derive_history(values)
        record.update(ratios(record["amt_income"], record["amt_credit"], record["amt_annuity"],
                             record["amt_goods_price"], self.income_cap()))
        missing = [f for f in SIMULATOR_FIELDS if rules.is_missing(record.get(f))]
        frame = pd.DataFrame([{c: record.get(c) for c in FEATURES}])
        pd_ = float(predict.score(frame, model_dir=self.model_dir).iloc[0])
        catalog = self.catalog()
        with self.cursor() as cur:
            hits, not_evaluated = rules.evaluate_record(cur, record, catalog)
        a = grading.assess(pd_, hits, grading.load_policy(), amt_credit=record["amt_credit"],
                           credit_to_income=record["credit_to_income"])
        return self.to_json(a, frame.assign(application_id=None, is_train=None, target=None),
                            pd_source="model", shap=self.explain(frame, None),
                            missing_fields=missing, rules_not_evaluated=not_evaluated, profile=profile_of(record))

    @staticmethod
    def to_json(a: grading.Assessment, frame: pd.DataFrame, *, pd_source: str, shap: dict,
                missing_fields: list[str] | None = None, rules_not_evaluated: list[dict] | None = None,
                profile: dict | None = None) -> dict:
        """The shared "assessment" shape of docs/dss_design.md 7.1, plus `profile` (default: from `frame`)."""
        r = frame.iloc[0]
        app_id, is_train, target = r["application_id"], r["is_train"], r["target"]
        is_train = None if is_train is None or pd.isna(is_train) else bool(is_train)
        numbers = {"pd": a.pd, "max_credit_to_income": a.max_credit_to_income, "amt_income": r["amt_income"],
                   "amt_credit": r["amt_credit"], "credit_to_income": r["credit_to_income"],
                   "recommended_credit": a.recommended_credit}
        bad = [k for k, v in numbers.items() if v is None or not math.isfinite(float(v))]
        if bad:  # unreachable within the input bounds; never answer null where the contract has a number
            raise HTTPException(400, f"The input gives a non-finite {', '.join(bad)}; use realistic amounts.")
        return {
            "application_id": None if app_id is None or pd.isna(app_id) else int(app_id),
            "is_train": is_train,
            "target": int(target) if is_train and not pd.isna(target) else None,
            "pd": a.pd,
            "pd_source": pd_source,
            "score_grade": a.score_grade,
            "rule_grade": a.rule_grade,
            "final_grade": a.final_grade,
            "grade_source": a.grade_source,
            "downgraded": a.downgraded,
            "summary": a.summary,
            "decision": a.decision,
            "decision_label": a.decision_label,
            "max_credit_to_income": a.max_credit_to_income,
            "amt_income": float(r["amt_income"]),
            "amt_credit": float(r["amt_credit"]),
            "credit_to_income": float(r["credit_to_income"]),
            "recommended_credit": a.recommended_credit,
            "shap": shap,
            "rule_hits": [dataclasses.asdict(h) for h in a.hits],
            "missing_fields": missing_fields or [],
            "rules_not_evaluated": rules_not_evaluated or [],
            "profile": profile if profile is not None else profile_of(r),
        }


def create_app(dw_path: Path | None = None, model_dir: Path | None = None) -> FastAPI:
    """model_dir: where model.pkl, oof_pd.parquet and metrics.json live (default: models/)."""
    wh = Warehouse(dw_path)
    q = Queries(wh)
    dss = DssService(wh, q, model_dir)
    explorer = Explorer(wh)
    cube = Cube(wh)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        wh.close()

    app = FastAPI(title="Home Credit DW API", version="1.0.0", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.exception_handler(QueryError)
    async def _bad_request(_: Request, exc: QueryError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's own 422, but with inf/NaN inputs echoed as text: JSON has no such numbers
        return JSONResponse({"detail": jsonable_encoder(_json_safe(exc.errors()))}, status_code=422)

    @app.exception_handler(Exception)
    async def _server_error(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Internal server error."}, status_code=500)

    # ---- metadata -----------------------------------------------------------------------------
    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/meta")
    def meta() -> dict:
        s = get_settings()
        return {
            "warehouse": {
                "target": s.dw_target,
                "name": (dw_path or s.dw_path).name if (dw_path or s.dw_target == "local") else s.md_database,
                "anchor_date": s.anchor_date,
                **q.warehouse_info(),
            },
            "dimensions": [{"key": k, "label": v} for k, v in DIMENSIONS.items()],
            "filter_dims": FILTER_DIMS,
            "measures": [{"key": k, "label": lbl, "kind": kind} for k, (lbl, _, kind) in MEASURES.items()],
            "members": {d: q.members(d) for d in DIMENSIONS},
            "hierarchies": HIERARCHIES,
        }

    # ---- application fact ---------------------------------------------------------------------
    @app.get("/api/kpis")
    def kpis(request: Request) -> dict:
        return q.kpis(parse_filters(request))

    @app.get("/api/kpis/summary")
    def kpi_summary() -> dict:
        """Portfolio KPIs (mart.kpi_portfolio_summary): counts, decisions, credit and credit at risk on every
        scored application; default rates on historical loans. Filter parameters are ignored."""
        return q.kpi_summary()

    @app.get("/api/by/{dim}")
    def by_dimension(dim: str, request: Request) -> list[dict]:
        return q.by_dimension(dim, parse_filters(request))

    @app.get("/api/cross")
    def cross(request: Request, x: str = Query(...), y: str = Query(...)) -> list[dict]:
        return q.cross(x, y, parse_filters(request))

    @app.get("/api/score-curve")
    def score_curve(request: Request) -> list[dict]:
        return q.score_curve(parse_filters(request))

    @app.get("/api/dti-by-outcome")
    def dti_by_outcome(request: Request) -> list[dict]:
        return q.dti_by_outcome(parse_filters(request))

    @app.get("/api/hierarchy")
    def hierarchy(request: Request, levels: str = Query(..., description="comma-separated dims")) -> list[dict]:
        dims = tuple(d for d in levels.split(",") if d)
        if not 1 <= len(dims) <= 4:
            raise HTTPException(400, "levels must name 1 to 4 dimensions")
        return q.hierarchy(dims, parse_filters(request))

    # ---- bureau fact --------------------------------------------------------------------------
    @app.get("/api/bureau/kpis")
    def bureau_kpis(request: Request) -> dict:
        return q.bureau_kpis(parse_filters(request))

    @app.get("/api/bureau/trend")
    def bureau_trend(request: Request, grain: str = Query("quarter", pattern="^(year|quarter|month)$")) -> list[dict]:
        return q.bureau_trend(parse_filters(request), grain)

    @app.get("/api/bureau/by-type")
    def bureau_by_type(request: Request) -> list[dict]:
        return q.bureau_by_type(parse_filters(request))

    # ---- OLAP ---------------------------------------------------------------------------------
    @app.get("/api/pivot")
    def pivot(request: Request, row: str = Query(...), col: str = Query(...),
              measure: str = Query("default_rate")) -> dict:
        return q.pivot(row, col, measure, parse_filters(request))

    @app.get("/api/olap")
    def olap_list() -> list[dict]:
        return [{"name": n, "sql": q.olap_sql(n)} for n in q.olap_files()]

    @app.get("/api/olap/{name}")
    def olap_run(name: str) -> dict:
        return {"name": name, "sql": q.olap_sql(name), **q.run_olap(name)}

    # ---- DSS: assessment, what-if, simulator, grades, policy, explanations ----------------------
    @app.get("/api/assessment/{application_id}")
    def assessment(application_id: int) -> dict:
        dss.require_model()
        dss.require_scored()
        dss.require_fresh_scores()
        return dss.stored(*dss.load_application(application_id))

    @app.post("/api/assessment/what-if")
    def what_if(req: WhatIfRequest) -> dict:
        return dss.what_if(req.application_id, req.overrides)

    @app.get("/api/simulator/options")
    def simulator_options() -> dict:
        return {
            "model_available": predict.model_available(dss.model_dir),
            "required_fields": SIMULATOR_REQUIRED,
            **{f: q.members(f) for f in CATEGORICAL_INPUTS},
            "credit_history_status": list(CREDIT_HISTORY_STATUSES),
            "region_rating_client": [1, 2, 3],
            "rules": [{"code": r.code, "group": r.group, "label": r.label, "requires": list(r.requires)}
                      for r in dss.catalog()],
        }

    @app.post("/api/simulator/score")
    def simulator_score(inp: SimulatorInput) -> dict:
        return dss.simulate_new(inp)

    @app.get("/api/grades")
    def grades(scope: str = Query("all")) -> dict:
        if scope not in GRADE_SCOPES:
            raise HTTPException(400, f"scope must be one of: {', '.join(GRADE_SCOPES)}.")
        dss.require_scored()
        policy = grading.load_policy()
        labels = get_settings().dss["grade_labels"]
        bounds = [0.0, *policy.thresholds, None]
        out = q.grades(scope)
        return {
            "scope": out["scope"], "n": out["n"], "thresholds": policy.thresholds,
            "policy": [{"grade": g, "label": labels[g], "min_rate": bounds[i], "max_rate": bounds[i + 1],
                        "decision": policy.by_grade[g].decision,
                        "decision_label": policy.decision_labels.get(policy.by_grade[g].decision),
                        "max_credit_to_income": policy.by_grade[g].max_credit_to_income}
                       for i, g in enumerate(GRADES)],
            **{k: v for k, v in out.items() if k not in ("scope", "n")},
        }

    @app.get("/api/rules")
    def rules_list() -> list[dict]:
        """Every rule in config order with its train statistics (mart.rule_stats) and editable param."""
        dss.require_scored()
        return finite_json(q.rule_list())

    @app.get("/api/policy/pd-distribution")
    def policy_pd_distribution() -> dict:
        dss.require_scored()
        return finite_json(q.pd_distribution())

    @app.post("/api/policy/simulate")
    def policy_simulate(body: dict[str, Any] = Body(default_factory=dict)) -> dict:
        unknown = sorted(set(body) - POLICY_KEYS)
        if unknown:
            raise HTTPException(400, f"Unknown field(s): {', '.join(unknown)}. "
                                     f"Allowed: {', '.join(sorted(POLICY_KEYS))}.")
        grade_type = body.get("grade_type") or "final"
        if grade_type not in dss_policy.GRADE_TYPES:
            raise HTTPException(400, f"grade_type must be one of: {', '.join(dss_policy.GRADE_TYPES)}.")
        check_policy_entries(body.get("policy"))
        try:
            policy = grading.policy_from_dict(body, grading.load_policy())
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, f"Invalid policy: {exc}") from exc
        try:
            dss_policy.check_rule_overrides(body.get("rules"))
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, f"Invalid rules: {exc}") from exc
        dss.require_scored()
        result = dss_policy.simulate(None, policy, grade_type, rules=body.get("rules"), inputs=dss.sim_inputs())
        if not result["n"]:
            raise HTTPException(503, NOT_SCORED)
        return finite_json(result)  # every key dss.policy.simulate returns, passed through

    # ---- Analytics: OLAP cube (slice/dice via conditions, roll-up/drill-down via levels, pivot) ---
    @app.post("/api/analytics/cube")
    def analytics_cube(body: CubeQuery) -> dict:
        return cube.run(body)

    # ---- Explorer: cohorts vs the book -----------------------------------------------------------
    @app.get("/api/explore/fields")
    def explore_fields() -> dict:
        return explorer.fields()

    @app.post("/api/explore/metrics")
    def explore_metrics(body: CohortQuery) -> dict:
        dss.require_scored()
        return explorer.metrics(body)

    @app.post("/api/explore/applications")
    def explore_applications(body: ApplicationsQuery) -> dict:
        dss.require_scored()
        return explorer.applications(body)

    @app.get("/api/explain/global")
    def explain_global() -> dict:
        path = dss.model_dir / "metrics.json"
        if not path.exists():
            raise HTTPException(503, NO_MODEL)
        m = json.loads(path.read_text(encoding="utf-8"))
        return {
            "auc_oof": m["auc_oof"], "brier": m["brier"], "n_train": m["n_train"],
            "gender_in_features": "gender" in m.get("features", []),
            "features": m["global_shap"],
            "calibration": m["calibration"],
        }

    # ---- built frontend (single-page app) -----------------------------------------------------
    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "Not found")
        if not FRONTEND_DIST.is_dir():
            return JSONResponse({"detail": "Frontend not built. Run `npm run build` in frontend/, "
                                           "or `npm run dev` there for development."}, status_code=404)
        target = (FRONTEND_DIST / path).resolve()
        if path and target.is_file() and FRONTEND_DIST.resolve() in target.parents:
            return FileResponse(target)
        return FileResponse(FRONTEND_DIST / "index.html")

    return app


app = create_app()
