"""Policy simulation (BR-09): re-grade the stored assessments under other thresholds or policies.

Works from what `hcdw score` stored, without re-running the model: score grade from
fact_risk_assessment.pd, business-rule grades from fact_rule_hit.rate, and the segment rule from
fact_risk_assessment.segment_rate (re-triggered under the new grades). Every scored application
(historical and new) counts towards grades, decisions and credit; default rates, defaults and rule
rates use the historical rows only, since only they have an outcome. See docs/dss_design.md 7.2.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

import duckdb
import numpy as np

from dss.grading import GRADES, Policy, load_policy
from hcdw.config import Settings, get_settings

GRADE_TYPES = ("final", "score")
# PD histogram (policy playground): 0.5 pp bins up to 30 %, then one overflow bin up to 100 %
PD_BIN_WIDTH = 0.005
PD_BIN_TOP = 0.30
# operators a rule `param` may use ("field op value"; NULL never violates)
RULE_OPS = {"<": np.less, "<=": np.less_equal, ">": np.greater, ">=": np.greater_equal}


def income_cap(con: duckdb.DuckDBPyConnection) -> float | None:
    """Income cap the ETL applied to ratios (largest capped income); None for an empty warehouse."""
    cap = con.execute("""SELECT MAX(amt_credit / credit_to_income) FROM dw.fact_loan_application
                         WHERE credit_to_income > 0""").fetchone()[0]
    return None if cap is None else float(cap)


def _ranks(rates: np.ndarray, thresholds: list[float]) -> np.ndarray:
    # side="right" = left-closed scale, same as grading.grade_of (bisect_right)
    return np.searchsorted(np.asarray(thresholds, dtype=float), rates, side="right")


def _recommended(policy: Policy, rank: np.ndarray, amt_credit: np.ndarray, cti: np.ndarray) -> np.ndarray:
    """grading.recommended_credit per row under the cap of its grade; rejected grades get no credit."""
    cap = np.array([0.0 if policy.by_grade[g].decision == "reject" else policy.by_grade[g].max_credit_to_income
                    for g in GRADES])[rank]
    with np.errstate(divide="ignore", invalid="ignore"):
        capped = np.where(cti > cap, amt_credit * cap / cti, amt_credit)
    return np.where((cap <= 0) | (amt_credit <= 0), 0.0, capped)


@dataclass(frozen=True)
class SimInputs:
    """Everything `simulate` reads, as arrays over every scored application (load once, reuse).

    is_train marks the rows with a known outcome (target is NaN elsewhere).
    stored: business rule code -> its stored hit rate per row (NaN = not violated).
    fields: input column of each parametric rule -> values per row (NaN = missing).
    """
    application_id: np.ndarray
    is_train: np.ndarray
    pd: np.ndarray
    target: np.ndarray
    amt_credit: np.ndarray
    credit_to_income: np.ndarray
    segment_rate: np.ndarray
    stored: dict[str, np.ndarray]
    fields: dict[str, np.ndarray]

    @property
    def n(self) -> int:
        return len(self.pd)


_IDENT = re.compile(r"[a-z_][a-z0-9_]*")


def rule_configs(settings: Settings | None = None) -> list[dict]:
    """dss.rules in config order, each param checked (field name, op, numeric bounds around value)."""
    rules = (settings or get_settings()).dss["rules"]
    for r in rules:
        p = r.get("param")
        if p is None:
            continue
        if r["group"] != "business" or not _IDENT.fullmatch(str(p.get("field", ""))) or p.get("op") not in RULE_OPS:
            raise ValueError(f"dss.rules {r['code']}: param needs a column name and one of {sorted(RULE_OPS)}")
        if not float(p["min"]) <= float(p["value"]) <= float(p["max"]):
            raise ValueError(f"dss.rules {r['code']}: param value must lie in [min, max]")
    return rules


def load_inputs(con: duckdb.DuckDBPyConnection, settings: Settings | None = None) -> SimInputs:
    """Stored assessment, stored business-rule hits and the parametric rules' inputs of every scored row."""
    rules = rule_configs(settings)
    fields = sorted({r["param"]["field"] for r in rules if r.get("param")})
    extra = "".join(f", w.{f}" for f in fields)  # names checked by rule_configs (trusted config)
    apps = con.execute(f"""
        SELECT a.application_id, f.is_train, a.pd, a.segment_rate, f.target, f.amt_credit,
               f.credit_to_income{extra}
        FROM dw.fact_risk_assessment a
        JOIN dw.fact_loan_application f USING (application_id)
        {"JOIN mart.v_application_wide w USING (application_id)" if fields else ""}
        ORDER BY a.application_id""").df()
    hits = con.execute("""
        SELECT h.application_id, h.rule_code, h.rate
        FROM dw.fact_rule_hit h JOIN dw.dim_rule d USING (rule_code)
        WHERE d.rule_group = 'business'""").df()
    ids = apps["application_id"].to_numpy()
    pos = {int(a): i for i, a in enumerate(ids)}
    stored = {}
    for r in rules:
        if r["group"] != "business":
            continue
        arr = np.full(len(ids), np.nan)
        part = hits[hits["rule_code"] == r["code"]]
        arr[[pos[int(a)] for a in part["application_id"]]] = part["rate"].to_numpy(dtype=float)
        stored[r["code"]] = arr
    return SimInputs(
        application_id=ids, is_train=apps["is_train"].to_numpy(dtype=bool), pd=apps["pd"].to_numpy(dtype=float),
        target=apps["target"].to_numpy(dtype=float, na_value=np.nan),
        amt_credit=apps["amt_credit"].to_numpy(dtype=float),
        credit_to_income=apps["credit_to_income"].to_numpy(dtype=float),
        segment_rate=apps["segment_rate"].to_numpy(dtype=float), stored=stored,
        fields={f: apps[f].to_numpy(dtype=float, na_value=np.nan) for f in fields})


