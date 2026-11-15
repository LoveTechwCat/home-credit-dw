"""Rule catalog and rule evaluation (segment rule BR-SEG-01 + business rules BR-RF-*).

Rules run as SQL over a DataFrame registered in DuckDB as `src`, using the very fragments the ETL
views use (`Settings.sql_params`), so batch scoring, the API and mart.rule_stats agree. Rates and
grades come from the warehouse: mart.rule_stats (business rules) and mart.segment_risk (segment rule).
See docs/dss_design.md ("Khái niệm luật", sections 1, 2, 5.4 and 7.3).
"""
from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import duckdb
import pandas as pd

from dss.grading import RuleHit, sort_hits
from hcdw.config import Settings, get_settings

SEGMENT, BUSINESS = "segment", "business"
# raw inputs of the segment attributes (age_band / income_group are derived from age_years / income_type)
SEGMENT_INPUTS = ("credit_history_status", "age_years", "income_type", "region_rating_client")


@dataclass(frozen=True)
class _Config:
    """What the rules need from one Settings object, built once (Settings.dss re-validates per access)."""
    rule_hits_sql: str
    segment_sql: str
    labels: dict                                   # dss.segment.labels, region keys as str
    rules: tuple[tuple[str, str, str, tuple[str, ...]], ...]   # (code, group, label, requires), catalog order
    label_memo: dict[str, str] = field(default_factory=dict)


_CONFIGS: dict[int, tuple[Settings, _Config]] = {}
_CONFIGS_LOCK = threading.Lock()


def _config(settings: Settings | None = None) -> _Config:
    """Per-Settings cache. Keyed by identity and holding the object, so an id is never reused while
    cached; get_settings() is itself cached, so normally there is a single entry."""
    s = settings or get_settings()
    hit = _CONFIGS.get(id(s))
    if hit is not None and hit[0] is s:
        return hit[1]
    dss, p = s.dss, s.sql_params
    labels = dss["segment"]["labels"]
    cfg = _Config(
        rule_hits_sql=p["dss_rule_hits_sql"],
        segment_sql=f"""
        WITH s AS (
            SELECT application_id, credit_history_status,
                   {p['dss_age_band_sql']} AS age_band, {p['dss_income_group_sql']} AS income_group,
                   region_rating_client
            FROM src)
        SELECT s.application_id, r.segment_key, r.rate_key, r.default_rate, r.grade, r.is_trigger
        FROM s
        JOIN mart.segment_risk r
          ON  r.credit_history_status = s.credit_history_status
          AND r.age_band              = s.age_band
          AND r.income_group          = s.income_group
          AND r.region_rating_client  = s.region_rating_client""",
        labels={"credit_history_status": dict(labels["credit_history_status"]),
                "income_group": dict(labels["income_group"]),
                "region_rating_client": {str(k): v for k, v in labels["region_rating_client"].items()}},
        rules=tuple(sorted(((r["code"], r["group"], r["label"], tuple(r.get("requires") or ()))
                            for r in dss["rules"]), key=lambda r: (r[1] != SEGMENT, r[0]))),
    )
    with _CONFIGS_LOCK:
        if len(_CONFIGS) >= 16:  # only tests build many Settings objects
            _CONFIGS.clear()
        _CONFIGS[id(s)] = (s, cfg)
    return cfg


@dataclass(frozen=True)
class RuleDef:
    """A catalog rule with its train statistics; rate/grade are None when no train row violates it."""
    code: str
    group: str                    # "segment" | "business"
    label: str
    sql: str | None               # None for the segment rule
    requires: tuple[str, ...]     # input fields the rule needs (simulator: missing -> not evaluated)
    n_violators: int
    rate: float | None
    grade: str | None


@dataclass(frozen=True)
class Segment:
    """The mart.segment_risk row an application falls in (whether or not it triggers the rule)."""
    segment_key: str
    rate_key: str                 # group that supplied the rate ('*' = dropped attribute)
    rate: float | None
    grade: str | None
    is_trigger: bool


def load_catalog(con: duckdb.DuckDBPyConnection) -> list[RuleDef]:
    """dw.dim_rule joined with mart.rule_stats; segment rule first, then business rules by code."""
    rows = con.execute("""
        SELECT d.rule_code, d.rule_group, d.label, d.sql_condition, d.requires,
               COALESCE(s.n_violators, 0), s.default_rate, s.grade
        FROM dw.dim_rule d
        LEFT JOIN mart.rule_stats s USING (rule_code)
        ORDER BY d.rule_group <> 'segment', d.rule_code""").fetchall()
    return [RuleDef(code, group, label, sql, tuple(requires or ()), int(n),
                    None if rate is None else float(rate), grade)
            for code, group, label, sql, requires, n, rate, grade in rows]


def is_missing(value: object) -> bool:
    """None / NaN / NA count as a missing input."""
    if value is None or isinstance(value, (list, tuple, dict, set)):
        return value is None
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _prepare(frame: pd.DataFrame, catalog: Sequence[RuleDef]) -> pd.DataFrame:
    """Add every input column the rules read but `frame` lacks, as NULL (= rule not violated)."""
    needed = {c for r in catalog for c in r.requires} | set(SEGMENT_INPUTS)
    missing = sorted(needed - set(frame.columns))
    return frame.assign(**{c: None for c in missing}) if missing else frame


