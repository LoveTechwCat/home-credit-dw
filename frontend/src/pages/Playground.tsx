import clsx from "clsx";
import { ArrowDown, ArrowRight, ArrowUp, Minus, Plus, RotateCcw, Wand2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { DecisionBadge, DssError } from "../components/AssessmentView";
import { GradeChip } from "../components/charts";
import { Card, ChartSkeleton, PageHeader } from "../components/ui";
import {
  ApiError, GRADES, postJson, useApi, type Decision, type Grade, type GradesResponse,
  type PolicySimRequest, type PolicySimResponse, type RuleDef, type SimRule,
} from "../lib/api";
import { fixed, num, pct } from "../lib/format";

/** Money the way the business reads it: "93.1B". */
const money = (v: number | null | undefined) =>
  v == null ? "–" : Math.abs(v) >= 1e9 ? `${fixed(v / 1e9, 1)}B` : Math.abs(v) >= 1e6 ? `${fixed(v / 1e6, 1)}M` : num(v);
const DEBOUNCE_MS = 150;

interface Draft {
  thresholds: number[]; // fractions, ordered, 0.5 pp steps
  policy: Record<Grade, { decision: Decision; max: number }>;
  /** Per-rule switch and threshold (null for toggle-only rules); empty when the API has no rule catalog. */
  rules: Record<string, { enabled: boolean; value: number | null }>;
}

function draftFrom(g: GradesResponse, catalog: RuleDef[]): Draft {
  const policy = {} as Draft["policy"];
  for (const grade of GRADES) {
    const p = g.policy.find((x) => x.grade === grade);
    policy[grade] = { decision: p?.decision ?? "review", max: p?.max_credit_to_income ?? 0 };
  }
  const rules: Draft["rules"] = {};
  for (const r of catalog) rules[r.code] = { enabled: true, value: r.param?.value ?? null };
  return { thresholds: [...g.thresholds], policy, rules };
}

/** Request body; `rules` carries only the rules that differ from the configured ones. */
function toRequest(d: Draft, catalog: RuleDef[]): PolicySimRequest {
  const rules: NonNullable<PolicySimRequest["rules"]> = {};
  for (const r of catalog) {
    const x = d.rules[r.code];
    if (!x) continue;
    const valueChanged = r.param != null && x.value != null && Math.abs(x.value - r.param.value) > 1e-9;
    if (!x.enabled || valueChanged) rules[r.code] = valueChanged ? { enabled: x.enabled, value: x.value as number } : { enabled: x.enabled };
  }
  return {
    thresholds: d.thresholds,
    policy: Object.fromEntries(GRADES.map((g) => [g, { decision: d.policy[g].decision, max_credit_to_income: d.policy[g].max }])) as PolicySimRequest["policy"],
    // Rules are switched per rule (all off = score only, verified equal on the server), so always "final".
    grade_type: "final",
    ...(Object.keys(rules).length ? { rules } : {}),
  };
}

/** Scenario chips (same meaning as before); each applies on top of the current draft. */
const PRESETS: { label: string; apply: (d: Draft) => Draft }[] = [
  { label: "Approve C", apply: (d) => ({ ...d, policy: { ...d.policy, C: { ...d.policy.C, decision: "approve" } } }) },
  { label: "Loosen", apply: (d) => ({ ...d, thresholds: [0.02, 0.09, 0.15, 0.25] }) },
  { label: "Tighten", apply: (d) => ({ ...d, thresholds: [0.005, 0.04, 0.1, 0.15] }) },
  {
    label: "Rules off",
    apply: (d) => ({ ...d, rules: Object.fromEntries(Object.entries(d.rules).map(([k, v]) => [k, { ...v, enabled: false }])) }),
  },
];

/** Playground (/playground): change the policy and watch five KPIs on all scored applications update live (default rate on historical loans). */
export default function Playground() {
  const grades = useApi<GradesResponse>("/api/grades");
  // Without the rule catalog (API error) the page still works, just without the Rules card.
  const rules = useApi<RuleDef[]>("/api/rules");
  const rulesSettled = !!rules.data || !!rules.error;
  return grades.data && rulesSettled ? <PolicyPlayground g={grades.data} catalog={Array.isArray(rules.data) ? rules.data : []} />
    : (
      <div className="space-y-6">
        <PageHeader title="Playground" />
        {grades.error ? <DssError status={grades.status} message={grades.error} /> : <ChartSkeleton height={320} />}
      </div>
    );
}

function PolicyPlayground({ g, catalog }: { g: GradesResponse; catalog: RuleDef[] }) {
  const initial = useMemo(() => draftFrom(g, catalog), [g, catalog]);
  const [draft, setDraft] = useState<Draft>(initial);
  const changed = JSON.stringify(draft) !== JSON.stringify(initial);

  // "Current" is always the configured policy as is (rules on).
  const [current, setCurrent] = useState<PolicySimResponse | null>(null);
  const [sim, setSim] = useState<PolicySimResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<{ message: string; status?: number } | null>(null);

  useEffect(() => {
    let live = true;
    postJson<PolicySimResponse>("policy/simulate", toRequest(initial, catalog))
      .then((r) => { if (live) setCurrent(r); })
      .catch((e: Error) => { if (live) setErr({ message: e.message, status: e instanceof ApiError ? e.status : undefined }); });
    return () => { live = false; };
  }, [initial, catalog]);

  // Live simulation: debounced, and only the newest answer is kept (older ones are dropped on arrival).
  const seq = useRef(0);
  useEffect(() => {
    if (!changed) { setSim(null); setLoading(false); setErr(null); return; }
    const id = ++seq.current;
    setLoading(true);
    const t = setTimeout(() => {
      postJson<PolicySimResponse>("policy/simulate", toRequest(draft, catalog))
        .then((r) => { if (id === seq.current) { setSim(r); setErr(null); } })
        .catch((e: Error) => { if (id === seq.current) setErr({ message: e.message, status: e instanceof ApiError ? e.status : undefined }); })
        .finally(() => { if (id === seq.current) setLoading(false); });
    }, DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [draft, changed, catalog]);

  const shown = changed ? sim : current; // what the per-grade table describes
  const moves = useMemo(() => aggregateMoves(changed ? sim : null), [changed, sim]);
  const setCuts = (thresholds: number[]) => setDraft((d) => ({ ...d, thresholds }));
  const setPolicy = (grade: Grade, patch: Partial<Draft["policy"][Grade]>) =>
    setDraft((d) => ({ ...d, policy: { ...d.policy, [grade]: { ...d.policy[grade], ...patch } } }));

  return (
    <div className="space-y-5">
      {/* 1. Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <PageHeader title="Playground" />
        <div className="flex flex-wrap items-center gap-2">
          {PRESETS.map((p) => (
            <button key={p.label} type="button" className="chip" onClick={() => setDraft((d) => p.apply(d))}>
              <Wand2 className="h-3.5 w-3.5" aria-hidden /> {p.label}
            </button>
          ))}
          <button type="button" className="chip" onClick={() => setDraft(initial)} disabled={!changed}>
            <RotateCcw className="h-3.5 w-3.5" aria-hidden /> Reset
          </button>
        </div>
      </div>

      {err && <DssError status={err.status} message={err.message} />}

      {/* 2. Hero: five KPIs, current → simulated */}
      <Hero current={current} sim={changed ? sim : null} loading={loading} />

      {/* 4. Decision mix: current vs simulated bars, then who moves */}
      <Card title="Decision mix">
        {current ? <DecisionMix current={current} sim={changed ? sim : null} changed={changed} loading={loading} />
          : <ChartSkeleton height={96} />}
        {moves.length > 0 && <div className="mt-5"><WhoMoves rows={moves} current={current} loading={loading} /></div>}
      </Card>

      {/* 5. Policy by grade */}
      <Card title="Policy by grade">
        <PolicyTable draft={draft} shown={shown} loading={loading && changed} onPolicy={setPolicy} onCuts={setCuts} />
      </Card>

      {/* 6. Rules */}
      {catalog.length > 0 && (
        <Card title="Rules">
          <RulesTable catalog={catalog} draft={draft} results={shown?.rules} loading={loading && changed}
            onRule={(code, patch) => setDraft((d) => ({ ...d, rules: { ...d.rules, [code]: { ...d.rules[code], ...patch } } }))} />
        </Card>
      )}
    </div>
  );
}

// ---- hero ---------------------------------------------------------------------------------------

interface Kpi { k: string; get: (r: PolicySimResponse) => number | null; fmt: (v: number | null) => string; higherIsBetter: boolean; pts: boolean }
const KPIS: Kpi[] = [
  { k: "Approval rate", get: (r) => r.approval_rate, fmt: (v) => pct(v, 1), higherIsBetter: true, pts: true },
  { k: "Approved credit", get: (r) => r.approved_credit, fmt: money, higherIsBetter: true, pts: false },
  { k: "Default rate (approved)", get: (r) => r.approved_default_rate, fmt: (v) => pct(v, 2), higherIsBetter: false, pts: true },
  { k: "Credit at risk (approved)", get: (r) => r.approved_expected_loss, fmt: money, higherIsBetter: false, pts: false },
  { k: "Manual review rate", get: (r) => r.referral_rate, fmt: (v) => pct(v, 1), higherIsBetter: false, pts: true },
];

function Hero({ current, sim, loading }: { current: PolicySimResponse | null; sim: PolicySimResponse | null; loading: boolean }) {
  return (
    <div className={clsx("grid grid-cols-2 gap-3 transition-opacity duration-150 md:grid-cols-3 xl:grid-cols-5", loading && "opacity-60")}
      aria-busy={loading}>
      {KPIS.map((x) => {
        const a = current ? x.get(current) : null;
        const b = sim ? x.get(sim) : null;
        const d = a != null && b != null ? b - a : null;
        const same = d == null || (x.pts ? Math.abs(d) < 0.00005 : Math.abs(d) < 5e6);
        const better = !same && (d! > 0) === x.higherIsBetter;
        return (
          <div key={x.k} className={clsx("card card-pad", sim && !same && (better ? "border-good/60" : "border-bad/60"))}>
            <p className="text-[13px] font-medium text-ink-2">{x.k}</p>
            {current == null ? <div className="mt-2 h-9 w-24 animate-pulse rounded-md bg-raised" /> : sim && !same ? (
              <>
                <p className="num mt-1 flex items-center gap-1 text-[13px] text-ink-2">{x.fmt(a)} <ArrowRight className="h-3 w-3" aria-label="to" /></p>
                <p className="big-num text-[32px] leading-none text-ink">{x.fmt(b)}</p>
                <span className={clsx("pill mt-2 px-2 text-[12px]", better ? "pill-ok" : "pill-bad")}>
                  {d! > 0 ? <ArrowUp className="h-3 w-3" aria-hidden /> : <ArrowDown className="h-3 w-3" aria-hidden />}
                  {x.pts ? `${d! > 0 ? "+" : "−"}${Math.abs(d! * 100).toFixed(x.k.startsWith("Default") ? 2 : 1)} pts` : `${d! > 0 ? "+" : "−"}${money(Math.abs(d!))}`}
                  <span className="sr-only">{better ? " (better)" : " (worse)"}</span>
                </span>
              </>
            ) : (
              <p className="big-num mt-2 text-[32px] leading-none text-ink">{x.fmt(a)}</p>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ---- cut-offs (edited in the policy table) ----------------------------------------------------------

const STEP = 0.005;
const CUT_MAX = 0.3;

/** Clamp cut-off i to 0.5 pp steps, between its neighbours (one step apart) and 0.5%–30%. */
function clampCut(cuts: number[], i: number, v: number): number {
  const lo = i === 0 ? STEP : cuts[i - 1] + STEP;
  const hi = i === cuts.length - 1 ? CUT_MAX : cuts[i + 1] - STEP;
  return Math.min(hi, Math.max(lo, Math.round(v / STEP) * STEP));
}

/** Upper PD bound of grade i as a compact % input; commits on Enter or blur, clamped to its neighbours. */
function CutoffInput({ i, cuts, onChange }: { i: number; cuts: number[]; onChange: (c: number[]) => void }) {
  const shown = fixed(cuts[i] * 100, 1);
  const [text, setText] = useState(shown);
  useEffect(() => setText(shown), [shown]);
  const commit = () => {
    const v = Number(text.replace(",", "."));
    if (!Number.isFinite(v)) { setText(shown); return; }
    const next = [...cuts];
    next[i] = +clampCut(cuts, i, v / 100).toFixed(4);
    if (next[i] !== cuts[i]) onChange(next);
    setText(fixed(next[i] * 100, 1));
  };
  return (
    <span className="inline-flex items-center gap-1">
      <input className="input num w-[60px] px-2 py-0.5 text-right text-[13px]" inputMode="decimal" value={text}
        aria-label={`Grade ${GRADES[i]} upper PD bound, percent`}
        onChange={(e) => setText(e.target.value)} onBlur={commit} onKeyDown={(e) => { if (e.key === "Enter") commit(); }} />
      <span className="text-ink-2">%</span>
    </span>
  );
}

// ---- who moves ------------------------------------------------------------------------------------

const DECISION_LABEL: Record<Decision, string> = { approve: "Approve", review: "Manual review", reject: "Reject" };

interface Move { from: Decision; to: Decision; n: number; nHist: number; defaults: number; credit: number; atRisk: number }

/** Loans whose decision changes vs the configured policy, grouped by decision change (biggest first, max 4). */
function aggregateMoves(sim: PolicySimResponse | null): Move[] {
  const m = new Map<string, Move>();
  for (const t of sim?.transitions ?? []) {
    if (t.from_decision === t.to_decision) continue;
    const k = `${t.from_decision}>${t.to_decision}`;
    const r = m.get(k) ?? { from: t.from_decision, to: t.to_decision, n: 0, nHist: 0, defaults: 0, credit: 0, atRisk: 0 };
    // Loans and credit cover all applications; the default rate is defaults over historical loans only.
    r.n += t.n; r.nHist += t.n_historical ?? t.n; r.defaults += t.defaults; r.credit += t.requested_credit; r.atRisk += t.credit_at_risk;
    m.set(k, r);
  }
  return [...m.values()].sort((a, b) => b.n - a.n).slice(0, 4);
}

function WhoMoves({ rows, current, loading }: { rows: Move[]; current: PolicySimResponse | null; loading: boolean }) {
  if (!rows.length) return null;
  const base = current?.approved_default_rate ?? null;
  return (
    <section aria-label="Who moves" className={clsx("transition-opacity duration-150", loading && "opacity-60")}>
      <h3 className="mb-2 text-[14px] font-bold text-ink">Who moves</h3>
      <ul className="divide-y divide-line rounded-xl border border-line">
        {rows.map((r) => {
          const rate = r.nHist ? r.defaults / r.nHist : null;
          const worse = rate != null && base != null && rate > base;
          return (
            <li key={`${r.from}>${r.to}`} className="num flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2.5 text-[13px] text-ink-2">
              <span className="inline-flex items-center gap-1.5">
                <DecisionBadge decision={r.from} text={DECISION_LABEL[r.from]} size="sm" />
                <ArrowRight className="h-3.5 w-3.5" aria-hidden /><span className="sr-only">to</span>
                <DecisionBadge decision={r.to} text={DECISION_LABEL[r.to]} size="sm" />
              </span>
              <span><b className="text-ink">{num(r.n)}</b> loans</span>
              <span>
                <b className={rate == null ? "text-ink-2" : worse ? "text-bad" : "text-good"}>{pct(rate, 1)}</b> default
                {base != null && <> (approved now {pct(base, 1)})</>}
              </span>
              <span><b className="text-ink">{money(r.credit)}</b> credit</span>
              <span><b className="text-ink">{money(r.atRisk)}</b> at risk</span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

// ---- decision mix ---------------------------------------------------------------------------------

interface MixSegment { d: Decision; share: number; n: number; rate: number | null; credit: number }

/** Approve / Manual review / Reject shares with loans, default rate and credit per decision (from by_grade). */
function mixOf(r: PolicySimResponse): MixSegment[] {
  const agg: Record<Decision, { n: number; defaults: number; rated: number; requested: number }> = {
    approve: { n: 0, defaults: 0, rated: 0, requested: 0 },
    review: { n: 0, defaults: 0, rated: 0, requested: 0 },
    reject: { n: 0, defaults: 0, rated: 0, requested: 0 },
  };
  for (const g of r.by_grade) {
    const a = agg[g.decision];
    a.n += g.n;
    a.requested += g.requested_credit ?? 0;
    if (g.default_rate != null) { a.defaults += g.default_rate * g.n; a.rated += g.n; }
  }
  const credit: Record<Decision, number> = { approve: r.approved_credit, review: r.review_credit, reject: agg.reject.requested };
  const share: Record<Decision, number> = { approve: r.approval_rate, review: r.referral_rate, reject: r.reject_rate };
  return (["approve", "review", "reject"] as Decision[]).map((d) => ({
    d, share: share[d] ?? 0, n: agg[d].n, rate: agg[d].rated ? agg[d].defaults / agg[d].rated : null, credit: credit[d],
  }));
}

/** Segment colours = decision tokens; text stays readable in both themes (amber is light in both). */
const SEG: Record<Decision, string> = {
  approve: "bg-good text-surface",
  review: "bg-highlight text-[#15232B]",
  reject: "bg-bad text-surface",
};

/**
 * Current and Simulated as two full-width stacked bars. Widths animate (~400 ms) when the simulation changes;
 * the Simulated bar slides in only once something has changed.
 */
function DecisionMix({ current, sim, changed, loading }: {
  current: PolicySimResponse; sim: PolicySimResponse | null; changed: boolean; loading: boolean;
}) {
  const cur = mixOf(current);
  // Until the first simulated answer arrives the bar starts from the current mix, then animates to the new one.
  const next = sim ? mixOf(sim) : cur;
  return (
    <div className="space-y-2">
      <MixBar label="Current" segs={cur} />
      <div className={clsx("grid transition-all duration-300 ease-out", changed ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0")}
        aria-hidden={!changed}>
        <div className={clsx("overflow-hidden transition-opacity duration-150", loading && "opacity-70")}>
          <MixBar label="Simulated" segs={next} />
        </div>
      </div>
    </div>
  );
}

function MixBar({ label, segs }: { label: string; segs: MixSegment[] }) {
  return (
    <div className="grid grid-cols-[84px_minmax(0,1fr)] items-center gap-3">
      <span className="text-[13px] font-semibold text-ink">{label}</span>
      <div className="flex h-10 w-full overflow-hidden rounded-lg bg-raised" role="img"
        aria-label={`${label}: ${segs.map((s) => `${DECISION_LABEL[s.d]} ${pct(s.share, 1)}`).join(", ")}`}>
        {segs.map((s) => (
          <div key={s.d} className={clsx("flex min-w-0 items-center justify-center overflow-hidden whitespace-nowrap px-2 text-[12.5px] font-semibold transition-[width] duration-[400ms] ease-out", SEG[s.d])}
            style={{ width: `${s.share * 100}%` }}
            title={`${DECISION_LABEL[s.d]}: ${pct(s.share, 1)} · ${num(s.n)} loans · ${pct(s.rate, 1)} default · ${money(s.credit)} credit`}>
            {/* Name + % when it fits, % alone on narrow segments (the name stays in the tooltip). */}
            {s.share >= 0.2 ? <span className="truncate">{DECISION_LABEL[s.d]} <span className="num">{pct(s.share, 1)}</span></span>
              : s.share >= 0.05 ? <span className="num">{pct(s.share, 0)}</span> : null}
          </div>
        ))}
      </div>
    </div>
  );
}


// ---- policy by grade ------------------------------------------------------------------------------

function PolicyTable({ draft, shown, loading, onPolicy, onCuts }: {
  draft: Draft; shown: PolicySimResponse | null; loading: boolean; onPolicy: (g: Grade, p: Partial<Draft["policy"][Grade]>) => void;
  onCuts: (c: number[]) => void;
}) {
  const cuts = draft.thresholds;
  const pctCut = (v: number) => `${fixed(v * 100, v * 1000 % 10 ? 1 : 0)}%`;
  const byGrade = new Map((shown?.by_grade ?? []).map((r) => [r.grade, r]));
  return (
    <div className={clsx("overflow-x-auto transition-opacity duration-150", loading && "opacity-60")} aria-busy={loading}>
      <table className="table min-w-[760px]">
        <thead>
          <tr>
            <th>Grade</th><th>PD range</th><th>Decision</th><th>Max credit ÷ income</th>
            <th className="text-right">Loans</th><th className="text-right">Default rate</th><th className="text-right">Credit at risk</th>
          </tr>
        </thead>
        <tbody>
          {GRADES.map((grade, gi) => {
            const p = draft.policy[grade];
            const r = byGrade.get(grade);
            const reject = p.decision === "reject";
            return (
              <tr key={grade}>
                <td><GradeChip grade={grade} size="sm" /></td>
                <td className="num whitespace-nowrap text-[13px]">
                  {gi === GRADES.length - 1 ? <>{pctCut(cuts[gi - 1])}+</> : (
                    <span className="inline-flex items-center gap-1.5">
                      <span className="text-ink-2">{gi === 0 ? "0%" : pctCut(cuts[gi - 1])} –</span>
                      <CutoffInput i={gi} cuts={cuts} onChange={onCuts} />
                    </span>
                  )}
                </td>
                <td><DecisionSwitch grade={grade} value={p.decision} onChange={(decision) => onPolicy(grade, { decision })} /></td>
                <td>
                  <Stepper value={p.max} disabled={reject} label={`Grade ${grade} max credit to income`}
                    onChange={(max) => onPolicy(grade, { max })} />
                </td>
                <td className="num text-right">{r ? <>{num(r.n)} <span className="text-ink-2">({pct(r.share, 1)})</span></> : "–"}</td>
                <td className="num text-right">{pct(r?.default_rate, 1)}</td>
                <td className="num text-right">{r && p.decision === "approve" ? money(r.expected_loss) : "–"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function DecisionSwitch({ grade, value, onChange }: { grade: Grade; value: Decision; onChange: (d: Decision) => void }) {
  const opts: [Decision, string][] = [["approve", "Approve"], ["review", "Review"], ["reject", "Reject"]];
  return (
    <div role="radiogroup" aria-label={`Grade ${grade} decision`} className="inline-flex rounded-full border-[1.5px] border-line bg-surface p-0.5">
      {opts.map(([v, l]) => (
        <button key={v} type="button" role="radio" aria-checked={value === v} onClick={() => onChange(v)}
          className={clsx("whitespace-nowrap rounded-full px-2.5 py-0.5 text-[12px] font-semibold transition",
            value === v ? (v === "approve" ? "bg-good-soft text-good" : v === "review" ? "bg-highlight-soft text-ink" : "bg-bad-soft text-bad")
              : "text-ink-2 hover:bg-raised hover:text-ink")}>
          {l}
        </button>
      ))}
    </div>
  );
}

function Stepper({ value, disabled, label, onChange }: { value: number; disabled: boolean; label: string; onChange: (v: number) => void }) {
  const set = (v: number) => onChange(Math.max(0, Math.min(100, Math.round(v * 2) / 2)));
  return (
    <div className={clsx("inline-flex items-center gap-1", disabled && "opacity-40")} role="group" aria-label={label}>
      <button type="button" className="rounded-full border-[1.5px] border-line p-1 text-ink-2 hover:bg-raised disabled:cursor-not-allowed"
        disabled={disabled || value <= 0} onClick={() => set(value - 0.5)} aria-label="Decrease">
        <Minus className="h-3 w-3" aria-hidden />
      </button>
      <span className="num w-12 text-center text-[13px] font-semibold text-ink" aria-live="polite">{disabled ? "0×" : `${fixed(value, 1)}×`}</span>
      <button type="button" className="rounded-full border-[1.5px] border-line p-1 text-ink-2 hover:bg-raised disabled:cursor-not-allowed"
        disabled={disabled || value >= 100} onClick={() => set(value + 0.5)} aria-label="Increase">
        <Plus className="h-3 w-3" aria-hidden />
      </button>
    </div>
  );
}

// ---- rules ----------------------------------------------------------------------------------------

/** Rule fields in words. */
const FIELD_WORDS: Record<string, string> = {
  prev_approval_rate: "Previous approval rate",
  late_payment_ratio: "Late instalments",
  max_dpd_days: "Worst days late",
  has_current_overdue: "Currently overdue in the bureau",
};
const OP_WORDS: Record<string, string> = { lt: "<", lte: "≤", gt: ">", gte: "≥", eq: "=", neq: "≠" };

function RulesTable({ catalog, draft, results, loading, onRule }: {
  catalog: RuleDef[];
  draft: Draft;
  results?: SimRule[];
  loading: boolean;
  onRule: (code: string, patch: Partial<Draft["rules"][string]>) => void;
}) {
  const sim = new Map((results ?? []).map((r) => [r.code, r]));
  return (
    <div className={clsx("overflow-x-auto transition-opacity duration-150", loading && "opacity-60")} aria-busy={loading}>
      <table className="table min-w-[760px]">
        <thead>
          <tr>
            <th>On</th><th>Rule</th><th>Condition</th>
            <th className="text-right">Loans hit</th><th className="text-right">Default rate</th><th>Grade</th>
          </tr>
        </thead>
        <tbody>
          {catalog.map((r) => {
            const x = draft.rules[r.code] ?? { enabled: true, value: r.param?.value ?? null };
            const res = sim.get(r.code);
            // Loans hit = all applications; the default rate stays the historical one.
            const n = res?.n_violators_all ?? res?.n_violators ?? r.n_violators;
            const rate = res ? res.default_rate : r.default_rate;
            const grade = res ? res.grade : r.grade;
            return (
              <tr key={r.code} className={clsx(!x.enabled && "opacity-50")}>
                <td><Switch on={x.enabled} changed={!x.enabled} label={`${r.code} ${x.enabled ? "on" : "off"}`}
                  onChange={(enabled) => onRule(r.code, { enabled })} /></td>
                <td className="min-w-0">
                  <span className="block font-mono text-[12.5px] font-semibold text-ink">{r.code}</span>
                  <span className="block max-w-[280px] truncate text-[12.5px] text-ink-2" title={r.label}>{r.label}</span>
                </td>
                <td className="whitespace-nowrap text-[13px] text-ink">
                  {r.param ? (
                    <span className="inline-flex items-center gap-1.5">
                      {FIELD_WORDS[r.param.field] ?? r.param.field} {OP_WORDS[r.param.op] ?? r.param.op}
                      <RuleValueInput code={r.code} param={r.param} value={x.value ?? r.param.value} disabled={!x.enabled}
                        onChange={(value) => onRule(r.code, { value })} />
                    </span>
                  ) : <span className="text-ink-2">{r.group === "segment" ? "Customer segment graded D or E" : r.label}</span>}
                </td>
                <td className="num text-right">{num(n)}</td>
                <td className="num text-right">{pct(rate, 1)}</td>
                <td>{grade ? <GradeChip grade={grade} size="sm" /> : "–"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Switch({ on, changed, label, onChange }: { on: boolean; changed: boolean; label: string; onChange: (v: boolean) => void }) {
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} onClick={() => onChange(!on)}
      className={clsx("relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border-[1.5px] transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
        on ? "border-accent bg-accent" : changed ? "border-accent bg-raised" : "border-line bg-raised")}>
      <span className={clsx("inline-block h-3.5 w-3.5 rounded-full bg-surface shadow transition-transform", on ? "translate-x-[18px]" : "translate-x-0.5")} />
    </button>
  );
}

/**
 * Rule threshold as a compact input: ratios edited as % (0.5 ↔ "50"), days as days. Commits on Enter or
 * blur, clamped to [min, max] and rounded to the rule's step; a value off the configured one is highlighted.
 */
function RuleValueInput({ code, param, value, disabled, onChange }: {
  code: string; param: NonNullable<RuleDef["param"]>; value: number; disabled: boolean; onChange: (v: number) => void;
}) {
  const k = param.unit === "ratio" ? 100 : 1;
  const show = (v: number) => fixed(v * k, Number.isInteger(+(v * k).toFixed(6)) ? 0 : 1);
  const [text, setText] = useState(show(value));
  // `show` only depends on the unit, which never changes for a rule.
  useEffect(() => setText(show(value)), [value]);
  const commit = () => {
    const raw = Number(text.replace(",", ".")) / k;
    if (!Number.isFinite(raw)) { setText(show(value)); return; }
    const stepped = Math.round((raw - param.min) / param.step) * param.step + param.min;
    const v = +Math.min(param.max, Math.max(param.min, stepped)).toFixed(6);
    if (Math.abs(v - value) > 1e-9) onChange(v);
    setText(show(v));
  };
  const differs = Math.abs(value - param.value) > 1e-9;
  return (
    <span className="inline-flex items-center gap-1">
      <input className={clsx("input num w-[64px] px-2 py-0.5 text-right text-[13px]", differs && "border-accent bg-accent-soft/40")}
        inputMode="decimal" value={text} disabled={disabled} aria-label={`${code} threshold${param.unit === "ratio" ? ", percent" : ", days"}`}
        onChange={(e) => setText(e.target.value)} onBlur={commit} onKeyDown={(e) => { if (e.key === "Enter") commit(); }} />
      <span className="text-ink-2">{param.unit === "ratio" ? "%" : "days"}</span>
    </span>
  );
}