def check_rule_overrides(overrides, settings: Settings | None = None) -> dict[str, dict]:
    """{code: {"enabled"?: bool, "value"?: number}} -> {code: {"enabled", "value"}} for every rule.

    Raises ValueError on an unknown code or key, a value on a rule without param, a non-finite value
    or one outside the param's [min, max]. Missing entries keep the configured rule.
    """
    rules = {r["code"]: r for r in rule_configs(settings)}
    overrides = {} if overrides is None else overrides
    if not isinstance(overrides, dict):
        raise ValueError("rules must be an object keyed by rule code")
    unknown = sorted(set(overrides) - set(rules))
    if unknown:
        raise ValueError(f"unknown rule code(s): {', '.join(map(str, unknown))}")
    out = {}
    for code, r in rules.items():
        entry = overrides.get(code, {})
        if not isinstance(entry, dict):
            raise ValueError(f"{code}: must be an object with enabled and/or value")
        extra = sorted(set(entry) - {"enabled", "value"})
        if extra:
            raise ValueError(f"{code}: unknown key(s) {', '.join(map(str, extra))}")
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"{code}: enabled must be true or false")
        p, value = r.get("param"), entry.get("value")
        if value is not None:
            if p is None:
                raise ValueError(f"{code}: this rule has no threshold to change")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{code}: value must be a number")
            try:
                value = float(value)
            except OverflowError:
                value = math.inf
            if not math.isfinite(value) or not float(p["min"]) <= value <= float(p["max"]):
                raise ValueError(f"{code}: value must be a finite number in [{p['min']}, {p['max']}]")
        out[code] = {"enabled": enabled, "value": value if value is not None else
                     (float(p["value"]) if p is not None else None)}
    return out