def _query(con: duckdb.DuckDBPyConnection, frame: pd.DataFrame, sql: str) -> list[tuple]:
    # A cursor has its own registrations, so concurrent callers never see each other's `src`.
    cur = con.cursor()
    try:
        cur.register("src", frame)
        return cur.execute(sql).fetchall()
    finally:
        cur.close()


def segment_label(rate_key: str, settings: Settings | None = None) -> str:
    """English display label of a segment key, e.g. 'Segment: No bureau data | 25-34 | Working | Region 3'."""
    cfg = _config(settings)
    label = cfg.label_memo.get(rate_key)
    if label is None:
        history, age, income, region = rate_key.split("|")
        parts = [cfg.labels["credit_history_status"].get(history, history), age,
                 cfg.labels["income_group"].get(income, income),
                 cfg.labels["region_rating_client"].get(region, region)]
        label = cfg.label_memo[rate_key] = "Segment: " + " | ".join("Any" if x == "*" else x for x in parts)
    return label


def lookup_segments(con: duckdb.DuckDBPyConnection, frame: pd.DataFrame,
                    settings: Settings | None = None) -> dict[int, Segment]:
    """Segment of each application in `frame`; ids with a NULL/unknown segment attribute are absent."""
    frame = _prepare(frame, [])
    rows = _query(con, frame, _config(settings).segment_sql)
    return {int(app): Segment(key, rate_key, None if rate is None else float(rate), grade, bool(trigger))
            for app, key, rate_key, rate, grade, trigger in rows}


def evaluate(con: duckdb.DuckDBPyConnection, frame: pd.DataFrame, catalog: Sequence[RuleDef] | None = None,
             settings: Settings | None = None) -> dict[int, list[RuleHit]]:
    """Violated rules per application_id of `frame` (every id present; [] = nothing violated).

    Batch semantics: a NULL input never violates a rule. A business rule without train violators has
    no rate, hence no grade, and is not reported. Pass `catalog` (load_catalog) to reuse it across calls.
    """
    settings = settings or get_settings()
    cfg = _config(settings)
    catalog = load_catalog(con) if catalog is None else list(catalog)
    frame = _prepare(frame, catalog)
    out: dict[int, list[RuleHit]] = {int(app): [] for app in frame["application_id"]}
    by_code = {r.code: r for r in catalog}

    for app, code in _query(con, frame, cfg.rule_hits_sql):
        rule = by_code.get(code)
        if rule is not None and rule.group == BUSINESS and rule.grade is not None:
            out[int(app)].append(RuleHit(rule.code, BUSINESS, rule.label, rule.rate, rule.grade))

    seg_rule = next((r for r in catalog if r.group == SEGMENT), None)
    if seg_rule is not None:
        for app, s in lookup_segments(con, frame, settings).items():
            if s.is_trigger and s.rate is not None and s.grade is not None:
                out[app].append(RuleHit(seg_rule.code, SEGMENT, segment_label(s.rate_key, settings), s.rate,
                                        s.grade, segment_key=s.segment_key, rate_key=s.rate_key))
    return {app: sort_hits(hits) for app, hits in out.items()}


def _catalog_rules(catalog: Sequence[RuleDef] | None) -> Sequence[tuple[str, str, str, Sequence[str]]]:
    if catalog is not None:
        return [(r.code, r.group, r.label, r.requires) for r in catalog]
    return _config().rules


def rules_not_evaluated(record: Mapping, catalog: Sequence[RuleDef] | None = None) -> list[dict]:
    """Rules a single (manually entered) record cannot be checked against: a required field is missing.

    Returns [{code, group, label, missing}] in catalog order (docs/dss_design.md 7.3). The catalog
    defaults to the rules in settings, so no warehouse is needed.
    """
    out = []
    for code, group, label, requires in _catalog_rules(catalog):
        missing = [f for f in requires if is_missing(record.get(f))]
        if missing:
            out.append({"code": code, "group": group, "label": label, "missing": missing})
    return out


def evaluate_record(con: duckdb.DuckDBPyConnection, record: Mapping, catalog: Sequence[RuleDef] | None = None,
                    settings: Settings | None = None) -> tuple[list[RuleHit], list[dict]]:
    """(hits, rules_not_evaluated) for one record; a rule that cannot be evaluated is never a hit."""
    catalog = load_catalog(con) if catalog is None else list(catalog)
    not_evaluated = rules_not_evaluated(record, catalog)
    skip = {r["code"] for r in not_evaluated}
    row = {k: (None if is_missing(v) else v) for k, v in record.items()}
    row["application_id"] = 0  # placeholder: simulator records have no id
    hits = evaluate(con, pd.DataFrame([row]), catalog, settings)[0]
    return [h for h in hits if h.code not in skip], not_evaluated
