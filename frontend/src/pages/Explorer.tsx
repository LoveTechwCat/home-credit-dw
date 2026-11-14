import clsx from "clsx";
import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight, Columns3, Search, X } from "lucide-react";
import { useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { DecisionBadge, DssError } from "../components/AssessmentView";
import { GradeChip } from "../components/charts";
import { CohortBar, Popover } from "../components/CohortBar";
import { Card, ChartSkeleton, PageHeader } from "../components/ui";
import {
  useApi, usePostApi, type CohortMetrics, type Decision, type ExploreApplicationsResponse, type ExploreField,
} from "../lib/api";
import { COHORT_PARAM, activeConditions, checkCohort, readCohort, writeCohort, type UiCondition } from "../lib/cohort";
import { compact, dateLabel, fixed, label, num, pct } from "../lib/format";

const PAGE_SIZE = 50;
const MAX_EXTRA_COLUMNS = 12;
const COLS_PARAM = "cols";

/** Explorer (/explore): where risk is concentrated, for any cohort built in the filter bar. */
export default function Explorer() {
  const [params, setParams] = useSearchParams();
  const fields = useApi<{ fields: ExploreField[] }>("/api/explore/fields");
  const catalog = useMemo(() => fields.data?.fields ?? [], [fields.data]);
  const ready = !!fields.data;
  const cohortRaw = params.get(COHORT_PARAM);
  const colsRaw = params.get(COLS_PARAM);
  const conditions = useMemo(() => readCohort(cohortRaw), [cohortRaw]);
  // Anything in the URL that the catalog does not know (edited link, renamed field) is shown but never sent.
  const invalid = useMemo(() => (ready ? checkCohort(conditions, catalog) : new Set<number>()), [ready, conditions, catalog]);
  const active = useMemo(() => activeConditions(conditions.filter((_, i) => !invalid.has(i))), [conditions, invalid]);
  const extraCols = useMemo(() => {
    const known = new Set(catalog.map((f) => f.key));
    return (colsRaw ?? "").split(",").filter((k) => k && (!ready || known.has(k))).slice(0, MAX_EXTRA_COLUMNS);
  }, [colsRaw, catalog, ready]);

  // Cohort and added columns live in the URL so a view can be shared.
  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value); else next.delete(key);
    setParams(next, { replace: true });
  };
  const setConditions = (c: UiCondition[]) => setParam(COHORT_PARAM, writeCohort(c));
  const setColumns = (cols: string[]) => setParam(COLS_PARAM, cols.length ? cols.join(",") : null);

  // Requests start once the catalog is known (so the cohort could be validated) and only change on
  // Apply / toggle / remove: editing inside a popover is local state and never touches the URL.
  const metrics = usePostApi<{ cohort: CohortMetrics; book: CohortMetrics }>("explore/metrics", ready ? { conditions: active } : null);

  return (
    <div className="space-y-6">
      <PageHeader title="Explorer" />

      {fields.error ? <DssError status={fields.status} message={fields.error} />
        : <CohortBar fields={catalog} conditions={conditions} invalid={invalid} onChange={setConditions} />}

      {/* Stale-while-revalidate: the previous numbers stay (dimmed) while a new cohort loads, so nothing
          unmounts or jumps; the skeleton appears only before the first answer and reserves the same height. */}
      <div className="relative min-h-[460px]">
        {metrics.loading && metrics.data && <LoadingBar />}
        {metrics.error ? <DssError status={metrics.status} message={metrics.error} />
          : metrics.data ? (
            <div className={clsx("transition-opacity duration-150", metrics.loading && "opacity-60")} aria-busy={metrics.loading}>
              <Metrics m={metrics.data} filtered={active.length > 0} />
            </div>
          ) : <ChartSkeleton height={440} />}
      </div>

      <Applications ready={ready} conditions={active} catalog={catalog} extraCols={extraCols} onColumns={setColumns} />
    </div>
  );
}

// ---- metrics ----------------------------------------------------------------------------------

