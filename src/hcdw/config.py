"""Project settings: configs/settings.yaml + .env (environment variables win)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


def find_root() -> Path:
    """Repo root = first parent holding pyproject.toml and configs/ (override with HCDW_ROOT)."""
    env = os.getenv("HCDW_ROOT")
    if env:
        return Path(env).resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists() and (parent / "configs").is_dir():
            return parent
    return Path.cwd()


ROOT = find_root()
SQL_DIR = ROOT / "src" / "sql"


@dataclass(frozen=True)
class Settings:
    raw: dict[str, Any] = field(repr=False)
    dw_target: str
    dw_path: Path
    raw_dir: Path
    sample_dir: Path
    fixture_dir: Path
    dq_report: Path
    md_database: str
    motherduck_token: str | None

    @property
    def anchor_date(self) -> str:
        return str(self.raw["time"]["anchor_date"])

    @property
    def synthetic_dir(self) -> Path:
        """Folder of the simulated source system (application_dates.csv), outside data/raw."""
        return _path(self.raw["paths"]["synthetic_dir"])

    @property
    def synthetic_dates(self) -> dict[str, Any]:
        """The validated `synthetic_dates` block (generator of the simulated application_dates source)."""
        return validate_synthetic_dates(self.raw["synthetic_dates"])

    @property
    def dss(self) -> dict[str, Any]:
        """The validated `dss` block (grade scale, policy, segment rule, rule catalog)."""
        return validate_dss(self.raw["dss"])

    @property
    def sql_params(self) -> dict[str, str]:
        """Values substituted into {{placeholders}} in src/sql/*.sql.

        The dss_* fragments are generated here (not written in SQL) so the ETL views and the scorer
        (src/dss/rules.py) run the very same expressions taken from configs/settings.yaml.
        """
        t = self.raw["time"]
        dss = self.dss
        seg = dss["segment"]
        return {
            "anchor_date": str(t["anchor_date"]),
            "history_days": str(int(t["history_days"])),
            "future_days": str(int(t["future_days"])),
            "segment_min_n": str(int(dss["segment_min_n"])),
            "dss_trigger_grades_sql": ", ".join(sql_str(g) for g in dss["segment_trigger_grades"]),
            "dss_age_band_sql": _age_band_sql(seg["age_band"]["edges"], seg["age_band"]["labels"]),
            "dss_income_group_sql": (
                "CASE WHEN income_type IS NULL THEN NULL WHEN income_type IN ("
                + ", ".join(sql_str(g) for g in seg["income_groups"])
                + ") THEN income_type ELSE 'Other' END"
            ),
            "dss_rule_hits_sql": _rule_hits_sql(dss["rules"]),
        }

    def bucket(self, name: str) -> tuple[list[float], list[str]]:
        b = self.raw["buckets"][name]
        return [float(e) for e in b["edges"]], list(b["labels"])


GRADES = ("A", "B", "C", "D", "E")
DECISIONS = ("approve", "review", "reject")


def validate_dss(dss: dict[str, Any]) -> dict[str, Any]:
    """Reject a `dss` block the warehouse and scorer cannot use; returns it unchanged."""
    t = [float(x) for x in dss["grade_thresholds"]]
    if len(t) != len(GRADES) - 1 or any(not 0 < x < 1 for x in t) or any(a >= b for a, b in zip(t, t[1:])):
        raise ValueError(f"dss.grade_thresholds must be 4 increasing rates in (0, 1), got {t}")
    policy = dss["policy"]
    if set(policy) != set(GRADES):
        raise ValueError(f"dss.policy must define exactly grades {GRADES}, got {sorted(policy)}")
    for grade, p in policy.items():
        if p["decision"] not in DECISIONS:
            raise ValueError(f"dss.policy.{grade}.decision must be one of {DECISIONS}")
        if float(p["max_credit_to_income"]) < 0:
            raise ValueError(f"dss.policy.{grade}.max_credit_to_income must be >= 0")
    if not set(dss["segment_trigger_grades"]) <= set(GRADES):
        raise ValueError("dss.segment_trigger_grades must be grades A-E")
    missing = set(GRADES) - set(dss.get("grade_labels") or {})
    if missing:
        raise ValueError(f"dss.grade_labels has no label for grades {sorted(missing)}")
    missing = {p["decision"] for p in policy.values()} - set(dss.get("decision_labels") or {})
    if missing:
        raise ValueError(f"dss.decision_labels has no label for decisions {sorted(missing)}")
    band = dss["segment"]["age_band"]
    edges = [float(e) for e in band["edges"]]
    if any(a >= b for a, b in zip(edges, edges[1:])):
        raise ValueError(f"dss.segment.age_band.edges must be strictly increasing, got {band['edges']}")
    if len(band["labels"]) != len(edges) - 1:
        raise ValueError(f"dss.segment.age_band needs {len(edges) - 1} labels for {len(edges)} edges, "
                         f"got {len(band['labels'])}")
    codes = [r["code"] for r in dss["rules"]]
    if len(codes) != len(set(codes)):
        raise ValueError("dss.rules: duplicate rule codes")
    if sum(r["group"] == "segment" for r in dss["rules"]) != 1:
        raise ValueError("dss.rules: exactly one rule must have group 'segment'")
    for r in dss["rules"]:
        if r["group"] not in ("segment", "business"):
            raise ValueError(f"dss.rules {r['code']}: group must be segment or business")
        if (r["group"] == "business") != bool(r.get("sql")):
            raise ValueError(f"dss.rules {r['code']}: business rules need sql, the segment rule has none")
        # dim_rule.requires is NOT NULL and the simulator relies on it, so every rule must list its inputs.
        req = r.get("requires")
        if not isinstance(req, list) or not all(isinstance(c, str) for c in req):
            raise ValueError(f"dss.rules {r['code']}: requires must be a list of column names")
        # Rule sql is inlined into one-line UNION ALL queries: a ';' would start a new statement and
        # '--' would comment out the rest of the line (the following rules).
        if r.get("sql") and (";" in r["sql"] or "--" in r["sql"]):
            raise ValueError(f"dss.rules {r['code']}: sql must be one expression without ';' or '--'")
    return dss


DAYS_PER_MONTH = 30  # month length used by the simulated dates source


def validate_synthetic_dates(cfg: dict[str, Any]) -> dict[str, Any]:
    """Reject generator settings that would produce dates outside their meaning; returns cfg unchanged."""
    if not isinstance(cfg.get("seed"), int) or isinstance(cfg.get("seed"), bool):
        raise ValueError("synthetic_dates.seed must be an integer")
    for key in ("historical_window_days", "new_window_days"):
        w = cfg.get(key)
        if not (isinstance(w, list) and len(w) == 2 and all(isinstance(x, int) for x in w) and w[0] >= w[1] >= 0):
            raise ValueError(f"synthetic_dates.{key} must be [far, near] days before the snapshot, far >= near >= 0")
    m = cfg.get("default_after_months")
    if not (isinstance(m, list) and len(m) == 2 and all(isinstance(x, int) for x in m) and 1 <= m[0] <= m[1]):
        raise ValueError("synthetic_dates.default_after_months must be [min, max] months with 1 <= min <= max")
    r = cfg.get("repaid_observed_months")
    if not isinstance(r, int) or r < 1:
        raise ValueError("synthetic_dates.repaid_observed_months must be an integer >= 1")
    # Historical outcomes are known facts, so even the latest application's outcome is on or before the snapshot.
    longest = max(m[1], r) * DAYS_PER_MONTH
    if cfg["historical_window_days"][1] < longest:
        raise ValueError(f"synthetic_dates.historical_window_days near end must be >= {longest} days so every "
                         "outcome falls on or before the snapshot date")
    return cfg


def sql_str(value: str) -> str:
    """Quote a config string as a SQL literal."""
    return "'" + str(value).replace("'", "''") + "'"


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else repr(float(x))


def _age_band_sql(edges: list[float], labels: list[str]) -> str:
    whens = " ".join(
        f"WHEN age_years >= {_num(lo)} AND age_years < {_num(hi)} THEN {sql_str(label)}"
        for lo, hi, label in zip(edges, edges[1:], labels)
    )
    return f"CASE {whens} END"


def _rule_hits_sql(rules: list[dict[str, Any]]) -> str:
    """(application_id, rule_code) of every business-rule violation in relation `src`; NULL -> no hit.

    The rule sql is trusted config (never user input), so it is inlined as written.
    """
    parts = [
        f"SELECT application_id, {sql_str(r['code'])} AS rule_code FROM src WHERE COALESCE(({r['sql']}), FALSE)"
        for r in rules if r["group"] == "business"
    ]
    return " UNION ALL ".join(parts) or "SELECT NULL::BIGINT AS application_id, NULL::VARCHAR AS rule_code WHERE FALSE"


def _path(value: str | os.PathLike) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (ROOT / p)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    with open(ROOT / "configs" / "settings.yaml", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    paths = raw["paths"]
    return Settings(
        raw=raw,
        dw_target=os.getenv("DW_TARGET", "local").strip().lower(),
        dw_path=_path(os.getenv("DW_PATH", paths["dw_path"])),
        raw_dir=_path(os.getenv("RAW_DIR", paths["raw_dir"])),
        sample_dir=_path(paths["sample_dir"]),
        fixture_dir=_path(paths["fixture_dir"]),
        dq_report=_path(paths["dq_report"]),
        md_database=os.getenv("MD_DATABASE", "home_credit_dw"),
        motherduck_token=os.getenv("MOTHERDUCK_TOKEN") or None,
    )