def _rule_rates(inputs: SimInputs, r: dict, value: float | None, thresholds: list[float]):
    """(hit rate per row with NaN = no hit, historical violators, all violators, rate) of one business rule.

    The configured value uses the stored hits exactly; another value re-evaluates `field op value` on
    every scored row (NULL = not violated, as in scoring). The rule's rate is the default rate of its
    historical violators (the only ones with an outcome); no historical violator means no rate, no hit.
    """
    p = r.get("param")
    if p is None or value == float(p["value"]):
        arr = inputs.stored[r["code"]]
        hit = ~np.isnan(arr)
        n_hist = int((hit & inputs.is_train).sum())
        return arr, n_hist, int(hit.sum()), (float(arr[hit][0]) if hit.any() else None)
    col = inputs.fields[p["field"]]
    with np.errstate(invalid="ignore"):
        hit = RULE_OPS[p["op"]](col, value) & ~np.isnan(col)
    hist = hit & inputs.is_train
    n_hist = int(hist.sum())
    rate = float(inputs.target[hist].mean()) if n_hist else None
    arr = np.where(hit, rate, np.nan) if rate is not None else np.full(inputs.n, np.nan)
    return arr, n_hist, int(hit.sum()), rate


def _grade_ranks(inputs: SimInputs, policy: Policy, grade_type: str, rule_rates: list[np.ndarray] | None = None,
                 segment_on: bool = True) -> np.ndarray:
    """Grade index (0 = A) of each row under `policy`: the score grade, or for "final" the worse of the
    score grade, the worst business-rule grade and the segment grade when the segment triggers.

    rule_rates: hit-rate arrays of the enabled business rules (default: every stored rule).
    """
    t = policy.thresholds
    rank = _ranks(inputs.pd, t)
    if grade_type == "final":
        rates = list(inputs.stored.values()) if rule_rates is None else rule_rates
        if rates:
            # grades are monotone in the rate, so the worst business hit is the one with the highest rate
            biz = np.fmax.reduce(np.vstack(rates), axis=0)
            rank = np.where(np.isnan(biz), rank, np.maximum(rank, _ranks(biz, t)))
        if segment_on:
            triggered, seg_rank = _segment_triggers(inputs, policy)
            rank = np.where(triggered, np.maximum(rank, seg_rank), rank)
    return rank


def _segment_triggers(inputs: SimInputs, policy: Policy) -> tuple[np.ndarray, np.ndarray]:
    triggers = policy.segment_trigger_grades
    if triggers is None:
        triggers = tuple(get_settings().dss["segment_trigger_grades"])
    seg = inputs.segment_rate
    seg_rank = _ranks(seg, policy.thresholds)
    is_trigger = np.array([g in triggers for g in GRADES])
    return ~np.isnan(seg) & is_trigger[seg_rank], seg_rank


def _transitions(from_rank: np.ndarray, to_rank: np.ndarray, base: Policy, policy: Policy,
                 target: np.ndarray, requested: np.ndarray, pd_: np.ndarray, is_train: np.ndarray) -> list[dict]:
    """Groups of loans whose grade or decision differs between `base` and `policy`, largest first.

    n and the credit sums cover every scored row; defaults and default_rate the historical ones.
    """
    k = len(GRADES)
    key = from_rank * k + to_rank
    count = np.bincount(key, minlength=k * k)
    hist = np.bincount(key, weights=is_train, minlength=k * k)
    defaults = np.bincount(key, weights=np.nan_to_num(target), minlength=k * k)
    credit = np.bincount(key, weights=requested, minlength=k * k)
    at_risk = np.bincount(key, weights=requested * pd_, minlength=k * k)
    out = []
    for idx in np.flatnonzero(count):
        f, t = divmod(int(idx), k)
        from_grade, to_grade = GRADES[f], GRADES[t]
        from_decision, to_decision = base.by_grade[from_grade].decision, policy.by_grade[to_grade].decision
        if from_grade == to_grade and from_decision == to_decision:
            continue
        n, n_hist = int(count[idx]), int(round(hist[idx]))
        out.append({"from_grade": from_grade, "to_grade": to_grade, "from_decision": from_decision,
                    "to_decision": to_decision, "n": n, "n_historical": n_hist,
                    "defaults": int(round(defaults[idx])),
                    "default_rate": float(defaults[idx] / n_hist) if n_hist else None,
                    "requested_credit": float(credit[idx]), "credit_at_risk": float(at_risk[idx])})
    return sorted(out, key=lambda r: (-r["n"], r["from_grade"], r["to_grade"]))


