import clsx from "clsx";
import {
  AlertOctagon, ArrowDown, ArrowRight, ArrowUp, CheckCircle2, CircleAlert, CircleHelp, Terminal,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import type { Assessment, AssessmentProfile, Decision, Grade, RuleHit, ShapFactor } from "../lib/api";
import { dateLabel, fixed, label, num, pct } from "../lib/format";
import { useTheme } from "../lib/theme";
import { GRADE_LABELS, GradeChip } from "./charts";
import { ErrorBox } from "./ui";

/** Status palette (reserved for decisions), always shown with an icon + a word - never colour alone. */
const DECISION: Record<Decision, { word: string; icon: typeof CheckCircle2; cls: string }> = {
  approve: { word: "Approve", icon: CheckCircle2, cls: "pill-ok" },
  review: { word: "Manual review", icon: CircleAlert, cls: "pill-warn" },
  reject: { word: "Reject", icon: AlertOctagon, cls: "pill-bad" },
};

export function DecisionBadge({ decision, text, size = "md" }: { decision: Decision; text?: string; size?: "sm" | "md" | "lg" }) {
  const d = DECISION[decision] ?? DECISION.review;
  const Icon = d.icon;
  return (
    <span className={clsx("pill", size === "sm" ? "text-xs" : size === "lg" ? "gap-2 px-4 py-2 text-[17px] font-bold" : "px-3 py-1 text-[13px]", d.cls)}>
      <Icon className={size === "sm" ? "h-3.5 w-3.5" : size === "lg" ? "h-5 w-5" : "h-4 w-4"} aria-hidden />
      {text ?? d.word}
    </span>
  );
}

/**
 * Error panel for the DSS endpoints: 503 means the model / batch scores are missing (one command
 * fixes it), 404 an unknown application, anything else (400 / 422 validation) shows the API message.
 */
export function DssError({ status, message, notFound }: { status?: number; message: string; notFound?: ReactNode }) {
  if (status === 503) {
    return (
      <div className="callout flex items-start gap-2 text-[13px] text-ink-2">
        <Terminal className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
        <span>
          {/* The server's 503 detail is a complete sentence that already says what to run. */}
          {message}
        </span>
      </div>
    );
  }
  if (status === 404 && notFound) {
    return (
      <div className="flex items-start gap-2 rounded-lg border border-line bg-raised/60 p-3 text-[13px] text-ink-2">
        <CircleHelp className="mt-0.5 h-4 w-4 shrink-0 text-ink-3" />
        <span>{notFound}</span>
      </div>
    );
  }
  return <ErrorBox message={message} />;
}

/** Outcome pill: "Repaid" (good), "Defaulted" (bad) or "No outcome yet" (new applications), date underneath. */
export function OutcomePill({ a }: { a: Assessment }) {
  const date = a.profile?.outcome_date;
  const months = a.profile?.months_to_outcome;
  if (a.target !== 0 && a.target !== 1) return <span className="pill pill-neutral">No outcome yet</span>;
  const bad = a.target === 1;
  return (
    <span className="inline-flex flex-col items-start gap-1">
      <span className={bad ? "pill pill-bad" : "pill pill-ok"}>{bad ? "Defaulted" : "Repaid"}</span>
      {date && (
        <span className="num text-[12.5px] text-ink-2">
          {bad ? <>{dateLabel(date)}{months != null && ` · ${months} mo after`}</> : dateLabel(date)}
        </span>
      )}
    </span>
  );
}

/** Decision band: final grade, decision, outcome, and requested vs recommended amount. */
export function DecisionBand({ a }: { a: Assessment }) {
  return (
    <div className="grid gap-4 sm:grid-cols-3 lg:grid-cols-[auto_auto_auto_minmax(0,1fr)] lg:items-center lg:gap-6">
      <div className="flex items-center gap-3">
        <GradeChip grade={a.final_grade} size="lg" />
        <div className="min-w-0">
          <p className="text-[18px] font-extrabold leading-tight tracking-[-0.01em] text-ink">{GRADE_LABELS[a.final_grade]}</p>
          <p className="text-[12.5px] text-ink-2">Final grade {a.final_grade}</p>
        </div>
      </div>
      <div className="lg:border-l lg:border-line lg:pl-6">
        <p className="mb-1 text-[12.5px] font-medium text-ink-2">Decision</p>
        <DecisionBadge decision={a.decision} text={a.decision_label} />
      </div>
      <div className="lg:border-l lg:border-line lg:pl-6">
        <p className="mb-1 text-[12.5px] font-medium text-ink-2">Outcome</p>
        <OutcomePill a={a} />
      </div>
      <div className="sm:col-span-3 lg:col-span-1 lg:border-l lg:border-line lg:pl-6">
        <Amounts a={a} />
      </div>
    </div>
  );
}

/** Requested → recommended, side by side; one short reason only when the amount was cut. */
function Amounts({ a }: { a: Assessment }) {
  const requested = a.amt_credit ?? 0;
  const rec = a.recommended_credit ?? 0;
  const cap = a.max_credit_to_income ?? 0;
  const none = a.decision === "reject" || cap === 0;
  const cut = rec < requested - 0.5;
  return (
    <div>
      <div className="flex flex-wrap items-end gap-x-5 gap-y-2">
        <div>
          <p className="text-[12.5px] font-medium text-ink-2">Requested</p>
          <p className="big-num mt-0.5 text-[26px] leading-none text-ink">{num(requested)}</p>
        </div>
        <ArrowRight className="mb-1 h-5 w-5 text-ink-3" aria-hidden />
        <div>
          <p className="text-[12.5px] font-medium text-ink-2">Recommended</p>
          <p className={clsx("big-num mt-0.5 text-[26px] leading-none", none ? "text-bad" : cut ? "text-accent" : "text-ink")}>{num(rec)}</p>
        </div>
      </div>
      {(none || cut) && (
        <p className="mt-1.5 text-[12.5px] text-ink-2">
          {none ? `No lending at grade ${a.final_grade}` : `Capped at ${fixed(cap, Number.isInteger(cap) ? 0 : 1)}× income`}
        </p>
      )}
    </div>
  );
}

// ---- how the grade was set ------------------------------------------------------------------------

/** "Segment: No bureau data | 25-34 | Working | Region 2" → "No bureau data · 25-34 · Working · Region 2". */
const ruleText = (h: RuleHit) => (h.group === "segment" ? h.label.replace(/^Segment:\s*/, "").split(/\s*\|\s*/).join(" · ") : h.label);
/** Short name for a business rule: the part before the colon ("Frequent late payer"). */
const shortRule = (h: RuleHit) => (h.group === "segment" ? "Customer segment" : h.label.split(":")[0]);

const SETS: Record<Assessment["grade_source"], { score: boolean; rule: boolean }> = {
  score: { score: true, rule: false },
  rule: { score: false, rule: true },
  both: { score: true, rule: true },
};

/**
 * "Grade breakdown": one section, two equal columns — Model (score grade, PD and the risk drivers) and
 * Rules (rule grade and the triggered rules). The final grade is the worse of the two (shown in the decision band);
 * the column that set it gets the accent border (both when they agree).
 */
export function GradeSection({ a }: { a: Assessment }) {
  const hits = [...(a.rule_hits ?? [])].sort((x, y) => (y.grade > x.grade ? 1 : y.grade < x.grade ? -1 : x.code.localeCompare(y.code)));
  const sets = SETS[a.grade_source] ?? SETS.score;
  return (
    <section className="card px-5 py-4" aria-labelledby="grade-section-title">
      <header className="mb-3">
        <h2 id="grade-section-title" className="text-[15px] font-bold text-ink">Grade breakdown</h2>
        {/* The API's one-line verdict, e.g. "Downgraded from B to D by segment rule BR-SEG-01." */}
        {a.summary && <p className="mt-0.5 text-[12.5px] text-ink-2">{a.summary}</p>}
      </header>

      <div className="grid gap-4 lg:grid-cols-2">
        <Column title="Model" grade={a.score_grade} sets={sets.score}
          detail={<>PD <b className="num text-ink">{pct(a.pd, 1)}</b></>}>
          <RiskDrivers a={a} />
        </Column>
        <Column title="Rules" grade={a.rule_grade} sets={sets.rule && !!a.rule_grade}
          detail={hits.length ? `${hits.length} rule${hits.length === 1 ? "" : "s"} triggered` : "No rule triggered"}>
          {hits.length === 0 ? (
            <p className="flex items-center gap-2 rounded-lg bg-raised/60 p-3 text-[13px] text-ink-2">
              <CheckCircle2 className="h-4 w-4 text-good" aria-hidden /> No underwriting rule applies to this application.
            </p>
          ) : (
            <ul className="divide-y divide-line rounded-lg border border-line">
              {hits.map((h) => (
                <li key={`${h.code}-${h.rate_key ?? ""}`} className="flex min-w-0 items-center gap-2.5 px-3 py-2 text-[13px]">
                  <GradeChip grade={h.grade} size="sm" />
                  <span className="shrink-0 font-mono text-[12px] font-semibold text-ink">{h.code}</span>
                  <span className="min-w-0 flex-1 truncate text-ink" title={h.group === "segment" ? `${shortRule(h)} · ${ruleText(h)}` : h.label}>
                    {shortRule(h)}
                    {h.group === "segment" && <span className="text-ink-2"> · {ruleText(h)}</span>}
                  </span>
                  <span className="num shrink-0 text-right text-[12.5px] text-ink-2" title="Historical default rate of loans that break this rule">
                    <b className="text-ink">{pct(h.rate, 1)}</b> <span className="hidden xl:inline">default</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Column>
      </div>
    </section>
  );
}

function Column({ title, grade, detail, sets, children }: {
  title: string; grade: Grade | null; detail: ReactNode; sets: boolean; children: ReactNode;
}) {
  return (
    <section aria-label={title} className={clsx("min-w-0 rounded-xl border-[1.5px] p-3.5", sets ? "border-accent" : "border-line")}>
      <div className="mb-3 flex items-center gap-2.5 border-b border-line pb-2.5">
        <GradeChip grade={grade} />
        <div className="min-w-0 flex-1">
          <p className="flex items-center gap-2 text-[13px] font-semibold text-ink">
            {title}
            {sets && <span className="text-[11px] font-semibold text-accent-ink">sets final grade</span>}
          </p>
          <p className="truncate text-[13px] text-ink-2">
            {grade ? <><span className="font-semibold text-ink">{GRADE_LABELS[grade]}</span> · </> : null}{detail}
          </p>
        </div>
      </div>
      {children}
    </section>
  );
}

// ---- risk drivers -----------------------------------------------------------------------------------

/** Feature value for display: ratios / scores with up to 3 decimals, large amounts grouped. */
function factorValue(v: ShapFactor["value"]): string {
  if (v === null || v === undefined || (typeof v === "number" && !Number.isFinite(v))) return "unknown";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "string") return label(v);
  if (Math.abs(v) >= 1e4 || Number.isInteger(v)) return num(v);
  return Math.abs(v) < 10 ? v.toFixed(3).replace(/\.?0+$/, "") : num(v, 1);
}

/**
 * Risk drivers: top 3 factors raising and top 3 lowering the PD (SHAP), each "Label = value" with the
 * warehouse column in small mono (BR-08); "Show all factors" lists every factor returned.
 */
function RiskDrivers({ a }: { a: Assessment }) {
  const [all, setAll] = useState(false);
  const { palette } = useTheme();
  const up = [...(a.shap?.up ?? [])].sort((x, y) => y.shap - x.shap);
  const down = [...(a.shap?.down ?? [])].sort((x, y) => x.shap - y.shap);
  const max = Math.max(1e-9, ...up.map((f) => Math.abs(f.shap)), ...down.map((f) => Math.abs(f.shap)));
  const hidden = Math.max(0, up.length - 3) + Math.max(0, down.length - 3);
  if (!up.length && !down.length) return <p className="text-[13px] text-ink-2">No factor contributions returned.</p>;

  const group = (title: string, list: ShapFactor[], raise: boolean) => (
    <div>
      <p className={clsx("mb-2 flex items-center gap-1 text-[13px] font-semibold", raise ? "text-bad" : "text-good")}>
        {raise ? <ArrowUp className="h-3.5 w-3.5" aria-hidden /> : <ArrowDown className="h-3.5 w-3.5" aria-hidden />} {title}
      </p>
      <ul className="space-y-2">
        {(all ? list : list.slice(0, 3)).map((f) => (
          <li key={f.feature} className="grid grid-cols-[minmax(0,1fr)_96px] items-center gap-3"
            title={`${f.label} (${f.feature}) = ${factorValue(f.value)} · SHAP ${f.shap > 0 ? "+" : ""}${fixed(f.shap, 3)}`}>
            <span className="min-w-0">
              <span className="block truncate text-[13px] text-ink">{f.label} = <b className="num">{factorValue(f.value)}</b></span>
              <span className="block truncate font-mono text-[11px] text-ink-2">{f.feature}</span>
            </span>
            <span className="flex items-center gap-1.5">
              <span className="h-2 flex-1 overflow-hidden rounded-full bg-raised" aria-hidden>
                <span className="block h-full rounded-full"
                  style={{ width: `${(Math.abs(f.shap) / max) * 100}%`, background: raise ? palette.status.critical : palette.status.good }} />
              </span>
              <span className="num w-9 text-right text-[11px] text-ink-2">{f.shap > 0 ? "+" : ""}{fixed(f.shap, 2)}</span>
            </span>
          </li>
        ))}
        {list.length === 0 && <li className="text-[12.5px] text-ink-2">None</li>}
      </ul>
    </div>
  );

  return (
    <div className="space-y-5">
      {group("Raises risk", up, true)}
      {group("Lowers risk", down, false)}
      {hidden > 0 && (
        <button type="button" className="text-[12.5px] font-semibold text-accent-ink hover:underline" aria-expanded={all}
          onClick={() => setAll((x) => !x)}>
          {all ? "Show top factors only" : `Show all factors (${hidden} more)`}
        </button>
      )}
    </div>
  );
}

// ---- applicant --------------------------------------------------------------------------------------

/**
 * Applicant information: three compact columns (Profile · Money · History) of key–value rows, label left and
 * value right on one line; long values are truncated with a tooltip. Missing values read "unknown".
 */
export function ApplicantFacts({ p, creditToIncome }: { p: AssessmentProfile; creditToIncome?: number | null }) {
  const unknown = "unknown";
  const text = (v: string | null) => (v ? label(v) : unknown);
  const share = (v: number | null) => (v == null ? unknown : pct(v, 0));
  const money = (v: number | null) => (v == null ? unknown : num(v));
  const cti = creditToIncome ?? (p.amt_credit != null && p.amt_income ? p.amt_credit / p.amt_income : null);
  const groups: [string, [string, string][]][] = [
    ["Profile", [
      ["Age", p.age_years == null ? unknown : `${Math.floor(p.age_years)} years`],
      ["Employment", text(p.income_type)],
      ["Occupation", text(p.occupation_type)],
      ["Education", text(p.education_type)],
      ["Years employed", p.years_employed == null ? unknown : `${fixed(p.years_employed, 1)} years`],
      ["Region rating", p.region_rating_client == null ? unknown : `${p.region_rating_client} of 3`],
    ]],
    ["Money", [
      ["Annual income", money(p.amt_income)],
      ["Credit requested", money(p.amt_credit)],
      ["Annuity", money(p.amt_annuity)],
      ["Credit ÷ income", cti == null ? unknown : `${fixed(cti, 2)}×`],
    ]],
    ["History", [
      ["Credit history", text(p.credit_history_status)],
      ["External score", p.ext_source_mean == null ? unknown : fixed(p.ext_source_mean, 2)],
      ["Worst days late", p.max_dpd_days == null ? unknown : `${num(p.max_dpd_days)} days`],
      ["Late instalments", share(p.late_payment_ratio)],
      ["Previous approval rate", share(p.prev_approval_rate)],
      ["Open bureau credits", p.cnt_bureau_active == null ? unknown : num(p.cnt_bureau_active)],
    ]],
  ];
  return (
    <div className="grid gap-x-8 gap-y-4 md:grid-cols-3">
      {groups.map(([g, items]) => (
        <section key={g} aria-label={g} className="min-w-0">
          <h3 className="mb-1 text-[11.5px] font-semibold uppercase tracking-[0.06em] text-ink-2">{g}</h3>
          <dl>
            {items.map(([k, v]) => (
              <div key={k} className="flex h-6 items-center justify-between gap-3 border-b border-line/60 text-[13px] last:border-b-0">
                <dt className="shrink-0 text-ink-2">{k}</dt>
                <dd className={clsx("num min-w-0 truncate text-right", v === unknown ? "italic text-ink-2" : "font-medium text-ink")} title={v}>{v}</dd>
              </div>
            ))}
          </dl>
        </section>
      ))}
    </div>
  );
}