/** Thin indeterminate bar shown over data that is being refreshed. */
function LoadingBar() {
  return (
    <div className="absolute inset-x-0 -top-3 h-0.5 overflow-hidden rounded-full bg-accent/15" role="progressbar" aria-label="Updating">
      <div className="h-full w-1/3 animate-pulse rounded-full bg-accent" />
    </div>
  );
}

type M = CohortMetrics;
type Direction = "up-good" | "up-bad" | "neutral";

/** "16.6K applications · 4.7% of book · 7.8B credit · 14.4K with known outcome" (muted, under the filter bar). */
function CohortSummary({ c, b, filtered }: { c: M; b: M; filtered: boolean }) {
  return (
    <p className="num -mt-3 px-1 text-[13px] text-ink-2">
      {filtered
        ? <>{compact(c.n_applications)} applications · {pct(c.share_of_book, 1)} of all · {compact(c.total_credit)} credit
          {b.total_credit ? ` (${pct(c.total_credit / b.total_credit, 1)} of all)` : ""} · {compact(c.n_historical)} with known outcome</>
        : <>All · {compact(c.n_applications)} applications · {compact(c.total_credit)} credit</>}
    </p>
  );
}

interface Kpi {
  k: string;
  tip: string;
  value: string;
  /** Delta vs book, already formatted, plus whether "up" is better; null hides the chip. */
  delta: { text: string; up: boolean; same: boolean; dir: Direction } | null;
}

/** Rate delta in percentage points. */
function ptsDelta(a: number | null, b: number | null, dir: Direction): Kpi["delta"] {
  if (a == null || b == null) return null;
  const d = (a - b) * 100;
  const same = Math.abs(d) < 0.05;
  return { text: same ? "same" : `${d > 0 ? "+" : "−"}${Math.abs(d).toFixed(1)} pts`, up: d > 0, same, dir };
}

function KpiStrip({ c, b, filtered }: { c: M; b: M; filtered: boolean }) {
  const dr = (m: M) => (m.n_historical ? m.blocked_risk.defaults_historical / m.n_historical : null);
  const kpis: Kpi[] = [
    { k: "Default rate", tip: "Share of historical loans that defaulted", value: pct(dr(c), 2),
      delta: ptsDelta(dr(c), dr(b), "up-bad") },
    { k: "Approval rate", tip: "Share of applications approved automatically", value: pct(c.auto_approve_rate, 1),
      delta: ptsDelta(c.auto_approve_rate, b.auto_approve_rate, "neutral") },
    { k: "Manual review rate", tip: "Share of applications sent to manual review", value: pct(c.manual_review_rate, 1),
      delta: ptsDelta(c.manual_review_rate, b.manual_review_rate, "up-bad") },
    { k: "Reject rate", tip: "Share of applications rejected", value: pct(c.reject_rate, 1),
      delta: ptsDelta(c.reject_rate, b.reject_rate, "neutral") },
    { k: "Bad capture rate", tip: "Share of defaulters stopped by manual review or reject", value: pct(c.blocked_risk.blocked_share, 1),
      delta: ptsDelta(c.blocked_risk.blocked_share, b.blocked_risk.blocked_share, "up-good") },
  ];
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
      {kpis.map((x) => (
        <div key={x.k} className="card card-pad" title={x.tip}>
          <p className="text-[13px] font-medium text-ink-2">{x.k}<span className="sr-only">: {x.tip}</span></p>
          <p className="big-num mt-1.5 text-[26px] leading-none text-ink">{x.value}</p>
          {filtered && x.delta && <DeltaChip d={x.delta} />}
        </div>
      ))}
    </div>
  );
}

function DeltaChip({ d }: { d: NonNullable<Kpi["delta"]> }) {
  const tone = d.same || d.dir === "neutral" ? "pill-neutral" : (d.up === (d.dir === "up-good")) ? "pill-ok" : "pill-bad";
  const Icon = d.same ? null : d.up ? ArrowUp : ArrowDown;
  return (
    <span className={clsx("pill mt-2 px-2 text-[11.5px]", tone)}>
      {Icon && <Icon className="h-3 w-3" aria-hidden />}{d.text}<span className="sr-only"> vs all</span>
    </span>
  );
}

