import clsx from "clsx";
import { useState } from "react";
import sys from "../../../docs/diagrams/01_system_architecture.svg?raw";
import etl from "../../../docs/diagrams/02_etl_pipeline.svg?raw";
import star from "../../../docs/diagrams/03_star_schema.svg?raw";
import lineage from "../../../docs/diagrams/04_data_lineage.svg?raw";
import request from "../../../docs/diagrams/05_request_flow.svg?raw";
import dss from "../../../docs/diagrams/06_dss_flow.svg?raw";
import olap from "../../../docs/diagrams/07_analytics_olap.svg?raw";
import scope from "../../../docs/diagrams/08_kpi_scope.svg?raw";
import { Diagram } from "../components/Diagram";
import { Card, PageHeader } from "../components/ui";

const DIAGRAMS = [
  {
    key: "system", name: "01_system_architecture", title: "System architecture (C4)", code: sys,
    points: [
      "The warehouse is a single DuckDB file: columnar, vectorised, no server to run - full refresh of 2.7 GB of CSV in about 15 s (≈ 1.5 min with --score).",
      "The API is the only reader of the warehouse; the browser never sees SQL it can change (dimensions are whitelisted, values are bound parameters).",
      "MotherDuck is an optional shared copy so teammates can query without downloading the data.",
    ],
  },
  {
    key: "etl", name: "02_etl_pipeline", title: "ETL pipeline", code: etl,
    points: [
      "Extract reads every column as text and TRY_CASTs to the declared staging type, so one bad value never aborts a 27M-row load.",
      "Cleaning rules R1–R10 are in pandas (readable, unit-tested); heavy aggregation is in SQL (fast).",
      "Quality gate: 50 checks - primary keys, foreign keys, row completeness and money reconciliation (staging total = fact total).",
    ],
  },
  {
    key: "star", name: "03_star_schema", title: "Star schema (fact constellation)", code: star,
    points: [
      "Grain: one loan application (SK_ID_CURR) and one bureau credit (SK_ID_BUREAU).",
      "dim_credit_history is a mini-dimension: tens of millions of history rows collapsed into ~200 bucketed profiles.",
      "dim_bureau_category is a junk dimension; dim_time is role-playing on both facts: application/outcome date (simulated) and bureau open/close date.",
      "The DSS adds dim_risk_grade, dim_rule, fact_risk_assessment and fact_rule_hit (one row per application and per rule hit).",
    ],
  },
  {
    key: "lineage", name: "04_data_lineage", title: "Data lineage", code: lineage,
    points: [
      "Child tables (1..N rows per applicant) are collapsed to one credit profile per applicant before they reach a dimension.",
      "fact_bureau_credit keeps the bureau grain and links back to dim_customer for drill-across.",
    ],
  },
  {
    key: "request", name: "05_request_flow", title: "Query flow (Analytics request)", code: request,
    points: [
      "Each Analytics panel is one POST /api/analytics/cube; one GROUPING SETS query returns cells, subtotals and the grand total (about 15–60 ms on 356K applications, not cached).",
      "Overview queries (/api/by, KPI summary) are memoised: the warehouse is a static snapshot until the next ETL.",
    ],
  },
  {
    key: "dss", name: "06_dss_flow", title: "Decision support flow", code: dss,
    points: [
      "Score grade: LightGBM on the warehouse (mart.v_application_wide), 5-fold out-of-fold PD, isotonic-calibrated; gender is not a feature. PD cut-offs 1% / 7% / 13% / 20% give A–E.",
      "Rule grade: the segment rule BR-SEG-01 (customer segments graded D or E) and business rules BR-RF-01..05, each graded by its historical default rate; the rule grade is the worst rule violated.",
      "Final grade = the worse of the two, so rules can only downgrade, never upgrade.",
      "Policy per grade: A / B Approve (up to 8× / 6× income), C / D Manual review (4× / 3×), E Reject; tuned in the Playground, including each rule's switch and threshold.",
      "Explanation: top SHAP factors up and down, the violated rules with code, rate and grade, and one summary sentence (e.g. “Downgraded from B to D by segment rule BR-SEG-01.”).",
    ],
  },
  {
    key: "olap", name: "07_analytics_olap", title: "Analytics: OLAP operations", code: olap,
    points: [
      "Slice / dice = filter-bar chips → conditions[] → WHERE with bound parameters.",
      "Roll-up / drill-down = Interval (Year / Quarter / Month) and ▲ / ▼ / × / + on dimension chips → levels in rows[] → GROUPING SETS subtotals.",
      "Pivot = ⇄ (rows and columns swap); Measure select = measure (Default rate on historical loans only).",
      "Drill-through: clicking a cell, bar or point opens the Explorer with that cell's members as conditions.",
      "Four fixed panels (applications, default rate, grade mix over time; default rate by segment) plus 6 saved SQL queries.",
    ],
  },
  {
    key: "scope", name: "08_kpi_scope", title: "KPI scope", code: scope,
    points: [
      "All scored applications (356,255) = historical loans (307,511, with outcome) + new applications (48,744, no outcome yet).",
      "On all scored applications: counts, decision rates, total credit, credit at risk, grade mix, average PD, rule loans hit.",
      "On historical loans only: default rates (including approved), rule default rates, AUC, outcome breakdown and the waffle.",
      "New applications have no outcome, so default rate over time ends around 2017-05.",
    ],
  },
];

