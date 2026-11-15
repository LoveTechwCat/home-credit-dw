import clsx from "clsx";
import { CheckCircle2, CircleAlert } from "lucide-react";
import { useState } from "react";
import { DecisionBadge, DssError } from "../components/AssessmentView";
import { CalibrationChart, GRADE_LABELS, GradeChip, ImportanceBars, MIN_RELIABLE } from "../components/charts";
import { Card, ChartSkeleton, PageHeader, Segmented, StatTile } from "../components/ui";
import { useApi, type GradeShareRow, type GradeType, type GlobalExplain, type GradesResponse } from "../lib/api";
import { fixed, num, pct } from "../lib/format";

type Scope = "all" | "train" | "test";

/** Grades & model (/grades): reference page for the grade scale and the model behind the score grade. */
export default function GradePolicy() {
  const [scope, setScope] = useState<Scope>("all");
  const [type, setType] = useState<GradeType>("final");
  const grades = useApi<GradesResponse>(`/api/grades?scope=${scope}`);
  const explain = useApi<GlobalExplain>("/api/explain/global");
  const g = grades.data;
  const e = explain.data;

  return (
    <div className="space-y-6">
      <PageHeader title="Grades & model">
        <Segmented<Scope> ariaLabel="Applications" value={scope} onChange={setScope}
          options={[{ value: "all", label: "All applications" }, { value: "train", label: "Historical loans" }, { value: "test", label: "New applications" }]} />
      </PageHeader>

      {grades.error && <DssError status={grades.status} message={grades.error} />}

      <div className="grid grid-cols-2 gap-3 lg:max-w-2xl">
        <StatTile label="Downgraded by rules" accent loading={!g && !grades.error} value={pct(g?.downgraded_share)} />
        <StatTile label="Model AUC" loading={!e && !explain.error} value={fixed(e?.auc_oof, 3)} />
      </div>

      <Card title="Grade scale"
        actions={<Segmented<GradeType> ariaLabel="Grade type" value={type} onChange={setType}
          options={[{ value: "score", label: "Score" }, { value: "rule", label: "Rule" }, { value: "final", label: "Final" }]} />}>
        {g ? <GradeTable g={g} type={type} scope={scope} /> : grades.error ? null : <ChartSkeleton height={220} />}
      </Card>

      <div className="grid gap-6 xl:grid-cols-2">
        <Card title="What drives the score">
          {e ? (
            <>
              <ImportanceBars data={e.features.slice(0, 12)} />
              <span className={clsx("pill mt-3", e.gender_in_features ? "pill-bad" : "pill-ok")}>
                {e.gender_in_features
                  ? <><CircleAlert className="h-3.5 w-3.5" aria-hidden /> Gender used</>
                  : <><CheckCircle2 className="h-3.5 w-3.5" aria-hidden /> Gender not used</>}
              </span>
            </>
          ) : explain.error ? <DssError status={explain.status} message={explain.error} /> : <ChartSkeleton />}
        </Card>
        <Card title="PD vs actual default">
          {e ? (
            <>
              <CalibrationChart data={e.calibration} />
            </>
          ) : explain.error ? <DssError status={explain.status} message={explain.error} /> : <ChartSkeleton />}
        </Card>
      </div>
    </div>
  );
}

/** Grade, risk range, decision, cap, share and default rate (inline bars) for one grade type. */
function GradeTable({ g, type, scope }: { g: GradesResponse; type: GradeType; scope: Scope }) {
  const policy = new Map(g.policy.map((p) => [p.grade, p]));
  const rows = g.distribution[type];
  const maxShare = Math.max(1e-9, ...rows.map((r) => r.share ?? 0));
  const maxRate = Math.max(1e-9, ...rows.map((r) => r.default_rate ?? 0));
  const bar = (v: number | null, max: number) => (
    <div className="hidden h-1.5 w-20 overflow-hidden rounded-full bg-raised sm:block" aria-hidden>
      <div className="h-full rounded-full bg-accent" style={{ width: `${((v ?? 0) / max) * 100}%` }} />
    </div>
  );
  return (
    <>
      <div className="overflow-x-auto">
        <table className="table">
          <thead>
            <tr>
              <th>Grade</th><th>Risk range</th><th>Decision</th><th className="text-right">Max credit ÷ income</th>
              <th className="text-right">Share</th><th className="text-right">Default rate</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const p = r.grade ? policy.get(r.grade) : undefined;
              return (
                <tr key={r.grade ?? "none"}>
                  <td>
                    <span className="inline-flex items-center gap-2">
                      <GradeChip grade={r.grade} size="sm" />
                      <span className="text-ink-2">{r.grade ? p?.label ?? GRADE_LABELS[r.grade] : "No rule hit"}</span>
                    </span>
                  </td>
                  <td className="num text-ink-2">{p ? `${pct(p.min_rate, 0)} – ${p.max_rate == null ? "100%" : pct(p.max_rate, 0)}` : "–"}</td>
                  <td>{p ? <DecisionBadge decision={p.decision} text={p.decision_label} size="sm" /> : <span className="text-ink-2">–</span>}</td>
                  <td className="num text-right text-ink">{p ? fixed(p.max_credit_to_income, 1, "×") : "–"}</td>
                  <td className="num text-right">
                    <div className="flex items-center justify-end gap-2">{bar(r.share, maxShare)}
                      <span className="w-16 text-ink" title={`${num(r.n)} loans`}>{pct(r.share, 1)}</span></div>
                  </td>
                  <td className="num text-right">
                    <div className="flex items-center justify-end gap-2">{bar(r.default_rate, maxRate)}
                      <span className="w-14 text-ink">{pct(r.default_rate, 2)}</span></div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {scope !== "test" && <Monotonic rows={rows} />}
    </>
  );
}

/** Checks BR-07 on grades with enough historical loans. */
function Monotonic({ rows }: { rows: GradeShareRow[] }) {
  const pts = rows.filter((r) => r.grade && r.default_rate != null && r.n >= MIN_RELIABLE);
  if (pts.length < 2) return null;
  const ok = pts.every((r, i) => i === 0 || (r.default_rate as number) >= (pts[i - 1].default_rate as number));
  return (
    <span className={clsx("pill mt-3", ok ? "pill-ok" : "pill-bad")}
      title={`Grades with at least ${MIN_RELIABLE} loans: ${pts.map((p) => p.grade).join(", ")}`}>
      {ok ? <CheckCircle2 className="h-3.5 w-3.5" aria-hidden /> : <CircleAlert className="h-3.5 w-3.5" aria-hidden />}
      {ok ? "Default rises A→E" : "Default does not rise A→E"}
    </span>
  );
}