/** Rows of the decision × outcome view, straight from the API's `outcome_by_decision` (historical loans). */
function outcomeRows(c: M) {
  const o = c.outcome_by_decision;
  return [
    { decision: "approve" as const, name: "Approve", total: o.approve.good + o.approve.bad, good: o.approve.good, bad: o.approve.bad },
    { decision: "review" as const, name: "Manual review", total: o.review.good + o.review.bad, good: o.review.good, bad: o.review.bad },
    { decision: "reject" as const, name: "Reject", total: o.reject.good + o.reject.bad, good: o.reject.good, bad: o.reject.bad },
  ];
}

/** Decision × outcome for the cohort's historical loans: one repaid / defaulted bar per decision. */
function DecisionOutcome({ c }: { c: M }) {
  const rows = outcomeRows(c);
  // Container width decides the layout (the card can sit in a narrow column, not only on a phone).
  const ref = useRef<HTMLDivElement>(null);
  const width = useWidth(ref);
  const stacked = width > 0 && width < 480;
  const empty = !c.n_historical || rows.every((r) => r.total === 0);

  return (
    <Card title="Decision × outcome">
      <div ref={ref}>
        {empty ? (
          <p className="text-[13px] text-ink-2">No historical loans in this cohort.</p>
        ) : (
          <>
            <div className="space-y-5">
              {rows.map((r) => (
                <div key={r.name} className={clsx("grid gap-x-4 gap-y-2", !stacked && "grid-cols-[150px_minmax(0,1fr)]")}>
                  <div className={clsx(!stacked && "pt-1")}>
                    <DecisionBadge decision={r.decision} text={r.name} size="sm" />
                    {r.total > 0 && <p className="num mt-1 text-[12px] text-ink-2">{num(r.total)} loans</p>}
                  </div>
                  {r.total === 0 ? (
                    // Nothing to draw: a quiet line instead of an empty bar and "0 repaid".
                    <p className={clsx("text-[12.5px] text-ink-3", !stacked && "pt-1.5")}>No loans</p>
                  ) : <OutcomeBar r={r} />}
                </div>
              ))}
            </div>
            <p className="mt-4 flex flex-wrap gap-4 text-[12px] text-ink-2">
              <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-sm bg-good/70" aria-hidden /> Repaid</span>
              <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-sm bg-bad" aria-hidden /> Defaulted</span>
              <span>Historical loans only</span>
            </p>
          </>
        )}
      </div>
    </Card>
  );
}

/** Repaid | defaulted bar; under it one block per segment (tag on its own line), kept inside the bar's width. */
function OutcomeBar({ r }: { r: ReturnType<typeof outcomeRows>[number] }) {
  const goodShare = r.total ? r.good / r.total : 0;
  const badShare = r.total ? r.bad / r.total : 0;
  return (
    <div className="min-w-0">
      <div className="flex h-7 w-full overflow-hidden rounded-md bg-raised" role="img"
        aria-label={`${r.name}: ${num(r.good)} repaid (${pct(goodShare, 1)}), ${num(r.bad)} defaulted (${pct(badShare, 1)})`}>
        {r.good > 0 && (
          <div className="h-full bg-good/70"
            style={{ width: `${goodShare * 100}%` }} title={`Repaid: ${num(r.good)} · ${pct(goodShare, 1)}`} />
        )}
        {r.bad > 0 && (
          <div className="h-full border-l-2 border-surface bg-bad"
            style={{ width: `${badShare * 100}%` }} title={`Defaulted: ${num(r.bad)} · ${pct(badShare, 1)}`} />
        )}
      </div>
      {/* Repaid count under the bar's left edge, defaulted count under its right edge; they wrap when tight. */}
      <div className="mt-1.5 flex flex-wrap justify-between gap-x-4 gap-y-1 text-[12.5px]">
        {r.good > 0 && <SegmentInfo kind="repaid" n={r.good} share={goodShare} align="start" />}
        {r.bad > 0 && <SegmentInfo kind="defaulted" n={r.bad} share={badShare} align="end" />}
      </div>
    </div>
  );
}

