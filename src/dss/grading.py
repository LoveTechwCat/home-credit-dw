"""A-E grading core: score grade from PD, rule grade = worst violated rule, final = the worse of the two.

Pure functions only (no warehouse, no model), shared by batch scoring, the API and policy simulation
so all three grade the same way. The scale and policy come from `dss` in configs/settings.yaml;
see docs/dss_design.md (sections 3 and 7.1) for the rules and the summary sentences.
"""
from __future__ import annotations

import math
import numbers
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from hcdw.config import DECISIONS, GRADES, Settings, get_settings

__all__ = [
    "GRADES", "Assessment", "GradePolicy", "Policy", "RuleHit", "assess", "credit_to_income", "grade_of",
    "load_policy", "policy_from_dict", "recommended_credit", "sort_hits", "summarize", "worse",
]


def _number(value: object, what: str) -> float:
    """A real number as float; anything else (str, None, bool, list, ...) is a ValueError."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(f"{what} must be a number, got {value!r}")
    return float(value)


def _check_thresholds(thresholds: Sequence[float]) -> list[float]:
    if isinstance(thresholds, (str, bytes, Mapping)) or not isinstance(thresholds, Iterable):
        raise ValueError(f"thresholds must be a list of {len(GRADES) - 1} rates, got {thresholds!r}")
    t = [_number(x, "threshold") for x in thresholds]
    if len(t) != len(GRADES) - 1 or any(not 0 < x < 1 for x in t) or any(a >= b for a, b in zip(t, t[1:])):
        raise ValueError(f"thresholds must be {len(GRADES) - 1} increasing rates in (0, 1), got {t}")
    return t


def grade_of(rate: float, thresholds: Sequence[float]) -> str:
    """Grade of a risk rate on a left-closed scale: a rate equal to a threshold takes the worse grade."""
    rate = float(rate)
    if math.isnan(rate) or not 0 <= rate <= 1:
        raise ValueError(f"rate must be in [0, 1], got {rate}")
    return GRADES[bisect_right(_check_thresholds(thresholds), rate)]


def worse(a: str | None, b: str | None) -> str | None:
    """The riskier of two grades; None means "no grade" and never wins."""
    if a is None or b is None:
        return b if a is None else a
    return GRADES[max(GRADES.index(a), GRADES.index(b))]


@dataclass(frozen=True)
class RuleHit:
    """One violated rule. segment_key / rate_key are set for the segment rule only."""
    code: str
    group: str            # "segment" | "business"
    label: str            # English UI label (segment rule: "Segment: ... | ... | ... | ...")
    rate: float           # train default rate of the violating group
    grade: str
    segment_key: str | None = None
    rate_key: str | None = None


@dataclass(frozen=True)
class GradePolicy:
    decision: str                 # approve | review | reject
    max_credit_to_income: float


@dataclass(frozen=True)
class Policy:
    """Grade scale plus the decision and credit cap of each grade (validated on construction).

    segment_trigger_grades: segment grades that violate the segment rule; None = take them from
    settings (only policy simulation needs them).
    """
    thresholds: list[float]
    by_grade: dict[str, GradePolicy]
    segment_trigger_grades: tuple[str, ...] | None = None
    decision_labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "thresholds", _check_thresholds(self.thresholds))
        if not isinstance(self.by_grade, Mapping) or set(self.by_grade) != set(GRADES):
            raise ValueError(f"policy must map exactly grades {GRADES}, got {self.by_grade!r}")
        by_grade = {}
        for grade, p in self.by_grade.items():
            if not isinstance(p, GradePolicy):
                raise ValueError(f"policy {grade}: expected a GradePolicy, got {p!r}")
            if not isinstance(p.decision, str) or p.decision not in DECISIONS:
                raise ValueError(f"policy {grade}: decision must be one of {DECISIONS}, got {p.decision!r}")
            mcti = _number(p.max_credit_to_income, f"policy {grade}: max_credit_to_income")
            if math.isnan(mcti) or mcti < 0:
                raise ValueError(f"policy {grade}: max_credit_to_income must be >= 0, got {mcti}")
            by_grade[grade] = GradePolicy(p.decision, mcti)
        object.__setattr__(self, "by_grade", by_grade)  # own copy, numbers as float
        if self.segment_trigger_grades is not None:
            object.__setattr__(self, "segment_trigger_grades", tuple(self.segment_trigger_grades))
            if not set(self.segment_trigger_grades) <= set(GRADES):
                raise ValueError(f"segment_trigger_grades must be grades {GRADES}")


def load_policy(settings: Settings | None = None) -> Policy:
    """The configured policy (`dss` in configs/settings.yaml)."""
    dss = (settings or get_settings()).dss
    return Policy(
        thresholds=[float(t) for t in dss["grade_thresholds"]],
        by_grade={g: GradePolicy(p["decision"], float(p["max_credit_to_income"])) for g, p in dss["policy"].items()},
        segment_trigger_grades=tuple(dss["segment_trigger_grades"]),
        decision_labels=dict(dss["decision_labels"]),
    )


def policy_from_dict(data: Mapping, base: Policy) -> Policy:
    """`base` with the request's `thresholds` and/or `policy` ({grade: {decision, max_credit_to_income}}).

    A missing or null field keeps the base value. Anything malformed raises ValueError (never
    TypeError/KeyError), which the API turns into a 400.
    """
    if not isinstance(data, Mapping):
        raise ValueError(f"policy request must be an object, got {data!r}")
    thresholds = data.get("thresholds")
    by_grade = base.by_grade
    if data.get("policy") is not None:
        raw = data["policy"]
        if not isinstance(raw, Mapping):
            raise ValueError(f"policy must map grades to {{decision, max_credit_to_income}}, got {raw!r}")
        by_grade = {}
        for grade, p in raw.items():
            if not isinstance(p, Mapping) or not {"decision", "max_credit_to_income"} <= set(p):
                raise ValueError(f"policy {grade}: needs decision and max_credit_to_income, got {p!r}")
            by_grade[grade] = GradePolicy(p["decision"], p["max_credit_to_income"])
    try:
        return Policy(thresholds=base.thresholds if thresholds is None else thresholds, by_grade=by_grade,
                      segment_trigger_grades=base.segment_trigger_grades, decision_labels=base.decision_labels)
    except (TypeError, KeyError) as exc:  # belt and braces: the API must answer 400, not 500
        raise ValueError(str(exc)) from exc


@dataclass
class Assessment:
    pd: float
    score_grade: str
    rule_grade: str | None        # None: no rule violated
    final_grade: str
    hits: list[RuleHit]           # worst grade first, then by code
    decision: str
    max_credit_to_income: float
    downgraded: bool
    summary: str
    grade_source: str             # score | rule | both
    n_rule_hits: int
    decision_label: str
    recommended_credit: float | None = None   # None when assess() was not given the amounts


def sort_hits(hits: Sequence[RuleHit]) -> list[RuleHit]:
    """Display order: worst grade first, then by rule code."""
    return sorted(hits, key=lambda h: (-GRADES.index(h.grade), h.code))


def _rule_list(group: str, codes: list[str]) -> str:
    return f"{group} rule{'s' if len(codes) > 1 else ''} {', '.join(codes)}"


def summarize(score_grade: str, rule_grade: str | None, final_grade: str, hits: Sequence[RuleHit]) -> str:
    """English one-line explanation of the final grade (docs/dss_design.md 7.1)."""
    if not hits:
        return "Final grade equals score grade (no rule violated)."
    if final_grade == score_grade:
        return f"Final grade equals score grade {score_grade}; violated rules do not downgrade it."
    # every rule at the rule grade caused the downgrade; segment rule named first
    parts = []
    for group in ("segment", "business"):
        codes = sorted(h.code for h in hits if h.grade == rule_grade and h.group == group)
        if codes:
            parts.append(_rule_list(group, codes))
    return f"Downgraded from {score_grade} to {final_grade} by {' and '.join(parts)}."


def credit_to_income(amt_credit: float, amt_income: float, income_cap: float | None = None) -> float:
    """Credit / income with income capped like the ETL does for ratios (cap: policy.income_cap)."""
    income = float(amt_income) if income_cap is None else min(float(amt_income), float(income_cap))
    if not income > 0:
        raise ValueError(f"amt_income must be > 0, got {amt_income}")
    return float(amt_credit) / income


def recommended_credit(amt_credit: float, credit_to_income: float, max_credit_to_income: float) -> float:
    """amt_credit x min(1, max_credit_to_income / credit_to_income), i.e. min(credit, cap x capped income)."""
    amt_credit, cti, mcti = float(amt_credit), float(credit_to_income), float(max_credit_to_income)
    if mcti <= 0 or amt_credit <= 0:
        return 0.0
    if math.isnan(cti) or cti < 0:
        raise ValueError(f"credit_to_income must be >= 0, got {credit_to_income}")
    return amt_credit if cti <= mcti else amt_credit * mcti / cti


def assess(pd: float, hits: list[RuleHit], policy: Policy, *,
           amt_credit: float | None = None, credit_to_income: float | None = None) -> Assessment:
    """Grade one application: rules can only downgrade the score grade, never upgrade it.

    With amt_credit and credit_to_income the recommended credit of the final grade is filled in
    (0 when its decision is reject). max_credit_to_income is the configured cap of the final grade.
    """
    cti = credit_to_income  # the public keyword shadows the module function; only the value is used
    pd = float(pd)
    if math.isnan(pd) or not 0 <= pd <= 1:
        raise ValueError(f"pd must be in [0, 1], got {pd}")
    hits = sort_hits(hits)
    score_grade = grade_of(pd, policy.thresholds)
    rule_grade = hits[0].grade if hits else None
    final_grade = worse(score_grade, rule_grade)
    if rule_grade is None or rule_grade < score_grade:
        source = "score"
    else:
        source = "both" if rule_grade == score_grade else "rule"
    p = policy.by_grade[final_grade]
    limit = None
    if amt_credit is not None and cti is not None:
        # a reject never comes with a limit, whatever cap is configured (same rule as policy.simulate)
        cap = 0.0 if p.decision == "reject" else p.max_credit_to_income
        limit = recommended_credit(amt_credit, cti, cap)
    return Assessment(
        pd=pd, score_grade=score_grade, rule_grade=rule_grade, final_grade=final_grade, hits=hits,
        decision=p.decision, max_credit_to_income=float(p.max_credit_to_income),
        downgraded=final_grade != score_grade,
        summary=summarize(score_grade, rule_grade, final_grade, hits),
        grade_source=source, n_rule_hits=len(hits),
        decision_label=policy.decision_labels.get(p.decision, p.decision),
        recommended_credit=limit,
    )