const STACK = [
  ["Storage & OLAP", "DuckDB (local file) · MotherDuck (optional cloud copy)"],
  ["ETL", "Python 3.11 · pandas · DuckDB SQL · Typer CLI (hcdw)"],
  ["Backend", "FastAPI · Uvicorn · whitelisted SQL with bound parameters"],
  ["Frontend", "React 18 · TypeScript · Tailwind CSS · Recharts · hand-drawn SVG diagrams"],
  ["Decision support", "LightGBM · scikit-learn (isotonic calibration) · SHAP · A–E grading & rules"],
  ["Quality", "pytest (ETL, API, DSS: model, grading, rules, policy) · data-quality report per run"],
];

export default function Architecture() {
  // ?diagram=star opens one diagram directly (deep link; used by scripts/capture_figures.py).
  const [active, setActive] = useState(() => {
    const want = new URLSearchParams(window.location.search).get("diagram");
    return DIAGRAMS.some((x) => x.key === want) ? want! : DIAGRAMS[0].key;
  });
  const d = DIAGRAMS.find((x) => x.key === active)!;
  return (
    <div className="space-y-6">
      <PageHeader title="Architecture" />

      <div className="flex flex-wrap gap-1 rounded-2xl border-[1.5px] border-line bg-surface p-1">
        {DIAGRAMS.map((x, i) => (
          <button key={x.key} onClick={() => setActive(x.key)}
            className={clsx("whitespace-nowrap rounded-full px-3 py-1.5 text-[13px] font-medium transition",
              active === x.key ? "bg-accent text-accent-on" : "text-ink-2 hover:bg-raised")}>
            <span className={clsx("num mr-1.5", active === x.key ? "opacity-70" : "text-ink-3")}>{i + 1}</span>{x.title}
          </button>
        ))}
      </div>

      <Card title={d.title}>
        <Diagram key={d.key} svg={d.code} name={d.name} />
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Design notes">
          <ul className="space-y-2.5">
            {d.points.map((p) => (
              <li key={p} className="flex gap-2 text-[13px] leading-relaxed text-ink-2">
                <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-accent" /> {p}
              </li>
            ))}
          </ul>
          <p className="mt-4 font-mono text-[11px] text-ink-3">docs/diagrams/{d.name}.svg</p>
        </Card>
        <Card title="Tech stack">
          <dl className="grid gap-x-6 gap-y-2.5 sm:grid-cols-2">
            {STACK.map(([k, v]) => (
              <div key={k}>
                <dt className="eyebrow">{k}</dt>
                <dd className="text-[13px] text-ink-2">{v}</dd>
              </div>
            ))}
          </dl>
        </Card>
      </div>
    </div>
  );
}