def _grade_or_none(rate: float | None, thresholds: list[float]) -> str | None:
    return None if rate is None else GRADES[int(_ranks(np.array([rate]), thresholds)[0])]


def simulate(con: duckdb.DuckDBPyConnection | None, policy: Policy, grade_type: str = "final",
             baseline: Policy | None = None, rules: dict | None = None, inputs: SimInputs | None = None) -> dict:
    """Grade distribution, default rate and decision mix of every scored application under `policy`.

    Scope: n, shares, decision rates, credit and expected loss cover every scored row (n); default
    rates, approved_default_rate, transition defaults and rule rates use the historical rows
    (n_historical) only. n_violators in `rules` counts historical violators, n_violators_all every row.

    With the configured policy it reproduces the stored grades and recommended credit. Credit volume
    (approved_credit, review_credit, per-grade credit_volume) applies each grade's max_credit_to_income
    to the ETL's credit_to_income (capped income), like scoring; rejected grades get no credit.
    Expected loss (approved_expected_loss, per-grade expected_loss) is that credit x stored pd.
    Before scoring, n is 0, the rates are None and the credit sums 0.

    `rules` ({code: {"enabled", "value"}}, see check_rule_overrides) switches rules off or moves a
    parametric rule's threshold, for the simulation only. The "score" grade ignores every rule, so it
    equals "final" with every rule disabled. `rules` in the result describes each rule at its simulated
    value (violators, their default rate and its grade under the simulated cut-offs); `enabled` says
    whether it counts towards the grades.

    `transitions` compares every loan with `baseline` (default: the configured policy, final grade, every
    rule as configured, i.e. the stored decisions): one row per (from_grade, to_grade) group whose grade
    or decision changes. `inputs` (load_inputs) avoids reading the warehouse again.
    """
    if grade_type not in GRADE_TYPES:
        raise ValueError(f"grade_type must be one of {GRADE_TYPES}, got {grade_type!r}")
    baseline = baseline or load_policy()
    configs = rule_configs()
    chosen = check_rule_overrides(rules)
    inputs = inputs if inputs is not None else load_inputs(con)
    t = policy.thresholds

    rule_out, enabled_rates, rules_as_configured = [], [], True
    for r in configs:
        state = chosen[r["code"]]
        enabled = state["enabled"] and grade_type == "final"
        rules_as_configured &= state["enabled"] and (r.get("param") is None or
                                                      state["value"] == float(r["param"]["value"]))
        if r["group"] == "business":
            arr, n_viol, n_all, rate = _rule_rates(inputs, r, state["value"], t)
            if enabled:
                enabled_rates.append(arr)
        else:
            hit, _ = _segment_triggers(inputs, policy)
            hist = hit & inputs.is_train
            n_viol, n_all = int(hist.sum()), int(hit.sum())
            rate = float(inputs.target[hist].mean()) if n_viol else None
        rule_out.append({"code": r["code"], "enabled": enabled, "value": state["value"],
                         "n_violators": n_viol, "n_violators_all": n_all,
                         "default_rate": rate, "grade": _grade_or_none(rate, t)})

    segment_on = chosen[next(r["code"] for r in configs if r["group"] == "segment")]["enabled"]
    rank = _grade_ranks(inputs, policy, grade_type, enabled_rates, segment_on)
    same_scale = (grade_type == "final" and rules_as_configured and policy.thresholds == baseline.thresholds
                  and policy.segment_trigger_grades == baseline.segment_trigger_grades)
    base_rank = rank if same_scale else _grade_ranks(inputs, baseline, "final")  # only decisions can differ

    target, requested, pd_ = inputs.target, inputs.amt_credit, inputs.pd
    credit = _recommended(policy, rank, requested, inputs.credit_to_income)
    loss = credit * pd_  # expected loss on the credit actually granted
    n = inputs.n
    # per-grade sums in one pass each (bincount), then decisions aggregate those five rows
    k = len(GRADES)
    count = np.bincount(rank, minlength=k)
    count_hist = np.bincount(rank, weights=inputs.is_train, minlength=k)  # outcome-known rows
    defaults = np.bincount(rank, weights=np.nan_to_num(target), minlength=k)
    credit_sum = np.bincount(rank, weights=credit, minlength=k)
    requested_sum = np.bincount(rank, weights=requested, minlength=k)
    loss_sum = np.bincount(rank, weights=loss, minlength=k)
    decision_of = [policy.by_grade[g].decision for g in GRADES]
    by_grade = [{"grade": grade, "n": int(count[i]), "n_historical": int(round(count_hist[i])),
                 "share": int(count[i]) / n if n else None,
                 "default_rate": float(defaults[i] / count_hist[i]) if count_hist[i] else None,
                 "decision": decision_of[i],
                 "credit_volume": float(credit_sum[i]),
                 "requested_credit": float(requested_sum[i]),
                 "expected_loss": float(loss_sum[i])} for i, grade in enumerate(GRADES)]

    def total(values, decision: str) -> float:
        return float(sum(values[i] for i in range(k) if decision_of[i] == decision))

    count_of = {d: int(total(count, d)) for d in ("approve", "review", "reject")}
    approved_hist = total(count_hist, "approve")
    return {
        "grade_type": grade_type,
        "n": n,
        "n_historical": int(inputs.is_train.sum()),
        "approval_rate": count_of["approve"] / n if n else None,
        "referral_rate": count_of["review"] / n if n else None,
        "reject_rate": count_of["reject"] / n if n else None,
        "approved_default_rate": total(defaults, "approve") / approved_hist if approved_hist else None,
        "approved_credit": total(credit_sum, "approve"),
        "approved_credit_requested": total(requested_sum, "approve"),
        "review_credit": total(credit_sum, "review"),
        "approved_expected_loss": total(loss_sum, "approve"),
        "by_grade": by_grade,
        "transitions": _transitions(base_rank, rank, baseline, policy, target, requested, pd_, inputs.is_train),
        "rules": rule_out,
    }