function SegmentInfo({ kind, n, share, align }: { kind: "repaid" | "defaulted"; n: number; share: number; align: "start" | "end" }) {
  return (
    <span className={clsx("num whitespace-nowrap text-ink-2", align === "end" && "ml-auto text-right")}>
      <b className="text-ink">{num(n)}</b> {kind} · {pct(share, 1)}
    </span>
  );
}

/** Width of an element, kept up to date with a ResizeObserver. */
function useWidth(ref: RefObject<HTMLElement>): number {
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.getBoundingClientRect().width);
    const ro = new ResizeObserver(([e]) => setW(e.contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return w;
}

function Metrics({ m, filtered }: { m: { cohort: M; book: M }; filtered: boolean }) {
  return (
    <div className="space-y-6">
      <CohortSummary c={m.cohort} b={m.book} filtered={filtered} />
      <KpiStrip c={m.cohort} b={m.book} filtered={filtered} />
      <DecisionOutcome c={m.cohort} />
    </div>
  );
}

// ---- applications list --------------------------------------------------------------------------

type Sort = { field: string; dir: "asc" | "desc" };
const FIXED: { key: string; label: string }[] = [
  { key: "application_id", label: "Application id" },
  { key: "loan_set", label: "Loan set" },
  { key: "final_grade", label: "Final grade" },
  { key: "decision", label: "Decision" },
  { key: "pd", label: "PD" },
  { key: "outcome", label: "Outcome" },
];

function Applications({ ready, conditions, catalog, extraCols, onColumns }: {
  ready: boolean;
  conditions: ReturnType<typeof activeConditions>;
  catalog: ExploreField[];
  extraCols: string[];
  onColumns: (cols: string[]) => void;
}) {
  const navigate = useNavigate();
  const [sort, setSort] = useState<Sort>({ field: "pd", dir: "desc" });
  // The page belongs to one cohort + sort: changing either starts again from page 1 (no extra request).
  const viewKey = JSON.stringify([conditions, sort]);
  const [paging, setPaging] = useState({ key: viewKey, page: 1 });
  const page = paging.key === viewKey ? paging.page : 1;
  const setPage = (p: number) => setPaging({ key: viewKey, page: p });

  // One column picker, opened from the header button or the "+" at the end of the table header row;
  // focus returns to whichever control opened it.
  const [picker, setPicker] = useState(false);
  const opener = useRef<HTMLElement | null>(null);
  const headBtn = useRef<HTMLButtonElement>(null);
  // Both triggers toggle: a click while open closes the picker (the popover ignores mousedown on them).
  const togglePicker = (e: { currentTarget: HTMLElement }) => {
    if (picker) { setPicker(false); return; }
    opener.current = e.currentTarget;
    setPicker(true);
  };
  const closePicker = (refocus: boolean) => {
    setPicker(false);
    if (refocus) requestAnimationFrame(() => opener.current?.focus());
  };

  const res = usePostApi<ExploreApplicationsResponse>("explore/applications",
    ready ? { conditions, columns: extraCols, sort, page, page_size: PAGE_SIZE } : null);
  const byKey = useMemo(() => new Map(catalog.map((f) => [f.key, f])), [catalog]);
  const d = res.data;
  const pages = d ? Math.max(1, Math.ceil(d.total / d.page_size)) : 1;

  const header = (key: string, text: string) => {
    const on = sort.field === key;
    return (
      <th key={key} aria-sort={on ? (sort.dir === "asc" ? "ascending" : "descending") : "none"} className="whitespace-nowrap">
        <button type="button" className="inline-flex items-center gap-1 uppercase tracking-[0.06em] hover:text-ink"
          onClick={() => setSort((s) => ({ field: key, dir: s.field === key && s.dir === "desc" ? "asc" : "desc" }))}>
          {text}
          {on ? (sort.dir === "desc" ? <ArrowDown className="h-3 w-3 text-accent" aria-hidden /> : <ArrowUp className="h-3 w-3 text-accent" aria-hidden />)
            : <ArrowDown className="h-3 w-3 opacity-30" aria-hidden />}
        </button>
      </th>
    );
  };

  return (
    <Card title="Applications">
      {/* Header row: total on the left, the column chooser on the right. */}
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <p className="text-[13px] text-ink-2" aria-live="polite">
          {d ? <><b className="num text-ink">{num(d.total)}</b> applications</> : "…"}
          {res.loading && d && <span className="ml-2 text-ink-3">updating…</span>}
        </p>
        <div className="relative">
          <button ref={headBtn} type="button" className="btn-add" aria-expanded={picker} aria-haspopup="dialog" onClick={togglePicker}>
            <Columns3 className="h-4 w-4" aria-hidden /> Add columns
            {extraCols.length > 0 && (
              <span className={clsx("num rounded-full px-1.5 text-[11.5px] font-bold",
                picker ? "bg-accent-on/20 text-accent-on" : "bg-accent text-accent-on")}>{extraCols.length}</span>
            )}
          </button>
          {picker && <ColumnPicker catalog={catalog} chosen={extraCols} onChange={onColumns} onClose={closePicker}
            ignore={() => [headBtn.current]} />}
        </div>
      </div>
      {extraCols.length > 0 && (
        <div className="mb-3 flex flex-wrap justify-end gap-1.5" aria-label="Added columns">
          {extraCols.map((k) => (
            <span key={k} className="pill pill-neutral gap-1 pr-1">
              {byKey.get(k)?.label ?? k}
              <button type="button" className="rounded-full p-0.5 hover:bg-surface" aria-label={`Remove column ${byKey.get(k)?.label ?? k}`}
                onClick={() => onColumns(extraCols.filter((x) => x !== k))}>
                <X className="h-3 w-3" aria-hidden />
              </button>
            </span>
          ))}
        </div>
      )}

      {res.error ? <DssError status={res.status} message={res.error} /> : !d ? <ChartSkeleton height={300} /> : (
        <>
          <div className={clsx("overflow-x-auto transition-opacity", res.loading && "opacity-60")}>
            <table className="table">
              <thead>
                <tr>
                  {FIXED.map((c) => header(c.key, c.label))}
                  {extraCols.map((k) => header(k, byKey.get(k)?.label ?? k))}
                </tr>
              </thead>
              <tbody>
                {d.rows.length === 0 && (
                  <tr><td colSpan={FIXED.length + extraCols.length} className="py-6 text-center text-ink-2">No application matches this cohort.</td></tr>
                )}
                {d.rows.map((r) => (
                  <tr key={r.application_id} className="cursor-pointer"
                    onClick={() => navigate(`/applications-details?id=${r.application_id}`)}>
                    <td className="num">
                      <Link to={`/applications-details?id=${r.application_id}`} className="font-semibold text-accent-ink hover:underline"
                        onClick={(e) => e.stopPropagation()}>{r.application_id}</Link>
                    </td>
                    <td className="text-ink-2">{loanSet(r.loan_set)}</td>
                    <td>{r.final_grade ? <GradeChip grade={r.final_grade} size="sm" /> : "–"}</td>
                    <td>{decisionCode(r.decision) ? <DecisionBadge decision={decisionCode(r.decision)!} text={r.decision!} size="sm" /> : "–"}</td>
                    <td className="num text-right">{pct(r.pd, 1)}</td>
                    <td>{outcome(r.outcome)}</td>
                    {extraCols.map((k) => <td key={k} className="num text-ink-2">{cell(r[k], byKey.get(k))}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <nav className="mt-3 flex items-center justify-between gap-2 text-[13px] text-ink-2" aria-label="Pages">
            <span className="num">Page {num(d.page)} of {num(pages)}</span>
            <span className="flex gap-2">
              <button type="button" className="btn text-xs" disabled={page <= 1} onClick={() => setPage(page - 1)}>
                <ChevronLeft className="h-3.5 w-3.5" aria-hidden /> Previous
              </button>
              <button type="button" className="btn text-xs" disabled={page >= pages} onClick={() => setPage(page + 1)}>
                Next <ChevronRight className="h-3.5 w-3.5" aria-hidden />
              </button>
            </span>
          </nav>
        </>
      )}
    </Card>
  );
}

/** The list returns decision labels; the badge needs the code for its colour and icon. */
const DECISION_BY_LABEL: Record<string, Decision> = { approve: "approve", "manual review": "review", review: "review", reject: "reject" };
const decisionCode = (v: string | null): Decision | null => (v ? DECISION_BY_LABEL[v.toLowerCase()] ?? null : null);

const loanSet = (v: string | null) =>
  v == null ? "–" : /hist|train/i.test(v) ? "Historical loan" : /new|test/i.test(v) ? "New application" : label(v);

function outcome(v: string | null) {
  if (v == null || v === "" || /unknown/i.test(v)) return <span className="text-ink-2">Unknown</span>;
  if (/default|bad/i.test(v)) return <span className="pill pill-bad text-xs">Defaulted</span>;
  if (/repaid|good/i.test(v)) return <span className="pill pill-ok text-xs">Repaid</span>;
  return label(v);
}

function cell(v: string | number | boolean | null | undefined, f?: ExploreField): string {
  if (v == null || v === "") return "–";
  if (f?.type === "date") return dateLabel(v);
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "number") {
    if (Math.abs(v) >= 1e4) return compact(v);
    return Number.isInteger(v) ? num(v) : fixed(v, f?.type === "number" && Math.abs(v) < 1 ? 3 : 2);
  }
  return label(String(v));
}

/** "+ Columns": searchable list of catalog fields to add (up to 12), grouped like the filter picker. */
function ColumnPicker({ catalog, chosen, onChange, onClose, ignore }: {
  catalog: ExploreField[]; chosen: string[]; onChange: (cols: string[]) => void; onClose: (refocus: boolean) => void;
  ignore: () => (Element | null)[];
}) {
  const [q, setQ] = useState("");
  const fixedKeys = new Set(FIXED.map((f) => f.key));
  const options = catalog.filter((f) => !fixedKeys.has(f.key) && f.label.toLowerCase().includes(q.trim().toLowerCase()));
  const full = chosen.length >= MAX_EXTRA_COLUMNS;
  return (
    <Popover label="Add columns" align="right" onClose={onClose} ignore={ignore}>
      <div className="p-3">
        <label className="relative block">
          <span className="sr-only">Search columns</span>
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-3" aria-hidden />
          <input autoFocus className="input pl-9" placeholder="Search columns" value={q} onChange={(e) => setQ(e.target.value)} />
        </label>
        <div className="mt-2 max-h-72 overflow-y-auto">
          {options.map((f) => {
            const on = chosen.includes(f.key);
            return (
              <label key={f.key} title={!on && full ? `Up to ${MAX_EXTRA_COLUMNS} columns` : undefined}
                className={clsx("flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-[13px] text-ink hover:bg-raised",
                !on && full && "cursor-not-allowed opacity-50")}>
                <input type="checkbox" className="h-4 w-4 accent-[rgb(var(--accent))]" checked={on} disabled={!on && full}
                  onChange={() => onChange(on ? chosen.filter((x) => x !== f.key) : [...chosen, f.key])} />
                <span className="flex-1">{f.label}</span>
                <span className="text-[11px] text-ink-2">{f.group}</span>
              </label>
            );
          })}
          {options.length === 0 && <p className="p-2 text-[13px] text-ink-2">No column matches.</p>}
        </div>
      </div>
    </Popover>
  );
}