def pd_histogram(pd_: np.ndarray, target: np.ndarray, amt_credit: np.ndarray,
                 width: float = PD_BIN_WIDTH, top: float = PD_BIN_TOP) -> list[dict]:
    """Loans per PD bin: `width`-wide bins from 0 to `top`, then one overflow bin [top, 1].

    Bins are left-closed like the grade scale: a PD equal to an edge (exactly 0.07) opens the next bin.
    Edges are rounded to their decimal value so they equal the configured cut-offs bit for bit.
    """
    n_regular = int(round(top / width))
    edges = np.array([round(i * width, 10) for i in range(n_regular + 1)])
    idx = np.clip(np.searchsorted(edges, pd_, side="right") - 1, 0, n_regular)
    size = n_regular + 1
    count = np.bincount(idx, minlength=size)
    defaults = np.bincount(idx, weights=np.nan_to_num(target), minlength=size)
    credit = np.bincount(idx, weights=amt_credit, minlength=size)
    at_risk = np.bincount(idx, weights=amt_credit * pd_, minlength=size)
    his = [*edges[1:], 1.0]
    return [{"lo": float(edges[i]), "hi": float(his[i]), "n": int(count[i]), "defaults": int(round(defaults[i])),
             "default_rate": float(defaults[i] / count[i]) if count[i] else None,
             "credit": float(credit[i]), "credit_at_risk": float(at_risk[i])} for i in range(size)]


def pd_distribution(con: duckdb.DuckDBPyConnection | None, inputs: SimInputs | None = None) -> dict:
    """PD histogram of the scored train applications (the population `simulate` grades)."""
    inputs = inputs if inputs is not None else load_inputs(con)
    h = inputs.is_train  # historical loans only: the bins carry default rates
    bins = pd_histogram(inputs.pd[h], inputs.target[h], inputs.amt_credit[h])
    return {"n": int(h.sum()), "bin_width": PD_BIN_WIDTH, "bins": bins}
